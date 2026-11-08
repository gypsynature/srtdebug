"""
srtdebug payload analysis - reads MPEG-TS and reports what is inside the stream.

Standard library only. Works on bytes as they arrive, so it can sit behind an SRT
receiver. It measures, per second:
  - mux rate, and bitrate per PID (video, audio, data)
  - null packet share (PID 0x1FFF)
  - continuity-counter errors (lost or reordered packets)
  - PCR discontinuities and backwards PCR
  - PAT / PMT version changes (the muxer restarted its tables)
  - codecs, from the PMT stream_type

Attribution (attribute()) turns per-second evidence into suspected causes. It is
inference from one end of the link, so every finding carries a confidence level and
the evidence it is based on.
"""

import bisect
import os
import re
import statistics
import threading
import time
from collections import defaultdict, deque

TS_PACKET = 188
NULL_PID = 0x1FFF
PCR_JUMP_S = 0.1          # a PCR gap larger than this, against wall time, is flagged

STREAM_TYPES = {
    0x01: "MPEG-1 video", 0x02: "MPEG-2 video", 0x03: "MPEG-1 audio", 0x04: "MPEG-2 audio",
    0x0F: "AAC (ADTS)", 0x11: "AAC (LATM)", 0x1B: "H.264/AVC", 0x24: "H.265/HEVC",
    0x06: "private data", 0x81: "AC-3", 0x87: "E-AC-3", 0x86: "SCTE-35", 0x15: "metadata",
}


SDT_PID = 0x0011
PCR_REPEAT_MAX_S = 0.040       # TR 101 290 2.3a: longest allowed gap between PCRs on one PID (DVB, 40 ms)
PCR_DISC_MAX_S = 0.100         # TR 101 290 2.3: a PCR step over 100 ms needs the discontinuity indicator
CAT_PID = 0x0001
PSI_PIDS = {0x0000, 0x0001, 0x0010, 0x0011, 0x0012, 0x0013, 0x0014}   # not "unreferenced" PIDs
PCR_GAP_WINDOW = 5000          # recent PCR intervals kept per PID for the median
# Each 188-byte TS packet carries 184 bytes of PES payload, so a codec's own rate is the measured
# stream rate times 184/188 (PES headers are a little more, so treat the result as approximate).
TS_PAYLOAD_RATIO = 184 / 188


def _text(b):
    """DVB text: an optional encoding byte (below 0x20) then the characters."""
    b = bytes(b)
    if b and b[0] < 0x20:
        b = b[1:]
    return b.decode("latin-1", "replace").strip().strip("\x00")


def codec_name(stream_type):
    return STREAM_TYPES.get(stream_type, f"stream type 0x{stream_type:02X}")


def is_video(stream_type):
    return stream_type in (0x01, 0x02, 0x1B, 0x24)


PSI_NAMES = {0x0000: "PAT (programme list)", 0x0001: "CAT (conditional access)", 0x0010: "NIT (network)",
             0x0011: "SDT (service names)", 0x0012: "EIT (programme guide)", 0x0013: "RST",
             0x0014: "TDT/TOT (clock)", 0x1FFF: "Null packets (padding)"}
LANGUAGE_NAME = {"eng": "English", "fra": "French", "fre": "French", "deu": "German", "ger": "German",
                 "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "jpn": "Japanese",
                 "zho": "Chinese", "chi": "Chinese", "kor": "Korean", "rus": "Russian", "ara": "Arabic",
                 "und": "undetermined"}
REGISTRATION_NAME = {"HEVC": "HEVC video", "AV01": "AV1 video", "VC-1": "VC-1 video", "DTS1": "DTS audio",
                     "DTS2": "DTS audio", "DTS3": "DTS audio", "AC-4": "AC-4 audio", "Opus": "Opus audio",
                     "BSSD": "SMPTE 302M audio", "KLVA": "KLV metadata", "SCTE": "SCTE 35 cue messages"}


def describe_one_descriptor(tag, body):
    """Plain-language name for one descriptor, from its tag and body."""
    return describe_descriptors(bytes([tag, len(body)]) + bytes(body))[0]


def split_descriptors(buf):
    """(tag, body) pairs for the descriptor loop that follows an elementary stream in the PMT."""
    out, j, buf = [], 0, bytes(buf)
    while j + 2 <= len(buf):
        tag, n = buf[j], buf[j + 1]
        out.append((tag, buf[j + 2:j + 2 + n]))
        j += 2 + n
    return out


def describe_descriptors(buf):
    """Plain-language notes from the descriptors that follow an elementary stream in the PMT:
    language, audio flavour, subtitles, conditional access and so on. Unknown tags are listed by number."""
    out, j = [], 0
    buf = bytes(buf)
    while j + 2 <= len(buf):
        tag, n = buf[j], buf[j + 1]
        body = buf[j + 2:j + 2 + n]
        j += 2 + n
        if tag == 0x0A and len(body) >= 3:                       # ISO 639 language
            code = body[:3].decode("latin-1", "replace").lower()
            kind = {1: ", clean effects", 2: ", hearing impaired", 3: ", visually impaired"}.get(
                body[3] if len(body) > 3 else 0, "")
            out.append(f"language {LANGUAGE_NAME.get(code, code)}{kind}")
        elif tag == 0x6A:
            out.append("AC-3 audio")
        elif tag == 0x7A:
            out.append("E-AC-3 audio")
        elif tag == 0x7C:
            out.append("AAC audio")
        elif tag == 0x59:
            out.append("DVB subtitles")
        elif tag == 0x56:
            out.append("teletext")
        elif tag == 0x28:
            out.append("AVC video")
        elif tag == 0x05 and len(body) >= 4:                     # registration: four-character format name
            fourcc = body[:4].decode("latin-1", "replace")
            out.append(REGISTRATION_NAME.get(fourcc, f"format {fourcc}"))
        elif tag == 0x09 and len(body) >= 2:                     # conditional access
            out.append(f"scrambled, CA system 0x{(body[0] << 8) | body[1]:04X}")
        elif tag == 0x52 and body:                               # stream identifier
            out.append(f"component tag {body[0]}")
        elif tag == 0x7F and body:                               # extension descriptor
            out.append("extension descriptor")
        else:
            out.append(f"descriptor 0x{tag:02X}")
    return out


_CRC_T = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = (((_c << 1) ^ 0x04C11DB7) if _c & 0x80000000 else (_c << 1)) & 0xFFFFFFFF
    _CRC_T.append(_c)


def crc32_mpeg(data):
    """CRC-32 as MPEG-2 PSI uses it. A complete section, CRC included, gives 0."""
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_T[((crc >> 24) ^ b) & 0xFF]
    return crc


def is_audio(stream_type):
    return stream_type in (0x03, 0x04, 0x0F, 0x11, 0x81, 0x87)


class TSAnalyzer:
    """Feed it raw TS bytes with feed(data, now). Read results from windows and tables."""

    LOCK_DEADLINE_S = 30.0    # if PAT, PMT and PCR are not all seen by then, start-up errors are counted anyway

    def __init__(self, start=None, keep_windows=900):
        self.start = start if start is not None else time.monotonic()
        self.buf = bytearray()
        self.last_cc = {}                       # pid -> last continuity counter seen
        self.pat = {}                           # program -> pmt pid
        self.pmt_pids = set()
        self.pat_version = None
        self.pmts = {}                          # program -> {"version", "pcr_pid", "streams": {pid: type}}
        self.pmt_versions = {}                  # program -> last version
        self._av = {}                           # pid -> "video" | "audio" (from the PMT)
        self.last_pcr = None                    # (pcr_27mhz, arrival)
        self.windows = deque(maxlen=keep_windows)
        self.totals = defaultdict(int)          # lifetime counters
        self.events = []                        # (utc time, kind, detail, None)
        self._cur = None                        # the second being filled
        self._cur_sec = None
        self.bytes_total = 0
        self.pid_total = defaultdict(int)      # bytes per PID for the whole run, for the PID table
        self.pid_af = defaultdict(int)         # adaptation-field bytes per PID (stuffing, PCR)
        self.pid_dup = defaultdict(int)        # packets repeated with the same continuity counter, per PID
        self.pid_pk = defaultdict(int)         # packets per PID
        self.last_data = None                   # monotonic time the last bytes arrived
        self.sdt = {}                           # service description: who muxed the stream
        # ETSI TR 101 290 checks (priority 1 to 3). Counts are episodes, not packets.
        self.tr = defaultdict(int)
        self.tr_first = {}                      # check -> UTC time of its first episode
        self.tr_last = defaultdict(lambda: deque(maxlen=10))   # check -> UTC times of the latest episodes
        self.tr_seen = {}                       # pid -> monotonic time its last packet arrived
        self.tr_pmt_seen = {}                   # PMT pid -> time its last table arrived
        self.tr_missing = set()                 # timers currently expired (each counts once)
        self.tr_pcr = {}                        # pid -> last PCR (27 MHz)
        self.tr_pts = {}                        # pid -> arrival time of the last PES header with a PTS
        self.tr_next_check = 0.0
        self.tr_unref = set()                   # PIDs counted as unreferenced already
        self.pcr_gaps = {}                      # pid -> [count, sum, max, recent gaps] in seconds
        self.pcr_early = {}                     # pid -> [count, max] for intervals in the first CONNECT_WINDOW_S seconds
        self.cat_seen = False                   # a CAT (table_id 0x01) has been seen on PID 0x0001
        self._scrambled_no_cat = False          # the "scrambled without CAT" episode is open
        self._in_sync = True
        # keyframes: PTS of video access units that start at a random-access point (adaptation flag)
        self.ri_pts = deque(maxlen=60)
        self.ri_total = 0
        # start-up: until PAT, PMT and a first PCR have all been seen the stream is not locked. Joining mid-stream
        # always shows some errors, so until the lock they are logged (tagged) but not counted.
        self.locked_at = None                   # monotonic time the stream locked
        self.locked_stamp = None                # UTC time of the lock
        self.startup = defaultdict(int)         # checks seen before the lock: name -> episodes (not counted)
        self.startup_first = {}
        # errors in the first CONNECT_WINDOW_S seconds of data, and after (all checks, counted or not)
        self.first_data = None
        self._now = None
        self.hits_early = 0
        self.hits_late = 0
        self.tr_early = defaultdict(int)        # per check: episodes in the first CONNECT_WINDOW_S seconds
        self.tr_late = defaultdict(int)         # per check: episodes after that

    # ---------------------------------------------------------------- feeding
    def feed(self, data, now=None):
        now = time.monotonic() if now is None else now
        if data:
            self.last_data = now
            if self.first_data is None:
                self.first_data = now
        self._now = now
        self.buf.extend(data)
        # Packets must start with 0x47 and repeat every 188 bytes. Resync if not.
        pos = 0
        n = len(self.buf)
        while pos + TS_PACKET <= n:
            if self.buf[pos] != 0x47:
                if self._in_sync:
                    self._tr_hit("sync_byte")
                    self._in_sync = False
                nxt = self.buf.find(b"\x47", pos + 1)
                if nxt < 0:
                    pos = n
                    break
                self.totals["resync_bytes"] += nxt - pos
                pos = nxt
                continue
            self._in_sync = True
            self._packet(bytes(self.buf[pos:pos + TS_PACKET]), now)
            pos += TS_PACKET
        del self.buf[:pos]

    def _second(self, now):
        sec = int(now - self.start)
        if self._cur is None or sec != self._cur_sec:
            if self._cur is not None:
                self.windows.append(self._cur)
            self._cur_sec = sec
            self._cur = {"t": sec, "utc": time.time(), "bytes": 0, "pid_bytes": defaultdict(int), "null": 0,
                         "packets": 0, "cc_errors": 0, "cc_dups": 0, "pcr_disc": 0,
                         "pcr_back": 0, "pcr_jitter": 0, "pat_pmt_change": 0,
                         "vpts": [], "apts": []}
        return self._cur

    def _packet(self, p, now):
        w = self._second(now)
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pusi = bool(p[1] & 0x40)
        afc = (p[3] >> 4) & 3
        cc = p[3] & 0x0F
        has_payload = bool(afc & 1)
        self.bytes_total += TS_PACKET
        self.totals["packets"] += 1
        w["packets"] += 1
        w["bytes"] += TS_PACKET
        w["pid_bytes"][pid] += TS_PACKET
        self.pid_total[pid] += TS_PACKET
        self.pid_pk[pid] += 1
        if (p[3] >> 4) & 2:                    # adaptation field present: its length is stuffing or PCR
            self.pid_af[pid] += 1 + p[4]

        if pid == NULL_PID:
            w["null"] += 1
            return
        if p[1] & 0x80:
            self._tr_hit("transport_error")
        scrambled = bool((p[3] >> 6) & 3)
        if pid == 0 and scrambled:
            self._tr_hit("pat_error")                 # TR 101 290 1.3: PID 0 must not be scrambled
        if scrambled and not self.cat_seen and not self._scrambled_no_cat:
            self._scrambled_no_cat = True             # TR 101 290 2.6: scrambled packets with no CAT
            self._tr_hit("cat_error")
        self.tr_seen[pid] = now
        if now >= self.tr_next_check:
            self.tr_next_check = now + 0.1
            self._tr_timers(now)

        # adaptation field: discontinuity flag and PCR
        off = 4
        rai = False
        if afc & 2:
            alen = p[4]
            off = 5 + alen
            rai = alen >= 1 and bool(p[5] & 0x40)     # random_access_indicator: a keyframe starts here
            if alen >= 7 and p[5] & 0x10:
                b = p[6:12]
                base = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
                ext = ((b[4] & 1) << 8) | b[5]
                pcr = base * 300 + ext
                self._tr_pcr(pid, pcr, bool(p[5] & 0x80))
                self._pcr(pcr, now, w, discontinuity=bool(p[5] & 0x80))
            elif alen >= 1 and p[5] & 0x80:
                w["pcr_disc"] += 1
                self.totals["pcr_disc"] += 1
                self._event(now, "pcr_disc", f"discontinuity flag on PID {pid}")

        # continuity counter (not checked on null PID; duplicates are allowed)
        if has_payload and pid in self.last_cc:
            last = self.last_cc[pid]
            if cc == last:
                w["cc_dups"] += 1
                self.pid_dup[pid] += 1
            elif cc != (last + 1) & 0x0F:
                self._tr_hit("cc_error")
                if self.locked_at is not None:
                    w["cc_errors"] += 1
                    self.totals["cc_errors"] += 1
                    shown = w["cc_errors"]
                    tag = ""
                else:
                    shown = self.startup["cc_error"]
                    tag = " (start-up, not counted)"
                if shown <= 3:
                    self._event(now, "cc_error", f"PID 0x{pid:04X} expected {(last + 1) & 0x0F} got {cc}{tag}")
        if has_payload:
            self.last_cc[pid] = cc

        # presentation timestamps of video and audio access units (A/V offset)
        if pusi and has_payload and pid in self._av and off + 14 <= TS_PACKET:
            self._pes_pts(p, off, pid, w, rai, now)

        # PSI tables
        if pusi and has_payload and off < TS_PACKET:
            self._psi_checks(p, off, pid, pid == 0 or pid in self.pmt_pids or pid in (SDT_PID, CAT_PID))
            if pid == CAT_PID:
                self._cat(p, off, now)
            if pid == 0:
                self._pat(p, off, now, w)
            elif pid in self.pmt_pids:
                self._pmt(p, off, pid, now, w)
            elif pid == SDT_PID:
                self._sdt(p, off, now)
        self._lock_check(now)

    def _lock_check(self, now):
        """Locked once PAT, PMT and a first PCR have all been seen. If that has not happened after
        LOCK_DEADLINE_S, the held-back errors are counted anyway, so a stream that never locks cannot look clean."""
        if self.locked_at is not None:
            return
        stamp = time.strftime("%H:%M:%S UTC", time.gmtime())
        if self.pat_version is not None and self.pmts and self.last_pcr is not None:
            self._lock(now, stamp, "stream locked: PAT, PMT and PCR seen", count_held=False)
        elif now - self.start > self.LOCK_DEADLINE_S:
            self._lock(now, stamp, f"stream did not lock within {self.LOCK_DEADLINE_S:.0f} s (PAT, PMT or PCR "
                                   f"missing): start-up errors are now counted", count_held=True)

    def _lock(self, now, stamp, text, count_held):
        """Lock the stream. Held-back errors stay out of the totals after a normal lock; if the stream never
        locked, they are counted now."""
        self.locked_at = now
        self.locked_stamp = stamp
        if count_held:
            for key, n in self.startup.items():
                self.tr[key] += n
                self.tr_first.setdefault(key, self.startup_first.get(key, stamp))
        self._event(now, "locked", text)

    def _psi_checks(self, p, off, pid, check_crc):
        """TR 101 290 2.2 CRC error on a complete PSI section that starts in this packet. Sections that
        span packets are not checked here."""
        if not check_crc:
            return
        s = off + 1 + p[off]
        if s + 3 > TS_PACKET:
            return
        length = ((p[s + 1] & 0x0F) << 8) | p[s + 2]
        end = s + 3 + length
        if end > TS_PACKET or p[s] == 0xFF:
            return
        if crc32_mpeg(p[s:end]) != 0:
            self._tr_hit("crc_error")

    def _cat(self, p, off, now):
        """CAT (PID 0x0001): table_id 0x01 is the only table allowed there (TR 101 290 2.6)."""
        s = off + 1 + p[off]
        if s >= TS_PACKET or p[s] == 0xFF:
            return
        if p[s] == 0x01:
            self.cat_seen = True
            self._scrambled_no_cat = False
        else:
            self._tr_hit("cat_error")

    def _tr_pcr(self, pid, pcr, discontinuity):
        """Priority 2: PCR repetition, TR 101 290 2.3a. PCRs must repeat at least every 40 ms."""
        last = self.tr_pcr.get(pid)
        if last is not None and not discontinuity:
            gap = (pcr - last) / 27_000_000.0
            if gap >= 0 and self.locked_at is not None:    # intervals are measured once the stream has locked
                if self._in_connect_window():
                    # set-up time after joining: kept apart so it does not set the figures for the run
                    e = self.pcr_early.setdefault(pid, [0, 0.0])
                    e[0] += 1
                    e[1] = max(e[1], gap)
                else:
                    self._record_pcr_gap(pid, gap)
            if 0 <= gap > PCR_REPEAT_MAX_S:
                self._tr_hit("pcr_repetition")        # 2.3a: interval over 40 ms
            if gap > PCR_DISC_MAX_S:
                self._tr_hit("pcr_error")             # 2.3: a step over 100 ms without the indicator
            elif gap < 0:
                self._tr_hit("pcr_di_error")          # 2.3b: PCR went backwards without the indicator
        self.tr_pcr[pid] = pcr

    def _record_pcr_gap(self, pid, gap):
        g = self.pcr_gaps.get(pid)
        if g is None:
            g = self.pcr_gaps[pid] = [0, 0.0, 0.0, deque(maxlen=PCR_GAP_WINDOW)]
        g[0] += 1
        g[1] += gap
        g[2] = max(g[2], gap)
        g[3].append(gap)

    def _in_connect_window(self):
        return (self.first_data is not None and self._now is not None
                and self._now - self.first_data <= CONNECT_WINDOW_S)

    def pcr_interval_stats(self):
        """Spacing between consecutive PCRs on each PID, in milliseconds. The mean and max cover the whole
        run after the connection settles; the median covers the most recent PCR_GAP_WINDOW intervals.
        Intervals in the first CONNECT_WINDOW_S seconds are reported separately as early_*."""
        out = []
        for pid in sorted(set(self.pcr_gaps) | set(self.pcr_early)):
            count, total, peak, recent = self.pcr_gaps.get(pid, [0, 0.0, 0.0, deque()])
            early = self.pcr_early.get(pid)
            out.append({"pid": pid, "count": count,
                        "mean_ms": round(1000.0 * total / count, 2) if count else None,
                        "median_ms": round(1000.0 * statistics.median(recent), 2) if recent else None,
                        "max_ms": round(1000.0 * peak, 2) if count else None,
                        "early_count": early[0] if early else 0,
                        "early_max_ms": round(1000.0 * early[1], 2) if early else None})
        return out

    def _tr_timers(self, now):
        """Priority 1 and 3 timeouts. An episode counts once, when the timer first expires."""
        def expire(key, last, limit):
            if last is None:
                return
            if now - last > limit:
                if key not in self.tr_missing:
                    self.tr_missing.add(key)
                    self._tr_hit(key[0])
            else:
                self.tr_missing.discard(key)

        if self.pat_version is not None:
            expire(("pat_error",), self.tr_seen.get(0), 0.5)
            for prog, pmt_pid in self.pat.items():
                expire(("pmt_error", pmt_pid), self.tr_pmt_seen.get(pmt_pid), 0.5)
            for info in self.pmts.values():
                for es_pid in info["streams"]:
                    expire(("pid_error", es_pid), self.tr_seen.get(es_pid), 0.5)
        expire(("sdt_error",), self.tr_seen.get(SDT_PID), 2.0)
        # TR 101 290 3.1, 3.6, 3.8: these tables apply to DVB streams. A check runs only once its PID has
        # been seen, so a stream that does not carry NIT, EIT or TDT is not marked down for them.
        expire(("nit_error",), self.tr_seen.get(0x0010), 10.0)
        expire(("eit_error",), self.tr_seen.get(0x0012), 2.0)
        expire(("tdt_error",), self.tr_seen.get(0x0014), 30.0)
        known = set(PSI_PIDS) | {NULL_PID}
        known |= self.pmt_pids
        for info in self.pmts.values():
            known |= set(info["streams"]) | {info["pcr_pid"]}
        for pid in self.tr_seen:
            if pid not in known and pid not in self.tr_unref and self.pat_version is not None:
                self.tr_unref.add(pid)
                self._tr_hit("unreferenced_pid")

    def _tr_hit(self, key):
        """Count one episode of a TR 101 290 check and note when it happened (UTC). Before the stream locks,
        the episode is kept aside instead (see _lock_check)."""
        stamp = time.strftime("%H:%M:%S UTC", time.gmtime())
        if self.first_data is not None and self._now is not None:
            if self._now - self.first_data <= CONNECT_WINDOW_S:
                self.hits_early += 1
                self.tr_early[key] += 1
            else:
                self.hits_late += 1
                self.tr_late[key] += 1
        if self.locked_at is None:
            self.startup[key] += 1
            self.startup_first.setdefault(key, stamp)
            # shown in Events, tagged, so nothing from the join goes unseen; the first few only, and cc_error
            # has its own event already
            if key != "cc_error" and self.startup[key] <= 3:
                self._event(None, "startup", f"{key} (start-up, not counted)")
            return
        self.tr[key] += 1
        self.tr_first.setdefault(key, stamp)
        self.tr_last[key].append(stamp)

    def connect_window(self):
        """How many TR 101 290 episodes fell in the first CONNECT_WINDOW_S seconds of data, and how many after."""
        return {"early": self.hits_early, "late": self.hits_late, "window_s": CONNECT_WINDOW_S}

    def tr101290(self):
        """The TR 101 290 indicators, grouped by priority. A count of None means the indicator is not
        measured by this tool; the report says so rather than showing a zero."""
        spec = [
            (1, "sync_byte", "1.1–1.2 Sync loss / sync byte error", "Packets are not starting where the 188-byte framing says. Hysteresis (5 bad then 2 good packets) is not applied: each loss of alignment counts once."),
            (1, "pat_error", "1.3 PAT error (timeout 500 ms, table_id, scrambling)", "The Program Association Table should arrive at least every 500 ms on PID 0, with table_id 0x00 and no scrambling."),
            (1, "cc_error", "1.4 Continuity count error", "A packet is missing, repeated more than twice, or out of order on a PID. Usually loss on the link or a sender re-muxing problem."),
            (1, "pmt_error", "1.5 PMT error (timeout 500 ms, table_id)", "The Program Map Table should arrive at least every 500 ms on each PMT PID, with table_id 0x02. Missing it means the decoder cannot find the video and audio PIDs."),
            (1, "pid_error", "1.6 PID error (timeout 500 ms)", "A stream PID has gone quiet for over 500 ms. Check the sender for a stall or a dropped stream."),
            (2, "transport_error", "2.1 Transport error indicator set", "A packet arrived with its transport error flag set, so something upstream flagged it as corrupt."),
            (2, "crc_error", "2.2 CRC error (PAT, PMT, SDT, CAT)", "A complete table section failed its CRC-32, so its contents cannot be trusted. Sections split across packets are not checked."),
            (2, "pcr_error", "2.3 PCR discontinuity over 100 ms without indicator", "The clock reference jumped forward by more than 100 ms without the discontinuity flag set. The decoder's clock is wrong after that point, so this is usually a sender-side clock problem."),
            (2, "pcr_repetition", "2.3a PCR interval over 40 ms", "The clock reference should repeat at least every 40 ms (TR 101 290). Longer gaps make the decoder's clock drift."),
            (2, "pcr_di_error", "2.3b PCR went backwards without indicator", "The clock reference stepped backwards without the discontinuity flag set."),
            (2, None, "2.4 PCR accuracy within ±500 ns", "Not measured: this needs a precise clock at the receiver, which network jitter hides."),
            (2, "pts_interval", "2.5 PTS interval over 700 ms", "Presentation timestamps should repeat at least every 700 ms on each stream. A longer gap usually means a stall at the sender or a network gap."),
            (2, "cat_error", "2.6 CAT error", "Scrambled packets are present but no CAT (table_id 0x01 on PID 0x0001) has been seen, or PID 0x0001 carries a non-CAT table."),
            (3, "nit_error", "3.1 NIT arrives at least every 10 s (only if carried)", "Checks that the Network Information Table keeps arriving. It does not read the table's contents. Shows 'not carried' if the stream has no NIT."),
            (3, None, "3.2 SI table repetition", "Not measured: needs the full SI repetition rules for the broadcast profile."),
            (3, None, "3.3 Buffer overflow / underflow", "Not measured: needs a decoder buffer model (TB, MB and EB)."),
            (3, "unreferenced_pid", "3.4 Unreferenced PID", "A PID carries data that no PMT or CAT points to. Harmless if intended, such as private data, but worth checking."),
            (3, "sdt_error", "3.5 SDT timeout over 2 s", "The Service Description Table should arrive every 2 s. Missing it affects only the service name, not the picture."),
            (3, "eit_error", "3.6 EIT arrives at least every 2 s (only if carried)", "Checks that the Event Information Table (programme guide) keeps arriving. It does not read the guide's contents. Shows 'not carried' if the stream has no EIT."),
            (3, None, "3.7 RST error", "Not measured: running status tables are not parsed."),
            (3, "tdt_error", "3.8 TDT arrives at least every 30 s (only if carried)", "Checks that the Time and Date Table keeps arriving. It does not read the table's contents. Shows 'not carried' if the stream has no TDT."),
            (3, None, "3.9 Empty buffer", "Not measured: needs a decoder buffer model."),
            (3, None, "3.10 Data delay", "Not measured: needs the delay model of the broadcast profile."),
        ]
        presence = {"nit_error": 0x0010, "eit_error": 0x0012, "tdt_error": 0x0014}
        out = []
        for pr, key, name, hint in spec:
            item = {"priority": pr, "name": name, "hint": hint,
                    "count": None if key is None else self.tr[key],
                    "first": None if key is None else self.tr_first.get(key),
                    "last": None if key is None or not self.tr_last[key] else self.tr_last[key][-1]}
            if key is not None:
                item["early"] = self.tr_early[key]    # episodes in the first CONNECT_WINDOW_S seconds
                item["late"] = self.tr_late[key]      # episodes after that
            if key in presence:
                item["carried"] = presence[key] in self.tr_seen   # False: the stream never sent this table
            out.append(item)
        return out

    def _pes_pts(self, p, off, pid, w, rai=False, now=None):
        """PES header: start code, stream id, PTS flags, then the 33-bit PTS (90 kHz)."""
        if p[off] != 0 or p[off + 1] != 0 or p[off + 2] != 1:
            return
        if (p[off + 7] >> 6) & 3 < 2:          # no PTS in this PES packet
            return
        b = p[off + 9:off + 14]
        pts = (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | (((b[2] >> 1) & 0x7F) << 15) \
            | (b[3] << 7) | ((b[4] >> 1) & 0x7F)
        w["vpts" if self._av[pid] == "video" else "apts"].append(pts)
        if rai and self._av.get(pid) == "video":
            self.ri_pts.append(pts)
            self.ri_total += 1
        # ETSI TR 101 290: a PTS must appear in the PES headers at least every 700 ms. Measured as the
        # arrival time between PES headers that carry a PTS. Comparing PTS values is wrong: with B-frames
        # the PTS jumps back and forth, and the wrap-around arithmetic turns a backward step into a huge gap.
        if now is not None:
            last = self.tr_pts.get(pid)
            if last is not None and now - last > 0.7:
                self._tr_hit("pts_interval")
            self.tr_pts[pid] = now

    def _av_offset_ms(self, window):
        """Median of (video PTS - nearest audio PTS) over one second, in ms.
        Positive: video is stamped later than audio, so audio leads.
        Resolution is about half an audio frame (about 11 ms for AAC at 48 kHz)."""
        vpts, apts = window["vpts"], sorted(window["apts"])
        if not vpts or not apts:
            return None
        diffs = []
        for v in vpts:
            i = bisect.bisect_left(apts, v)
            cands = [apts[j] for j in (i - 1, i) if 0 <= j < len(apts)]
            a = min(cands, key=lambda x: abs(x - v))
            d = (v - a + (1 << 32)) % (1 << 33) - (1 << 32)   # wrap-safe for the 33-bit clock
            diffs.append(d / 90.0)
        return round(statistics.median(diffs), 1)

    def _pcr(self, pcr, now, w, discontinuity):
        if self.last_pcr is not None:
            last_pcr, last_arrival = self.last_pcr
            pcr_delta = (pcr - last_pcr) / 27_000_000.0
            wall_delta = now - last_arrival
            if pcr < last_pcr and not discontinuity:
                w["pcr_back"] += 1
                self.totals["pcr_back"] += 1
                self._event(now, "pcr_back", "PCR went backwards")
            elif abs(pcr_delta - wall_delta) > PCR_JUMP_S and pcr_delta > 0 and wall_delta > 0:
                # PCR advances at the sender's clock, so gaps against wall time mean delivery was bursty
                w["pcr_jitter"] += 1
        if discontinuity:
            w["pcr_disc"] += 1
            self.totals["pcr_disc"] += 1
            self._event(now, "pcr_disc", "discontinuity indicator set")
        self.last_pcr = (pcr, now)

    def _pat(self, p, off, now, w):
        ptr = p[off]
        s = off + 1 + ptr
        if s < TS_PACKET and p[s] != 0x00 and p[s] != 0xFF:
            self._tr_hit("pat_error")                 # TR 101 290 1.3: PID 0 must carry table_id 0x00
        if s + 8 > TS_PACKET or p[s] != 0x00:
            return
        length = ((p[s + 1] & 0x0F) << 8) | p[s + 2]
        version = (p[s + 5] >> 1) & 0x1F
        programs = {}
        i = s + 8
        end = min(TS_PACKET, s + 3 + length - 4)
        while i + 4 <= end:
            prog = (p[i] << 8) | p[i + 1]
            pmt_pid = ((p[i + 2] & 0x1F) << 8) | p[i + 3]
            if prog != 0:
                programs[prog] = pmt_pid
            i += 4
        if self.pat_version is not None and version != self.pat_version:
            w["pat_pmt_change"] += 1
            self.totals["table_changes"] += 1
            self._event(now, "pat_change", f"PAT version {self.pat_version} -> {version}")
        self.pat_version = version
        self.pat = programs
        self.pmt_pids = set(programs.values())
        for pmt_pid in self.pmt_pids:
            self.tr_pmt_seen.setdefault(pmt_pid, now)   # a listed PMT that never arrives times out

    def _pmt(self, p, off, pid, now, w):
        ptr = p[off]
        s = off + 1 + ptr
        if s < TS_PACKET and p[s] not in (0x02, 0xFF):
            self._tr_hit("pmt_error")                 # TR 101 290 1.5: the PMT PID must carry table_id 0x02
        if s + 12 > TS_PACKET or p[s] != 0x02:
            return
        length = ((p[s + 1] & 0x0F) << 8) | p[s + 2]
        prog = (p[s + 3] << 8) | p[s + 4]
        version = (p[s + 5] >> 1) & 0x1F
        pcr_pid = ((p[s + 8] & 0x1F) << 8) | p[s + 9]
        info_len = ((p[s + 10] & 0x0F) << 8) | p[s + 11]
        streams = {}
        es_desc, es_raw = {}, {}
        i = s + 12 + info_len
        end = min(TS_PACKET, s + 3 + length - 4)
        while i + 5 <= end:
            stype = p[i]
            es_pid = ((p[i + 1] & 0x1F) << 8) | p[i + 2]
            es_info = ((p[i + 3] & 0x0F) << 8) | p[i + 4]
            streams[es_pid] = stype
            raw = p[i + 5:min(i + 5 + es_info, TS_PACKET)]
            es_desc[es_pid] = describe_descriptors(raw)
            es_raw[es_pid] = [{"tag": f"0x{tag:02X}", "len": len(body), "hex": body[:64].hex(" "),
                               "name": describe_one_descriptor(tag, body)}
                              for tag, body in split_descriptors(raw)]
            i += 5 + es_info
        previous = self.pmts.get(prog)
        if previous is not None and previous["version"] != version:
            w["pat_pmt_change"] += 1
            self.totals["table_changes"] += 1
            old = previous["streams"]
            detail = (f"PMT version {previous['version']} -> {version}; streams "
                      + ", ".join(f"0x{k:04X} {codec_name(v)}" for k, v in sorted(streams.items())))
            if old != streams:
                detail += "  (stream layout changed)"
            self._event(now, "pmt_change", detail)
        self.pmts[prog] = {"version": version, "pcr_pid": pcr_pid, "streams": streams,
                           "descriptors": es_desc, "raw": es_raw}
        self.tr_pmt_seen[pid] = now
        for es_pid in streams:
            self.tr_seen.setdefault(es_pid, now)   # a listed PID that never arrives times out from here
        self._av = {pid: ("video" if is_video(t) else "audio") for info in self.pmts.values()
                    for pid, t in info["streams"].items() if is_video(t) or is_audio(t)}

    def _sdt(self, p, off, now):
        """Service description table (PID 0x0011): the provider and service name the muxer wrote.
        Most encoders and muxers fill these in, and FFmpeg writes its own name by default."""
        ptr = p[off]
        s = off + 1 + ptr
        if s + 11 > TS_PACKET or p[s] != 0x42:          # table_id 0x42 = actual transport stream
            return
        length = ((p[s + 1] & 0x0F) << 8) | p[s + 2]
        end = min(TS_PACKET, s + 3 + length - 4)         # the last 4 bytes are the CRC
        i = s + 11                                       # past the section header and original_network_id
        found = {}
        while i + 5 <= end:
            service_id = (p[i] << 8) | p[i + 1]
            dlen = ((p[i + 3] & 0x0F) << 8) | p[i + 4]
            j, dend = i + 5, min(end, i + 5 + dlen)
            while j + 2 <= dend:
                tag, n = p[j], p[j + 1]
                body = p[j + 2:j + 2 + n]
                if tag == 0x48 and len(body) >= 3:       # service descriptor
                    prov_len = body[1]
                    provider = _text(body[2:2 + prov_len])
                    rest = body[2 + prov_len:]
                    name = _text(rest[1:1 + rest[0]]) if rest else ""
                    found.setdefault(service_id, {"provider": provider, "service": name})
                j += 2 + n
            i += 5 + dlen
        if not found:
            return
        sid, info = sorted(found.items())[0]
        if self.sdt.get("service") != info["service"] or self.sdt.get("provider") != info["provider"]:
            if self.sdt:
                self._event(now, "sdt_change", f"service description changed to "
                            f"{info['provider'] or '?'} / {info['service'] or '?'}")
            self.sdt = {"service_id": sid, **info}

    def _event(self, now, kind, detail):
        # stored with a UTC time so the report can list it beside the monitor's own events
        self.events.append((time.strftime("%H:%M:%S UTC", time.gmtime()), kind, detail, None))
        if len(self.events) > 500:
            del self.events[:100]

    # ---------------------------------------------------------------- reporting
    def finish_window(self):
        if self._cur is not None:
            self.windows.append(self._cur)
            self._cur = None

    def streams(self):
        """Codec list for the first program, one entry per elementary stream."""
        out = []
        for prog, info in sorted(self.pmts.items()):
            for pid, stype in sorted(info["streams"].items()):
                kind = "video" if is_video(stype) else "audio" if is_audio(stype) else "data"
                out.append({"program": prog, "pid": pid, "type": stype, "codec": codec_name(stype), "kind": kind})
        return out

    def pid_table(self):
        """Every PID seen this run: what it is (from the PMT and its descriptors where possible), its
        average rate and its share of the stream. A PID with no PMT entry is shown as unreferenced."""
        dur = 1.0
        now = getattr(self, "_now", None)
        if self.first_data is not None and now:
            dur = max(1.0, now - self.first_data)
        total = sum(self.pid_total.values()) or 1
        es, pcr_pids, owner = {}, set(), {}
        for prog, info in self.pmts.items():
            pcr_pids.add(info["pcr_pid"])
            for pid, t in info["streams"].items():
                es[pid] = (t, info.get("descriptors", {}).get(pid, []), info.get("raw", {}).get(pid, []))
                owner[pid] = prog
        recent_wins = list(self.windows)[-RECENT_S:] + ([self._cur] if self._cur else [])
        rsec = max(1, len(recent_wins))
        rbytes = defaultdict(int)
        for w in recent_wins:
            for rp, rb in w["pid_bytes"].items():
                rbytes[rp] += rb
        rows = []
        for pid in sorted(self.pid_total):
            b = self.pid_total[pid]
            af = self.pid_af.get(pid, 0)
            raw, stype, prog = [], None, owner.get(pid)
            if pid == SDT_PID and self.sdt:
                role = "Table"
                desc = (f"SDT · service \"{self.sdt.get('service') or '?'}\", provider "
                        f"\"{self.sdt.get('provider') or '?'}\", service ID {self.sdt.get('service_id')}")
            elif pid in PSI_NAMES:
                role, desc = "Table", PSI_NAMES[pid]
            elif pid in self.pmt_pids:
                role, desc = "Table", "PMT (lists each programme's streams)"
            elif pid in es:
                t, notes, raw = es[pid]
                stype = t
                kind = "Video" if is_video(t) else "Audio" if is_audio(t) else "Data"
                role = f"{kind} · {codec_name(t)}"
                desc = "; ".join(notes)
            elif pid in pcr_pids:
                role, desc = "PCR clock", "carries the clock reference only"
            else:
                role, desc = "Unreferenced", "no PMT entry points here"
            rows.append({"pid": pid, "role": role, "desc": desc, "stream_type": stype, "program": prog,
                         "pcr": pid in pcr_pids, "descriptors": raw,
                         "stuffing_pct": round(100.0 * af / b, 2) if b else 0.0,
                         "dup_pct": round(100.0 * self.pid_dup.get(pid, 0) / self.pid_pk[pid], 2)
                         if self.pid_pk.get(pid) else 0.0,
                         "kbps": round(rbytes.get(pid, 0) * 8 / rsec / 1000, 1),
                         "kbps_run": round(b * 8 / dur / 1000, 1),
                         "share": round(100.0 * b / total, 2)})
        return rows

    def summary_row(self, window):
        """Flatten one second of evidence into a row for display and export."""
        mbps = lambda b: b * 8 / 1e6
        row = {
            "t": window["t"],
            "mux_mbps": round(mbps(window["bytes"]), 3),
            "null_pct": round(100.0 * window["null"] / window["packets"], 2) if window["packets"] else 0.0,
            "cc_errors": window["cc_errors"],
            "pcr_disc": window["pcr_disc"],
            "pcr_back": window["pcr_back"],
            "table_changes": window["pat_pmt_change"],
            "av_offset_ms": self._av_offset_ms(window),
        }
        kinds = {s["pid"]: s for s in self.streams()}
        for pid, kind in (("video", "video"), ("audio", "audio")):
            pids = [p for p, s in kinds.items() if s["kind"] == kind]
            row[f"{pid}_mbps"] = round(mbps(sum(window["pid_bytes"].get(p, 0) for p in pids)), 3)
        return row


# ------------------------------------------------------------------ attribution

def attribute(windows, srt_by_t=None, min_mux_drop=0.3):
    """
    Suspect where each anomaly came from. windows: list of per-second dicts from
    TSAnalyzer (fields: t, bytes, null, packets, cc_errors, pcr_back, pcr_disc,
    pat_pmt_change). srt_by_t: optional {t: {"lost", "dropped", "reconnect", "rtt"}}
    from the SRT statistics for the same second.

    Returns findings: {t, owner, confidence, what, evidence}.
    owner is one of: sender, network, receiver, link, unknown.
    """
    srt_by_t = srt_by_t or {}
    findings = []
    mux_hist = []
    null_prev = None

    last = windows[-1] if windows else None
    for w in windows:
        t = w["t"]
        s = srt_by_t.get(t, {})
        partial = w is last  # the newest second may still be filling: rate rules skip it
        srt_lost = s.get("lost", 0)
        srt_drop = s.get("dropped", 0)
        srt_reconnect = s.get("reconnect", False)
        mux = w["bytes"] * 8 / 1e6
        null_pct = 100.0 * w["null"] / w["packets"] if w["packets"] else 0.0

        def add(owner, confidence, what, evidence):
            findings.append({"t": t, "owner": owner, "confidence": confidence,
                             "what": what, "evidence": evidence})

        # 1. continuity errors: where did the packets go missing?
        if w["cc_errors"]:
            if srt_lost > 0:
                add("network", "likely", "continuity errors",
                    f"{w['cc_errors']} CC error(s) and {srt_lost} SRT packet(s) lost in this second")
            elif srt_drop > 0:
                add("receiver", "likely", "continuity errors",
                    f"{w['cc_errors']} CC error(s) and {srt_drop} packet(s) dropped on the receiver")
            else:
                add("sender", "likely", "continuity errors",
                    f"{w['cc_errors']} CC error(s) with no SRT loss: packets arrived clean but the counters were already broken")

        # 2. table changes: did the muxer restart, or did the link?
        if w["pat_pmt_change"]:
            if srt_reconnect:
                add("link", "likely", "stream tables changed after a link reconnect",
                    "PAT/PMT changed in the same second the SRT connection was re-established")
            else:
                add("sender", "likely", "muxer restarted its program tables",
                    "PAT/PMT version changed with no SRT reconnect")

        # 3. PCR problems
        if w["pcr_back"]:
            add("sender", "likely", "PCR went backwards",
                "the sender's clock reference jumped back; a restart or splice upstream")
        if w["pcr_disc"] and not srt_lost:
            add("sender", "possible", "PCR discontinuity flagged",
                "the sender marked a discontinuity with no SRT loss to explain it")

        # 4. mux rate collapse (complete seconds only)
        if len(mux_hist) >= 5 and not partial:
            typical = statistics.median(mux_hist[-10:])
            if typical > 0 and mux < typical * (1 - min_mux_drop):
                if srt_lost or srt_reconnect:
                    add("network", "possible", "mux rate fell",
                        f"{mux:.2f} Mbps vs typical {typical:.2f}; SRT loss or reconnect in the same second")
                else:
                    add("sender", "possible", "mux rate fell",
                        f"{mux:.2f} Mbps vs typical {typical:.2f} with a clean link: the input dropped")
        mux_hist.append(mux)

        # 5. null packet share jump (muxer padding changed)
        if null_prev is not None and not partial and abs(null_pct - null_prev) > 15 and not srt_lost:
            add("sender", "possible", "null packet share changed",
                f"null packets went from {null_prev:.0f}% to {null_pct:.0f}% with a clean link")
        if not partial:
            null_prev = null_pct

    # merge repeats of the same finding within 2 seconds
    utc_by_t = {w["t"]: w.get("utc") for w in windows}     # stream second -> wall-clock start (epoch)
    merged = []
    for f in findings:
        if merged and merged[-1]["what"] == f["what"] and merged[-1]["owner"] == f["owner"] \
                and f["t"] - merged[-1]["t_end"] <= 2:
            merged[-1]["t_end"] = f["t"]
            merged[-1]["count"] += 1
            merged[-1]["utc_end"] = utc_by_t.get(f["t"]) or merged[-1].get("utc_end")
        else:
            merged.append({**f, "t_end": f["t"], "count": 1,
                           "utc": utc_by_t.get(f["t"]), "utc_end": utc_by_t.get(f["t"])})
    return merged


def owner_label(owner, mode=None):
    """Who a finding points at, worded for this run. The remote peer is the caller when we
    listen, and the listener when we call out. 'receiver' is this machine."""
    if owner == "sender":
        remote = {"listener": "caller", "caller": "listener"}.get(mode, "sender")
        return f"the remote {remote} side"
    if owner == "receiver":
        return "our side (receiver)"
    if owner in ("network", "link"):
        return f"the {owner}"
    return "an unknown side"


def verdict_for(findings, mode=None):
    """Overall read: which side is most often suspected."""
    if not findings:
        return "no payload anomalies detected", {}
    tally = defaultdict(int)
    for f in findings:
        tally[f["owner"]] += f.get("count", 1)
    worst = max(tally, key=tally.get)
    return f"most anomalies suspected on {owner_label(worst, mode)}", dict(tally)


# ------------------------------------------------------------------ codec detail

RECENT_S = 30                 # seconds in the rolling rate shown on the dashboard
CONNECT_WINDOW_S = 15.0      # errors within this many seconds of the first data are reported as connection set-up

# ffprobe's field_order codes, written out in plain words
FIELD_ORDER_TEXT = {
    "progressive": "progressive",
    "tt": "interlaced, top field first",
    "bb": "interlaced, bottom field first",
    "tb": "interlaced, top field coded first",
    "bt": "interlaced, bottom field coded first",
    "unknown": "unknown",
}


def field_order_text(code):
    return FIELD_ORDER_TEXT.get(code, f"interlaced ({code})")


PROBE_FIELDS = ("id,codec_type,codec_name,profile,level,width,height,pix_fmt,display_aspect_ratio,"
                "avg_frame_rate,r_frame_rate,field_order,has_b_frames,color_primaries,color_transfer,"
                "color_space,color_range,sample_rate,channels,channel_layout,bit_rate")

PRIMARIES_NAME = {"bt709": "BT.709", "bt2020": "BT.2020", "smpte170m": "SMPTE 170M (SD)",
                  "bt470bg": "BT.601 (PAL)", "smpte432": "Display P3"}
TRANSFER_NAME = {"bt709": "BT.709 (SDR)", "smpte2084": "PQ (HDR10)", "arib-std-b67": "HLG (HDR)",
                 "smpte170m": "BT.601 (SDR)", "iec61966-2-1": "sRGB"}
MATRIX_NAME = {"bt709": "BT.709", "bt2020nc": "BT.2020 non-constant", "bt2020c": "BT.2020 constant",
               "smpte170m": "BT.601", "bt470bg": "BT.601 (PAL)", "gbr": "RGB"}
RANGE_NAME = {"tv": "limited (TV)", "pc": "full (PC)"}
CODEC_NAME = {"h264": "H.264/AVC", "hevc": "HEVC/H.265", "aac": "AAC", "ac3": "AC-3", "eac3": "E-AC-3"}


def _fps(text):
    try:
        n, d = (text or "").split("/")
        f = float(n) / float(d) if float(d) else None
        return f if f is not None and 0 < f <= 120 else None   # 90000/1 is the TS clock, not a rate
    except (ValueError, ZeroDivisionError):
        return None


def _rate_text(x):
    """A rate without trailing zeros: 25, 29.97, 59.94."""
    return ("%.2f" % x).rstrip("0").rstrip(".")


def format_name(height, fps, kind, interlaced_sig, field_order):
    """Standard broadcast name for the video format, e.g. 1080i50, 720p25, 1080PsF25 or 1080i59.94.
    For interlaced video the number is the field rate (two fields per frame), as broadcasters write it.
    Returns '' when the scan type is not known yet."""
    if not height or not fps:
        return ""
    if kind and kind[0] == "interlaced" or (not kind and interlaced_sig):
        return f"{height}i{_rate_text(fps * 2)}"
    if kind and kind[0] == "psf":
        return f"{height}PsF{_rate_text(fps)}"
    if kind and kind[0] == "progressive" or (not kind and field_order == "progressive"):
        return f"{height}p{_rate_text(fps)}"
    return ""


def stream_fields(s):
    """Every detail we can show for one stream, as (label, value) pairs in plain words."""
    rows = []
    codec = CODEC_NAME.get(s.get("codec_name", ""), (s.get("codec_name") or "unknown").upper())
    if s.get("codec_type") == "audio":
        rows.append(("Codec", " ".join(x for x in (codec, s.get("profile", "")) if x)))
        if s.get("sample_rate"):
            rows.append(("Sample rate", f"{int(s['sample_rate'])} Hz"))
        if s.get("channels"):
            rows.append(("Channels", f"{s['channels']} ({s.get('channel_layout') or 'layout not signalled'})"))
        if s.get("bit_rate"):
            rows.append(("Declared bit rate", f"{int(s['bit_rate']) / 1000:.0f} kbps"))
        if s.get("measured_kbps"):
            rows.append(("Measured bit rate", f"{s['measured_kbps']:.0f} kbps (from the packets)"))
        return rows
    rows.append(("Codec", " ".join(x for x in (codec, s.get("profile", "")) if x)))
    lvl = s.get("level")
    if isinstance(lvl, int) and lvl > 0:
        rows.append(("Level", f"{lvl / (30 if s.get('codec_name') == 'hevc' else 10):.1f}"))
    if s.get("width"):
        rows.append(("Resolution", f"{s['width']}x{s['height']}"))
    dar = s.get("display_aspect_ratio")
    if dar and dar not in ("0:1", "N/A"):
        rows.append(("Display aspect", dar))
    fps = _fps(s.get("avg_frame_rate")) or _fps(s.get("r_frame_rate"))
    kind = scan_kind(s.get("scan"), s.get("field_order", ""))
    interlaced_sig = s.get("field_order") in ("tt", "bb", "tb", "bt")   # the declared scan type, even without a frame scan
    name = format_name(s.get("height"), fps, kind, interlaced_sig, s.get("field_order"))
    if name:
        rows.append(("Format", name))
    if fps:
        if (kind and kind[0] == "interlaced") or (not kind and interlaced_sig):
            rows.append(("Frame rate", f"{fps:.2f} frames/s, {fps * 2:.0f} fields/s"))
        elif kind and kind[0] == "psf":
            rows.append(("Frame rate", f"{fps:.2f} frames/s, sent as {fps * 2:.0f} fields/s"))
        else:
            rows.append(("Frame rate", f"{fps:.2f} fps"))
    if kind:
        if kind[0] == "interlaced":
            rows.append(("Scan", f"Interlaced, {kind[1]}" if kind[1] else "Interlaced"))
        elif kind[0] == "psf":
            rows.append(("Scan", "Progressive frames in an interlaced stream (PsF)"))
        elif kind[0] == "progressive":
            rows.append(("Scan", "Progressive"))
        else:
            rows.append(("Scan", "Mixed: some frames interlaced, some progressive"))
    elif s.get("field_order"):
        rows.append(("Scan", field_order_text(s["field_order"])))
    pix = s.get("pix_fmt", "") or ""
    chroma = ("4:2:2" if "422" in pix else "4:2:0" if "420" in pix else "4:4:4" if "444" in pix
              else "4:0:0" if "gray" in pix else "not signalled")
    rows.append(("Chroma subsampling", chroma))
    m = re.search(r"p(\d+)", pix)
    depth = m.group(1) if m else ("8" if pix.startswith("yuv") else "")
    rows.append(("Bit depth", f"{depth}-bit" if depth else "not signalled"))
    rows.append(("Pixel format", pix or "not signalled"))
    p, t, mx = s.get("color_primaries"), s.get("color_transfer"), s.get("color_space")
    rows.append(("Primaries", PRIMARIES_NAME.get(p, p) if p else "not signalled"))
    rows.append(("Transfer", TRANSFER_NAME.get(t, t) if t else "not signalled"))
    rows.append(("Matrix", MATRIX_NAME.get(mx, mx) if mx else "not signalled"))
    rows.append(("Range", RANGE_NAME.get(s.get("color_range"), s.get("color_range") or "not signalled")))
    if s.get("has_b_frames") is not None:
        rows.append(("B-frames", str(s["has_b_frames"])))
    if s.get("bit_rate"):
        rows.append(("Bit rate", f"{int(s['bit_rate']) / 1e6:.2f} Mbps"))
    return rows


def probe_stream(data, timeout=20):
    """Ask ffprobe for profile, chroma and colour of each stream in a TS snapshot.
    Returns {pid: detail} or None if ffprobe is missing or the snapshot could not be read."""
    import json
    import shutil
    import subprocess
    if not shutil.which("ffprobe"):
        return None
    cmd = ["ffprobe", "-v", "error", "-probesize", "16000000", "-analyzeduration", "8000000",
           "-show_entries", "stream=" + PROBE_FIELDS,
           "-of", "json", "-i", "pipe:0"]
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
        info = json.loads(p.stdout or b"{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    out = {}
    for s in info.get("streams", []):
        try:
            pid = int(str(s.get("id", "0")), 16)
        except ValueError:
            continue
        out[pid] = s
    vid = next((s for s in out.values() if s.get("codec_type") == "video"), None)
    # only scan frames when the field order is known: a snapshot that starts mid-stream reads frames
    # with no interlace flags, which would wrongly look progressive
    if vid is not None and vid.get("field_order") not in (None, "", "unknown"):
        vid["scan"] = frame_scan(data, timeout=timeout)
    aud = next((s for s in out.values() if s.get("codec_type") == "audio"), None)
    if aud is not None:
        aud["measured_kbps"] = audio_packet_rate(data, timeout=timeout)
    return out


def audio_packet_rate(data, timeout=20):
    """Average bit rate of the first audio stream, measured from its packet sizes and timestamps in a
    TS snapshot. This is what the stream really carries, unlike the rate a header declares.
    Returns kbps, or None if fewer than two timestamped packets were read."""
    import shutil
    import subprocess
    if not shutil.which("ffprobe"):
        return None
    cmd = ["ffprobe", "-v", "error", "-probesize", "16000000", "-analyzeduration", "8000000",
           "-select_streams", "a:0", "-show_entries", "packet=size,pts_time",
           "-of", "csv=p=0", "-i", "pipe:0"]
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    pk = []
    for line in p.stdout.decode("utf-8", errors="replace").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and parts[1].isdigit():    # ffprobe prints pts_time first, then size
            try:
                pk.append((float(parts[0]), int(parts[1])))
            except ValueError:
                continue                      # no timestamp on this packet
    if len(pk) < 2:
        return None
    pk.sort()
    span = pk[-1][0] - pk[0][0]
    if span <= 0:
        return None
    # every packet but the last covers the time up to the next one
    return round(sum(size for _, size in pk[:-1]) * 8 / span / 1000, 1)


def gop_probe(data, timeout=30):
    """GOP length and frame pattern of the video in a TS snapshot, from frame types in decode order.
    Returns None if ffprobe is missing, else a dict (gop is None when fewer than two keyframes were seen)."""
    import shutil
    import subprocess
    if not shutil.which("ffprobe"):
        return None
    cmd = ["ffprobe", "-v", "error", "-probesize", "16000000", "-analyzeduration", "8000000",
           "-select_streams", "v:0", "-show_entries", "frame=pict_type,key_frame",
           "-of", "csv=p=0", "-i", "pipe:0"]
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    frames = []
    for line in p.stdout.decode("utf-8", errors="replace").splitlines():
        parts = [x.strip() for x in line.split(",")]   # ffprobe prints key_frame,pict_type,
        types = [x for x in parts if x in ("I", "P", "B")]
        if types:
            frames.append((types[0], "1" in parts[:1]))
    keys = [i for i, (_, k) in enumerate(frames) if k]
    if len(keys) < 2:
        return {"gop": None, "keys": len(keys), "frames": len(frames)}
    intervals = [b - a for a, b in zip(keys, keys[1:])]
    one = "".join(t for t, _ in frames[keys[0]:keys[1]])
    runs = re.findall("B+", one)
    counts = {t: sum(1 for x, _ in frames if x == t) for t in "IPB"}
    return {"gop": int(statistics.median(intervals)), "keys": len(keys), "frames": len(frames),
            "pattern": one, "bframes": max((len(r) for r in runs), default=0), "counts": counts}


def frame_scan(data, timeout=30):
    """Scan type counted frame by frame: is each frame coded as interlaced fields or as a progressive
    frame, and which field comes first. Returns None if ffprobe is missing or no frames were read."""
    import shutil
    import subprocess
    if not shutil.which("ffprobe"):
        return None
    cmd = ["ffprobe", "-v", "error", "-probesize", "16000000", "-analyzeduration", "8000000",
           "-select_streams", "v:0", "-show_entries", "frame=interlaced_frame,top_field_first,repeat_pict",
           "-of", "csv=p=0"]
    # data is either a snapshot of bytes (read from a pipe) or a path to a file
    if isinstance(data, str):
        cmd += ["-read_intervals", "%+15", "-i", data]      # a file: the first 15 s are enough to classify
    else:
        cmd += ["-i", "pipe:0"]
    try:
        p = subprocess.run(cmd, input=None if isinstance(data, str) else data, capture_output=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    counts = {"frames": 0, "interlaced": 0, "tff": 0, "bff": 0, "repeat": 0}
    for line in p.stdout.decode("utf-8", errors="replace").splitlines():
        parts = [x.strip() for x in line.split(",")] + ["", "", ""]
        il, tff, rep = parts[0], parts[1], parts[2]
        if il not in ("0", "1"):
            continue                       # N/A or blank: no frame flag to count
        counts["frames"] += 1
        if il == "1":
            counts["interlaced"] += 1
            if tff == "1":
                counts["tff"] += 1
            elif tff == "0":
                counts["bff"] += 1
        if rep not in ("", "N/A", "0"):
            counts["repeat"] += 1
    return counts if counts["frames"] else None


def scan_kind(scan, field_order):
    """Plain words for a frame_scan() result, judged against the stream's declared scan type.
    'psf' is progressive frames carried as field pairs in an interlaced stream."""
    if not scan or not scan.get("frames"):
        return None
    n = scan["frames"]
    il = scan["interlaced"] / n
    if il >= 0.95:
        order = scan["tff"] / scan["interlaced"] if scan["interlaced"] else 0.0
        if scan["tff"] + scan["bff"] == 0:
            side = ""
        elif order >= 0.95:
            side = "top field first"
        elif order <= 0.05:
            side = "bottom field first"
        else:
            side = "field order mixed"
        return ("interlaced", side)
    if il <= 0.05:
        if field_order in ("tt", "bb", "tb", "bt"):
            return ("psf", "")
        return ("progressive", "")
    return ("mixed", "")


def describe_source(path, timeout=120):
    """Format of a source file (any container ffmpeg reads, such as MP4 or MOV, or TS), in plain words.
    Returns (sections, error): sections is a list of (heading, [(label, value), ...]), one per stream.
    Used before a feed, so the operator knows exactly what is being sent."""
    import json
    import os
    import shutil
    import subprocess
    if not shutil.which("ffprobe"):
        return [], "ffprobe is not installed"
    if not os.path.isfile(path):
        return [], f"file not found: {path}"
    cmd = ["ffprobe", "-v", "error", "-probesize", "50000000", "-analyzeduration", "20000000",
           "-show_entries", "stream=" + PROBE_FIELDS + ",sample_aspect_ratio",
           "-of", "json", "-i", path]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        info = json.loads(p.stdout or b"{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return [], f"could not read the file: {e}"
    streams = info.get("streams", [])
    if not streams:
        return [], "no audio or video streams found in the file"
    sections = []
    for s in streams:
        if s.get("codec_type") == "video":
            s["scan"] = frame_scan(path, timeout=timeout)
            rows = [(k, v) for k, v in stream_fields(s) if k not in ("Chroma subsampling", "Pixel format",
                                                                   "Bit depth", "B-frames", "Matrix")]
            sar = s.get("sample_aspect_ratio")
            if sar and sar not in ("0:1", "N/A", "1:1"):
                rows.append(("Pixel aspect", sar))
            sections.append(("Video", rows))
        elif s.get("codec_type") == "audio":
            sections.append(("Audio", stream_fields(s)))
    return sections, ""


def source_report_lines(sections):
    """Plain-text lines for describe_source() output: a headline, then each stream's details."""
    lines = []
    video = next((rows for head, rows in sections if head == "Video"), None)
    if video:
        d = dict(video)
        head = [d.get("Format", ""), d.get("Display aspect", ""), d.get("Frame rate", ""),
                d.get("Scan", "")]
        lines.append("Source: " + ", ".join(x for x in head if x))
    for heading, rows in sections:
        lines.append(heading)
        for label, value in rows:
            lines.append(f"  {label:<16} {value}")
    return lines


def describe_detail(s):
    """Plain-language line for one ffprobe stream entry, e.g. 'High 4:2:2 10-bit, yuv422p10le, BT.2020'."""
    if not s:
        return "no detail"
    if not s.get("width"):  # audio: no chroma or colour to describe
        return s.get("profile") or s.get("codec_name", "")
    pix = s.get("pix_fmt", "") or ""
    chroma = ("4:2:2" if "422" in pix else "4:2:0" if "420" in pix else
              "4:4:4" if "444" in pix else "4:0:0" if "gray" in pix else pix or "unknown")
    depth = "10-bit" if "10" in pix or "p10" in pix else "8-bit" if pix else ""
    primaries = s.get("color_primaries") or "colour not signalled"
    prim_name = {"bt709": "BT.709", "bt2020": "BT.2020", "smpte170m": "SMPTE 170M (SD)",
                 "bt470bg": "BT.601 (PAL)"}.get(primaries, primaries)
    transfer = s.get("color_transfer") or ""
    tr_name = {"bt709": "SDR", "smpte2084": "PQ (HDR)", "arib-std-b67": "HLG (HDR)"}.get(transfer, transfer)
    fps = _fps(s.get("avg_frame_rate")) or _fps(s.get("r_frame_rate"))
    fmt = format_name(s.get("height"), fps, scan_kind(s.get("scan"), s.get("field_order", "")),
                      s.get("field_order") in ("tt", "bb", "tb", "bt"), s.get("field_order"))
    parts = [fmt, s.get("profile", "") or s.get("codec_name", ""), chroma + (f" {depth}" if depth else ""),
             f"{s['width']}x{s['height']}" if s.get("width") else "", prim_name]
    if tr_name:
        parts.append(tr_name)
    return ", ".join(p for p in parts if p)


# ------------------------------------------------------------------ shared payload feed

PROBE_BYTES = 16 * 1024 * 1024   # about 6 s at 20 Mbps: enough to hold a keyframe with its SPS


def keyframe_interval(pts):
    """Median gap between video keyframes in seconds, and the number of gaps, from their 90 kHz PTS.
    None until two keyframes are known. Works for any GOP length, because it spans the whole session."""
    if len(pts) < 2:
        return None
    gaps = [((b - a) & 0x1FFFFFFFF) / 90000.0 for a, b in zip(pts, pts[1:])]
    return statistics.median(gaps), len(gaps)


class PayloadFeed:
    """Reads the stream bytes from srt-live-transmit's stdout, analyses them, and keeps a
    rolling copy for ffprobe. Used by the monitor, the TUI and the report."""

    def __init__(self, first_probe_s=4.0, probe_every_s=15.0, mode=None):
        self.mode = mode
        self.ts = TSAnalyzer(start=time.monotonic())
        self.ring = bytearray()
        self.lock = threading.Lock()
        self.detail = {}
        self.fields = {}                       # pid -> [(label, value)], for the stream details panel
        self.probe_status = "waiting for enough stream to probe"
        self.changes = []
        self.first_probe_s = first_probe_s
        self.probe_every_s = probe_every_s
        self.proc = None

    def start(self, proc):
        self.proc = proc
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        threading.Thread(target=self._probe, args=(proc,), daemon=True).start()

    def _read(self, proc):
        fd = proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 188 * 200)
            except OSError:
                break
            if not chunk:
                break
            self.ts.feed(chunk, now=time.monotonic())
            with self.lock:
                self.ring.extend(chunk)
                if len(self.ring) > PROBE_BYTES:
                    del self.ring[:len(self.ring) - PROBE_BYTES]

    def _probe(self, proc):
        time.sleep(self.first_probe_s)
        while proc.poll() is None and self.proc is proc:
            with self.lock:
                snap = bytes(self.ring)
            if len(snap) >= 188 * 200:
                info = probe_stream(snap)
                if info is None:
                    self.probe_status = "ffprobe not available"
                    return
                if info:
                    desc = {pid: describe_detail(s) for pid, s in info.items()}
                    if self.detail and desc != self.detail:
                        self.changes.append((time.strftime("%H:%M:%S UTC", time.gmtime()), "codec detail changed"))
                    self.detail = desc
                    self.fields = {pid: stream_fields(s) for pid, s in info.items()}
                    self._add_gop(snap, info)
                    self.probe_status = f"probed {time.strftime('%H:%M:%S UTC', time.gmtime())}"
            time.sleep(self.probe_every_s)

    def _add_gop(self, snap, info):
        """GOP length and frame pattern, added to the video stream's details."""
        vid = next((pid for pid, st in info.items() if st.get("codec_type") == "video"), None)
        if vid is None:
            return
        fps = _fps(info[vid].get("avg_frame_rate")) or _fps(info[vid].get("r_frame_rate"))
        rows = self.fields.get(vid, [])
        ki = keyframe_interval(list(self.ts.ri_pts))
        if ki:
            rows.append(("Keyframe interval", f"every {ki[0]:.2f} s  (median of {ki[1]} gaps, "
                                              f"{self.ts.ri_total} keyframes seen)"))
        self.fields[vid] = rows
        g = gop_probe(snap)
        if not g or not g.get("gop"):
            return
        rows.append(("GOP size", f"{g['gop']} frames" + (f" ({g['gop'] / fps:.2f} s)" if fps else "")))
        pat = g["pattern"]
        shown = pat if len(pat) <= 24 else pat[:24] + "…"
        c = g["counts"]
        rows.append(("Frame pattern", f"{shown}  (decode order; I {c['I']}, P {c['P']}, B {c['B']})"))
        rows.append(("B-frames", f"up to {g['bframes']} in a row" if g["bframes"] else "none"))
        self.fields[vid] = rows

    def snapshot(self, srt_sec):
        return payload_snapshot(self.ts, self.detail, srt_sec, self.probe_status, mode=self.mode,
                                fields=self.fields)


def rate_mode(rates, null_pct=0.0):
    """Whether the mux rate looks constant bit rate (CBR) or variable (VBR), from the per-second rates.
    The spread is the 10th-to-90th percentile range relative to the median, so keyframe spikes
    at the top do not dominate. Returns None with fewer than ten seconds of data.
    This judges how the rate behaves on the wire; it cannot see the encoder's own setting."""
    vals = sorted(r for r in rates if r > 0)
    if len(vals) < 10:
        return None
    med = vals[len(vals) // 2]
    p10 = vals[int(len(vals) * 0.10)]
    p90 = vals[min(len(vals) - 1, int(len(vals) * 0.90))]
    spread = (p90 - p10) / med * 100 if med else 0.0
    if spread < 10:
        kind = "CBR-like"
        text = f"steady: within {spread:.0f}% of the median"
        if null_pct > 0.5:
            text += ", padded with null packets"
    else:
        kind = "VBR-like"
        text = f"varies: the 10th to 90th percentile spans {spread:.0f}% of the median"
    return {"mode": kind, "spread_pct": round(spread, 1), "label": f"{kind} ({text})"}


def payload_snapshot(ts, detail, srt_sec, probe_status="", mode=None, fields=None, raw=None):
    """Everything the views need, in one plain dict."""
    wins = list(ts.windows)
    use = wins[1:-1] if len(wins) > 3 else wins   # the first and last seconds are usually partial
    rows = [ts.summary_row(w) for w in use]

    def avg(key):
        vals = [r[key] for r in rows]
        return sum(vals) / len(vals) if vals else 0.0

    def recent(key, seconds=RECENT_S):
        vals = [r[key] for r in rows[-seconds:]]
        return sum(vals) / len(vals) if vals else 0.0

    findings = attribute(wins, srt_sec)
    for f in findings:
        f["who"] = owner_label(f["owner"], mode)
    verdict, tally = verdict_for(findings, mode)
    last = ts.summary_row(wins[-1]) if wins else None
    if last is not None and time.monotonic() - ts.start - wins[-1]["t"] > 2:
        # no packets for over two seconds: the stream has stopped, so the rate is zero
        last.update({"mux_mbps": 0.0, "video_mbps": 0.0, "audio_mbps": 0.0})
    return {
        "mode": mode,
        "pcr_intervals": ts.pcr_interval_stats(),
        "pids": ts.pid_table(),
        "streams": [{"pid": s["pid"], "kind": s["kind"], "codec": s["codec"],
                     "detail": detail.get(s["pid"], ""),
                     "fields": (fields or {}).get(s["pid"], [])} for s in ts.streams()],
        "last": last,
        "mux_avg": avg("mux_mbps"),
        "mux_peak": max((r["mux_mbps"] for r in rows), default=0.0),
        "video_avg": avg("video_mbps"),
        "audio_avg": avg("audio_mbps"),
        # rolling figures for the dashboard: the last 30 seconds, so a start-up burst does not hold them up
        "mux_recent": recent("mux_mbps"),
        "video_recent": recent("video_mbps"),
        "audio_recent": recent("audio_mbps"),
        "null_avg": avg("null_pct"),
        "rate_mode": rate_mode([r["mux_mbps"] for r in rows], avg("null_pct")),
        "cc_errors": ts.totals["cc_errors"],
        "pcr_disc": ts.totals["pcr_disc"],
        "table_changes": ts.totals["table_changes"],
        "sdt": dict(ts.sdt),
        "tr101290": ts.tr101290(),
        "connect_window": ts.connect_window(),
        "findings": findings,
        "verdict": verdict,
        "probe_status": probe_status,
        "config": config_findings(raw) if raw else [],
    }


def _pid_label(pid):
    try:
        return f"PID 0x{int(pid):04X}"
    except (TypeError, ValueError):
        return f"PID {pid}"


UNKNOWN_VALUES = {"-", "not signalled", "unknown", ""}   # a partial probe, not a real change
VOLATILE_FIELDS = {"Bit rate"}                           # a measurement that moves every probe


def field_changes(old, new):
    """Compare two {pid: [(label, value), ...]} maps from stream_fields().
    Returns rows (pid, field, before, after) for real changes only. A stream that appears or
    goes away is one row."""
    out = []
    for pid in sorted(set(old) | set(new), key=str):
        if pid not in old:
            out.append((pid, "stream", "not present", "present"))
            continue
        if pid not in new:
            out.append((pid, "stream", "present", "removed"))
            continue
        a, b = dict(old[pid]), dict(new[pid])
        for label in dict.fromkeys(list(a) + list(b)):
            before, after = a.get(label, "-"), b.get(label, "-")
            if label in VOLATILE_FIELDS or before == after:
                continue
            if before in UNKNOWN_VALUES or after in UNKNOWN_VALUES:
                continue
            out.append((pid, label, before, after))
    return out


def change_summary(changes):
    """One line for the events list: which fields changed on which stream."""
    by_pid = {}
    for pid, field, _, _ in changes:
        by_pid.setdefault(pid, []).append(field)
    parts = [f"{_pid_label(pid)} {', '.join(fields)}" for pid, fields in by_pid.items()]
    return "; ".join(parts) if parts else "stream details"


# ------------------------------------------------------------------ probe merge and config checks

def merge_probe(old, new):
    """Fold a new ffprobe reading into the previous one. A probe window that misses the SPS
    (where profile, chroma and colour live) reports those fields as missing; keep the last
    known values instead of flipping the display back to 'not signalled'."""
    out = {}
    for pid, s in new.items():
        prev = old.get(pid)
        if prev and prev.get("codec_name") == s.get("codec_name"):
            merged = dict(prev)
            for k, v in s.items():
                if v not in (None, "", "unknown"):
                    merged[k] = v
            out[pid] = merged
        else:
            out[pid] = dict(s)
    return out


# H.264 levels: (MaxFS in macroblocks, MaxMBPS in macroblocks per second)
H264_LEVELS = [("3.0", 1620, 40500), ("3.1", 3600, 108000), ("3.2", 5120, 216000),
               ("4.0", 8192, 245760), ("4.1", 8192, 245760), ("4.2", 8704, 522240),
               ("5.0", 22080, 589824), ("5.1", 36864, 983040), ("5.2", 36864, 2073600)]
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")
MATRIX_SDR = ("bt709", "smpte170m", "bt470bg", "")


def _fps(text):
    try:
        a, b = str(text).split("/")
        f = float(a) / float(b) if float(b) else 0.0
        return f if 0 < f <= 120 else 0.0                       # 90000/1 is the TS clock, not a rate
    except (ValueError, ZeroDivisionError):
        return 0.0


def config_findings(raw):
    """Signalling and profile mismatches in the stream's own metadata. Each finding says what
    the mismatch does on screen and what to check at the sender. These are checks on the
    metadata only: pixel clipping and banding need a picture analysis."""
    out = []

    def add(pid, level, title, why, check):
        out.append({"pid": pid, "level": level, "title": title, "why": why, "check": check})

    for pid, s in sorted(raw.items(), key=lambda kv: str(kv[0])):
        if s.get("codec_type") == "audio":
            sr = int(s.get("sample_rate") or 0)
            if sr and sr != 48000:
                add(pid, "warn", f"Audio sample rate is {sr} Hz, not 48 kHz",
                    "Broadcast chains normally run at 48 kHz. Receivers that do not resample "
                    "play it at the wrong pitch or speed.",
                    "Encode at 48 kHz (ffmpeg -ar 48000).")
            codec = s.get("codec_name", "")
            if codec and codec != "aac":
                add(pid, "info", f"Audio codec is {codec}, not AAC",
                    "AAC is the usual choice for SRT and MPEG-TS. Some receivers do not decode "
                    f"{codec}.",
                    "Use AAC-LC unless the receiver needs another codec.")
            continue
        if s.get("codec_type") != "video":
            continue

        codec = s.get("codec_name", "")
        prim = s.get("color_primaries", "")
        tr = s.get("color_transfer", "")
        mat = s.get("color_space", "")
        rng = s.get("color_range", "")
        pix = s.get("pix_fmt", "") or ""
        prof = s.get("profile", "") or ""
        w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        ten = "10" in pix
        c422, c444 = "422" in pix, "444" in pix
        hdr = tr in HDR_TRANSFERS
        bt2020 = prim == "bt2020"

        if hdr and prim != "bt2020":
            add(pid, "fail", "HDR transfer with non-BT.2020 primaries",
                f"The transfer is {tr}, an HDR curve, but the primaries are "
                f"{prim or 'not signalled'}. Colours land in the wrong gamut, so saturated colours "
                "and skin tones shift.",
                "Tag the primaries as bt2020 at the encoder (x264 colorprim=bt2020, "
                "ffmpeg -color_primaries bt2020).")
        if bt2020 and tr == "bt709":
            add(pid, "warn", "BT.2020 colours tagged with an SDR transfer",
                "Wide-gamut colours are tagged as SDR. Players that honour the tags show them "
                "washed out or clipped.",
                "If the source is HDR, set the transfer to arib-std-b67 (HLG) or smpte2084 (PQ). "
                "If it is SDR, set the primaries to bt709.")
        if hdr and not ten:
            add(pid, "warn", "HDR transfer at 8-bit",
                "HDR gradients need 10 bits. At 8 bits the highlights have few steps, which "
                "shows as banding and clipping.",
                "Encode 10-bit (yuv420p10le, High 10 profile).")
        if not tr and (ten or bt2020):
            add(pid, "warn", "Transfer function not signalled on a 10-bit or wide-gamut stream",
                "The decoder has to guess the transfer curve. HLG or PQ content decoded as SDR "
                "clips the highlights and bands the gradients. This is the most likely cause of "
                "clipping and banding on a 10-bit feed.",
                "Tag the transfer at the encoder (x264 transfer=arib-std-b67 for HLG, "
                "smpte2084 for PQ), then confirm it in the stream.")
        if bt2020 and mat in MATRIX_SDR:
            add(pid, "warn", "Colour matrix does not match BT.2020 primaries",
                f"Primaries are BT.2020 but the matrix is {mat or 'not signalled'}. Luma and "
                "chroma decode with different coefficients, so colours shift.",
                "Set the matrix to bt2020nc (x264 colormatrix=bt2020nc, ffmpeg -colorspace bt2020nc).")
        if rng == "pc" and (hdr or bt2020):
            add(pid, "info", "Full-range (PC) levels on wide-gamut or HDR video",
                "Broadcast HDR is normally limited range. If the receiver expects limited range, "
                "blacks crush and whites clip.",
                "Confirm the range at the source (x264 range=tv, ffmpeg -color_range tv).")
        if codec == "h264" and prof:
            if prof == "High" and (ten or c422 or c444):
                add(pid, "fail", "Profile does not allow this bit depth or chroma",
                    "High profile is 8-bit 4:2:0 only. This stream needs High 10 or High 4:2:2, "
                    "so some decoders refuse it or misdecode it.",
                    "Encode with -profile:v high10 (10-bit 4:2:0) or high422 (4:2:2).")
            elif prof == "High 10" and (c422 or c444):
                add(pid, "fail", "High 10 profile with 4:2:2 or 4:4:4 content",
                    "High 10 is 4:2:0 only. The 4:2:2 or 4:4:4 content is outside the profile.",
                    "Encode with -profile:v high422.")
            elif prof == "Main" and (ten or c422 or c444):
                add(pid, "fail", "Main profile with 10-bit or 4:2:2 content",
                    "Main profile is 8-bit 4:2:0 only.",
                    "Encode with a profile that matches the bit depth and chroma.")
        if codec == "hevc" and prof == "Main" and ten:
            add(pid, "fail", "HEVC Main profile with 10-bit content",
                "HEVC Main is 8-bit. 10-bit needs Main 10.",
                "Encode with Main 10.")
        if codec == "h264" and isinstance(s.get("level"), int) and s["level"] > 0 and w and h:
            fps = _fps(s.get("avg_frame_rate") or s.get("r_frame_rate"))
            fs = -(-w // 16) * -(-h // 16)
            need = next((name for name, max_fs, max_mbps in H264_LEVELS
                         if fs <= max_fs and fs * fps <= max_mbps), None)
            have = f"{s['level'] / 10:.1f}"
            limit = next((mbps for name, _, mbps in H264_LEVELS if name == have), None)
            if need and limit and fs * fps > limit:
                add(pid, "warn", f"Level {have} is too low for {w}x{h} at {fps:.2f} fps",
                    f"Level {have} allows {limit:,} macroblocks per second. This picture needs "
                    f"{fs * fps:,.0f}. Some decoders refuse the stream or drop frames.",
                    f"Signal level {need} or higher (x264 level={need}).")
        fo = s.get("field_order", "")
        if fo and fo not in ("progressive", "unknown"):
            add(pid, "info", f"Scan type: {field_order_text(fo)}",
                "Describes the picture format. Interlaced video is sent as two fields per frame. "
                "This is not a fault.", "")
    return out


def _video_es(snap, pid):
    """The elementary stream of one video PID in a TS snapshot: PES payloads joined in order."""
    es = bytearray()
    cur = None                                   # PES bytes being collected for this PID
    # a snapshot can start in the middle of a packet: find the first sync byte that repeats 188 bytes later
    first = next((k for k in range(min(188, len(snap))) if snap[k:k + 1] == b"\x47"
                  and snap[k + 188:k + 189] == b"\x47"), 0)
    for off in range(first, len(snap) - 187, 188):
        p = snap[off:off + 188]
        if p[0] != 0x47:
            continue
        this_pid = ((p[1] & 0x1F) << 8) | p[2]
        if this_pid != pid:
            continue
        start = bool(p[1] & 0x40)
        afc = (p[3] >> 4) & 3
        i = 4
        if afc & 2:
            i += 1 + p[4]
        if not (afc & 1) or i >= 188:
            continue
        if start:
            if cur is not None and len(cur) > 9 and cur[:3] == b"\x00\x00\x01":
                es += cur[9 + cur[8]:]
            cur = bytearray()
        if cur is not None:
            cur += p[i:]
    if cur is not None and len(cur) > 9 and cur[:3] == b"\x00\x00\x01":
        es += cur[9 + cur[8]:]
    return bytes(es)


def count_keyframe_headers(es):
    """For each IDR picture, whether an SPS and a PPS came before it since the previous picture.
    Returns {'keyframes': n, 'with_headers': m}."""
    keys = with_headers = 0
    seen_sps = seen_pps = False
    i = es.find(b"\x00\x00\x01")
    while 0 <= i < len(es) - 3:
        nal = es[i + 3] & 0x1F
        if nal == 7:
            seen_sps = True
        elif nal == 8:
            seen_pps = True
        elif nal in (1, 5):
            if nal == 5:
                keys += 1
                if seen_sps and seen_pps:
                    with_headers += 1
            seen_sps = seen_pps = False
        i = es.find(b"\x00\x00\x01", i + 3)
    return {"keyframes": keys, "with_headers": with_headers}


def keyframe_header_check(snap):
    """Headers in front of keyframes for the video stream of a snapshot. None if it cannot be read."""
    info = probe_stream(snap)
    if not info:
        return None
    vid = next((pid for pid, st in info.items() if st.get("codec_type") == "video"), None)
    if vid is None:
        return None
    return count_keyframe_headers(_video_es(snap, vid))
