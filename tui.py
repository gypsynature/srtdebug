"""
srtdebug ui - interactive curses front end.

Same engine as `srtdebug monitor`, but every setting can be changed while it
runs: mode, host, port, latency, streamid, sample interval, stats cadence,
verbosity, CSV/JSONL export, counter reset, freeze, and a re-run of the check.
"""

import csv
import curses
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
import netlink

import report
import srtdebug as core
import tsanalyze as ta

STATS_CHOICES = [5, 10, 20, 50, 100, 200, 500]
BLOCKS = " ▁▂▃▄▅▆▇█"
FLASH_SECONDS = 3.0


# ------------------------------------------------------------------ engine

class Session:
    """Owns the srt-live-transmit child and turns its stats into rows."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.start = time.time()
        self.base = {k: 0 for k in core.KEYS}   # totals carried across connections/resets
        self.last = None                         # latest raw sample (cumulative per connection)
        self.prev = None                         # rate baseline (last emitted sample)
        self.row = None
        self.last_seen = None
        self.history = deque(maxlen=600)         # bitrate per sample, for the chart
        self.rate_n, self.rate_sum, self.rate_min, self.rate_max = 0, 0.0, None, 0.0
        self.rtt_n, self.rtt_sum, self.rtt_max = 0, 0.0, 0.0
        self.events = deque(maxlen=200)          # (time, kind, text)
        self.event_count = 0
        self.rows = 0
        self.proc = None
        self.tmp = None
        self.stats_path = None
        self.fh = None
        self.buf = ""
        self.error = None
        self.check = []
        self.export_fh = None
        self.export_fmt = None
        self.export_writer = None
        self.export_path = None
        self.payload = ta.PayloadFeed(mode=cfg.get("mode")) if cfg.get("payload") else None
        self.srt_sec = {}          # per-second SRT evidence, for attribution
        self.rows_list = []        # every sample, for the report
        self.prev_lost = None
        self.prev_dropped = None
        self.connects = 0
        self.reconnect_pending = False
        self._view = None
        self._view_t = 0.0

    # -- connection lifecycle
    def connect(self):
        self.stop_proc()
        self.error = None
        self.prev = None
        self.last_seen = None
        self.row = None
        c = self.cfg
        try:
            uri = core.build_uri(c["mode"], c["host"], c["port"], c["latency"], c["streamid"],
                                 c.get("passphrase"), c.get("pbkeylen"))
        except SystemExit as e:
            self.error = str(e)
            return
        self.tmp = tempfile.mkdtemp(prefix="srtdebug-")
        self.stats_path = os.path.join(self.tmp, "stats.jsonl")
        # -f: cumulative counters. Without it srt-live-transmit reports per-interval counts.
        cmd = [core.SRT_BIN, "-s", str(c["stats_every"]), "-f", "-pf", "json",
               "-statsout", self.stats_path, uri, core.SINK]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if self.payload else subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, text=True)
        except OSError as e:
            self.error = f"cannot start {core.SRT_BIN}: {e}"
            self.proc = None
            return
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
        if self.payload:
            self.payload.start(self.proc)

    def stop_proc(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
        if self.fh:
            self.fh.close()
            self.fh = None
        self.buf = ""
        if self.last:
            for k in core.KEYS:
                self.base[k] += self.last[k]   # keep totals when the connection goes away
        self.last = None
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    def run_check(self):
        self.stop_proc()  # so our own listener does not count as "port in use"
        self.check = core.check_environment(self.cfg["mode"], self.cfg["host"], self.cfg["port"])
        self.connect()

    # -- background: srt-live-transmit stderr (connect / disconnect messages)
    def _read_stderr(self, proc):
        for line in proc.stderr:
            line = line.strip()
            if not line:
                continue
            kind = self._classify(line)
            if kind:
                self.events.append((time.strftime("%H:%M:%S UTC", time.gmtime()), kind, line, None))
                if kind in ("connected", "disconnected"):
                    self.event_count += 1
                if kind == "connected":
                    self.connects += 1
                    if self.connects > 1:
                        self.reconnect_pending = True

    @staticmethod
    def _classify(line):
        low = line.lower()
        if "disconnect" in low or "broken" in low:
            return "disconnected"
        if "connected" in low or "accepted" in low:
            return "connected"
        if "error" in low or "fail" in low:
            return "error"
        return None

    def add_event(self, kind, text):
        self.events.append((time.strftime("%H:%M:%S UTC", time.gmtime()), kind, text, None))

    # -- stats
    def pump(self):
        """Read whatever stats lines are available. Never blocks."""
        if self.proc is None:
            return
        if self.fh is None and self.stats_path and os.path.exists(self.stats_path):
            self.fh = open(self.stats_path, "r")
        while self.fh:
            chunk = self.fh.readline()
            if not chunk:
                break
            self.buf += chunk
            if self.buf.endswith("\n"):
                self._on_line(self.buf)
                self.buf = ""

    def _on_line(self, text):
        try:
            d = json.loads(text)
        except ValueError:
            return
        s = core.parse_sample(d)
        self.last_seen = time.time()
        before = self.last
        self.last = s
        if before is not None and s["rx_bytes"] < before["rx_bytes"]:
            for k in core.KEYS:  # counters restarted: the stream reconnected inside srt
                self.base[k] += before[k]
            self.prev = None
            self.add_event("info", "counters restarted (reconnect)")
        if self.prev is None:
            self.prev = s
            return

        interval = self.cfg["interval"]
        dt = s["t"] - self.prev["t"]
        if dt <= 0 or dt < interval:
            return
        rate = (s["rx_bytes"] - self.prev["rx_bytes"]) * 8 / dt / 1e6
        self.prev = s

        total = s["rx_packets"] + s["lost"]
        row = {
            "time": time.strftime("%H:%M:%S UTC", time.gmtime()),
            "elapsed_s": round(time.time() - self.start, 1),
            "bitrate_mbps": round(rate, 3),
            "rx_packets": s["rx_packets"],
            "lost": s["lost"],
            "loss_pct": round(100.0 * s["lost"] / total, 3) if total else 0.0,
            "retrans": s["retrans"],
            "dropped": s["dropped"],
            "belated": s["belated"],
            "rtt_ms": s["rtt"],
            "buf_ms": s["buf_ms"],
            "link_bw_mbps": round(s["link_bw"], 1),
            "flow_window": s["window"],
        }
        sec = int(row["elapsed_s"])
        lost_d = max(0, s["lost"] - self.prev_lost) if self.prev_lost is not None else 0
        drop_d = max(0, s["dropped"] - self.prev_dropped) if self.prev_dropped is not None else 0
        self.prev_lost, self.prev_dropped = s["lost"], s["dropped"]
        self.srt_sec[sec] = {"lost": lost_d, "dropped": drop_d, "reconnect": self.reconnect_pending}
        self.reconnect_pending = False
        self.rows_list.append(row)
        self.row = row
        self.rows += 1
        self.history.append(rate)
        self.rate_n += 1
        self.rate_sum += rate
        self.rate_min = rate if self.rate_min is None else min(self.rate_min, rate)
        self.rate_max = max(self.rate_max, rate)
        self.rtt_n += 1
        self.rtt_sum += s["rtt"]
        self.rtt_max = max(self.rtt_max, s["rtt"])
        self._write_row(row)

    def payload_view(self):
        if not self.payload:
            return None
        now = time.time()
        if self._view is None or now - self._view_t >= 1.0:
            self._view = self.payload.snapshot(self.srt_sec)
            self._view_t = now
        return self._view

    def totals(self):
        last = self.last or {k: 0 for k in core.KEYS}
        return {k: self.base[k] + last[k] for k in core.KEYS}

    def status(self, now):
        if self.error:
            return "CONFIG ERROR", "red"
        if self.proc is None:
            return "IDLE", "dim"
        if self.proc.poll() is not None:
            return "STOPPED", "red"
        if self.last_seen is None:
            return "WAITING FOR STREAM", "yellow"
        iv = self.cfg["interval"]
        if now - self.last_seen > max(5.0, 3 * iv):
            return f"NO DATA for {int(now - self.last_seen)}s", "red"
        if self.row and self.row["bitrate_mbps"] < 0.01:
            return "CONNECTED, NO PAYLOAD", "yellow"
        return "RECEIVING", "green"

    def reset_counters(self):
        """Zero the totals and rate stats from now on, without dropping the connection."""
        last = self.last or {k: 0 for k in core.KEYS}
        self.base = {k: -last[k] for k in core.KEYS}
        self.rate_n, self.rate_sum, self.rate_min, self.rate_max = 0, 0.0, None, 0.0
        self.rtt_n, self.rtt_sum, self.rtt_max = 0, 0.0, 0.0
        self.history.clear()

    # -- export
    def set_export(self, path):
        self.close_export()
        if not path:
            return
        fmt = "csv" if path.lower().endswith(".csv") else "jsonl"
        fh = open(path, "w", newline="", buffering=1)  # may raise OSError; caller handles it
        self.export_fh, self.export_fmt, self.export_path = fh, fmt, path
        if fmt == "csv":
            self.export_writer = csv.DictWriter(fh, fieldnames=core.ROW_FIELDS)
            self.export_writer.writeheader()

    def close_export(self):
        if self.export_fh:
            self.export_fh.close()
        self.export_fh = self.export_writer = self.export_fmt = None

    def _write_row(self, row):
        if not self.export_fh:
            return
        if self.export_fmt == "csv":
            self.export_writer.writerow(row)
        else:
            self.export_fh.write(json.dumps(row) + "\n")

    def close(self):
        self.stop_proc()
        self.close_export()

    def summary_lines(self):
        t = self.totals()
        out = [f"Session summary  ({core.fmt_hms(time.time() - self.start)})"]
        if self.rate_n:
            out.append(f"  Bitrate   avg {self.rate_sum / self.rate_n:.2f} Mbps   "
                       f"min {self.rate_min:.2f}   peak {self.rate_max:.2f}")
        else:
            out.append("  Bitrate   no data received")
        total = t["rx_packets"] + t["lost"]
        pct = 100.0 * t["lost"] / total if total else 0.0
        out.append(f"  Packets   {t['rx_packets']:,} received   lost {t['lost']} ({pct:.2f}%)   "
                   f"retransmitted {t['retrans']}   dropped {t['dropped']}")
        out.append(f"  Data      {t['rx_bytes'] / 1e6:,.1f} MB")
        if self.rtt_n:
            out.append(f"  RTT       avg {self.rtt_sum / self.rtt_n:.1f} ms   max {self.rtt_max:.1f} ms")
        out.append(f"  Events    {self.event_count} connect/disconnect")
        if self.export_path:
            out.append(f"  Exported  {self.rows} rows -> {self.export_path}")
        return out


# ------------------------------------------------------------------ drawing

COLOR_PAIRS = {"green": 1, "yellow": 2, "red": 3, "cyan": 4, "dim": 0}


def init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_GREEN, bg)
    curses.init_pair(2, curses.COLOR_YELLOW, bg)
    curses.init_pair(3, curses.COLOR_RED, bg)
    curses.init_pair(4, curses.COLOR_CYAN, bg)


def attr(name, extra=0):
    if name == "dim":
        return curses.A_DIM | extra
    return curses.color_pair(COLOR_PAIRS.get(name, 0)) | extra


def put(scr, y, x, text, a=0):
    h, w = scr.getmaxyx()
    if 0 <= y < h and 0 <= x < w:
        try:
            scr.addnstr(y, x, text, max(0, w - x - 1), a)
        except curses.error:
            pass


def loss_name(pct):
    return "red" if pct >= core.LOSS_BAD_PCT else "yellow" if pct >= core.LOSS_WARN_PCT else "green"


def rtt_name(ms):
    return "red" if ms >= core.RTT_BAD_MS else "yellow" if ms >= core.RTT_WARN_MS else "green"


def draw_chart(scr, y0, x0, width, height, values):
    values = values[-width:]
    peak = max(values + [0.1])
    levels = height * 8
    for i, v in enumerate(values):
        x = x0 + width - len(values) + i
        filled = int(round(v / peak * levels))
        for r in range(height):
            rb = height - 1 - r
            cell = min(8, max(0, filled - rb * 8))
            put(scr, y0 + r, x, BLOCKS[cell], attr("cyan"))


def draw(scr, sess, st):
    scr.erase()
    h, w = scr.getmaxyx()
    now = time.time()
    cfg = sess.cfg
    status, color = sess.status(now)
    row = sess.row or {}
    rate = row.get("bitrate_mbps", 0.0) if status == "RECEIVING" else 0.0
    t = sess.totals()
    right = w >= 100
    lw = w - 36 if right else w

    where = (f"listener {cfg['host'] or '0.0.0.0'}:{cfg['port']}" if cfg["mode"] == "listener"
             else f"caller -> {cfg['host'] or '?'}:{cfg['port']}")
    lat = f"{cfg['latency']} ms" if cfg["latency"] is not None else "default"
    frozen = "   [FROZEN]" if st["frozen"] else ""
    header = (f" srtdebug   {where}   latency {lat}"
              f"   up {core.fmt_hms(now - sess.start)}{frozen}")
    put(scr, 0, 0, header.ljust(w), curses.A_REVERSE | curses.A_BOLD)

    put(scr, 2, 2, f"● {status}", attr(color, curses.A_BOLD))
    if sess.events:
        ts, _, text, _detail = sess.events[-1]
        put(scr, 2, 4 + len(status) + 2, f"last event {ts}: {text}"[:max(0, lw - len(status) - 10)], attr("dim"))

    put(scr, 4, 2, "BITRATE", attr("dim"))
    put(scr, 5, 2, f"{rate:8.2f} Mbps", curses.A_BOLD)
    if sess.rate_n:
        put(scr, 5, 24, f"avg {sess.rate_sum / sess.rate_n:.2f}   "
                        f"min {sess.rate_min:.2f}   peak {sess.rate_max:.2f}", attr("dim"))

    chart_w = max(10, lw - 4)
    put(scr, 7, 2, f"last {len(sess.history)} samples", attr("dim"))
    draw_chart(scr, 8, 2, chart_w, 4, list(sess.history)[-chart_w:])

    total = t["rx_packets"] + t["lost"]
    loss = 100.0 * t["lost"] / total if total else 0.0
    rtt = row.get("rtt_ms", 0)
    y = 13
    put(scr, y, 2, "PACKETS", attr("dim"))
    put(scr, y, 14, f"{t['rx_packets']:,} rx")
    put(scr, y, 30, "lost", attr("dim"))
    put(scr, y, 36, f"{t['lost']} ({loss:.2f}%)", attr(loss_name(loss), curses.A_BOLD))
    put(scr, y, 56, f"retrans {t['retrans']}   dropped {t['dropped']}")
    y += 1
    put(scr, y, 2, "LATENCY", attr("dim"))
    put(scr, y, 14, "RTT", attr("dim"))
    put(scr, y, 18, f"{rtt:.1f} ms", attr(rtt_name(rtt), curses.A_BOLD))
    put(scr, y, 30, "buffer", attr("dim"))
    put(scr, y, 37, f"{row.get('buf_ms', 0)} ms")
    if cfg["verbose"]:
        y += 1
        put(scr, y, 2, "LINK", attr("dim"))
        put(scr, y, 14, f"est. bandwidth {row.get('link_bw_mbps', 0)} Mbps   "
                        f"flow window {row.get('flow_window', 0)}   belated {row.get('belated', 0)}")


    # payload panel (when --payload is on), then the event log fills the rest of the column
    view = sess.payload_view()
    y += 2
    if view:
        y = draw_payload(scr, y, 2, lw - 4, view)
    put(scr, y, 2, "EVENTS", attr("dim"))
    room = max(0, (h - 6) - (y + 1))
    recent = list(sess.events)[-room:] if room else []
    for i, (ts, kind, text, _detail) in enumerate(recent):
        color_name = {"connected": "green", "disconnected": "yellow", "error": "red"}.get(kind, "dim")
        put(scr, y + 1 + i, 4, f"{ts}  {text}", attr(color_name))

    if right:
        draw_settings(scr, 2, lw + 2, sess, st)

    # footer: key hints + transient message
    put(scr, h - 3, 2, "q quit   m mode   p port   h host   l latency   s streamid   +/- interval   [ ] stats",
        attr("dim"))
    put(scr, h - 2, 2, "v verbose   e export   r reset   space freeze   c check   R report   ? help",
        attr("dim"))
    msg = st.get("msg", "") if time.time() < st.get("msg_until", 0) else ""
    put(scr, h - 1, 2, msg, attr("cyan", curses.A_BOLD))
    scr.refresh()


def draw_payload(scr, y, x, width, view):
    """Codecs, rates, integrity and the latest suspected causes, in a fixed 7-row block.
    Returns the next free row. A fixed block keeps the layout steady as streams appear."""
    put(scr, y, x, "PAYLOAD", attr("dim"))
    last = view.get("last") or {}
    put(scr, y, x + 12, f"mux {last.get('mux_mbps', 0):.2f} Mbps   video {last.get('video_mbps', 0):.2f}   "
                        f"audio {last.get('audio_mbps', 0):.2f}   null {last.get('null_pct', 0):.1f}%",
        curses.A_BOLD)
    streams = view["streams"][:3]
    for i in range(3):  # always three stream rows; unused ones stay blank
        row = y + 1 + i
        if i == 0 and not streams:
            put(scr, row, x + 2, "waiting for the program tables (PAT/PMT)...", attr("dim"))
        elif i < len(streams):
            st = streams[i]
            color = "cyan" if st["kind"] == "video" else "dim"
            put(scr, row, x + 2, f"PID 0x{st['pid']:04X}  {st['kind']:<6} {st['codec']:<13} {st['detail']}",
                attr(color))
    bad = view["cc_errors"] or view["table_changes"]
    put(scr, y + 4, x + 2, f"CC errors {view['cc_errors']}   PCR breaks {view['pcr_disc']}   "
                           f"table changes {view['table_changes']}   codec detail: {view['probe_status']}",
        attr("red" if bad else "green", curses.A_BOLD))
    owner_colour = {"sender": "yellow", "network": "cyan", "receiver": "magenta", "link": "cyan"}
    findings = view["findings"][-2:]
    for i in range(2):  # two finding rows, blank when there are fewer
        if i < len(findings):
            f = findings[i]
            put(scr, y + 5 + i, x + 2, f"{f['t']}s  suspected on {f.get('who', f['owner'])} ({f['confidence']}): {f['what']}",
                attr(owner_colour.get(f["owner"], "dim")))
    return y + 7


def write_session_report(sess, path):
    meta = {
        "title": "srtdebug session", "mode": sess.cfg["mode"], "host": sess.cfg["host"],
        "port": sess.cfg["port"], "latency": sess.cfg["latency"], "streamid": sess.cfg["streamid"],
        "target_mbps": None, "redact": False,
        "started": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(sess.start)),
    }
    return report.write_output(path, sess.rows_list, meta, checks=sess.check,
                               events=list(sess.events), payload=sess.payload_view())


def draw_settings(scr, y, x, sess, st):
    cfg = sess.cfg
    put(scr, y, x, "SETTINGS", attr("dim"))
    exp = os.path.basename(sess.export_path) if sess.export_path else "off"
    rows = [
        ("mode", cfg["mode"], "m"),
        ("host", cfg["host"] or "-", "h"),
        ("port", str(cfg["port"]), "p"),
        ("latency", f"{cfg['latency']} ms" if cfg["latency"] is not None else "default", "l"),
        ("streamid", cfg["streamid"] or "-", "s"),
        ("interval", f"{cfg['interval']:.1f} s", "+ -"),
        ("stats every", f"{cfg['stats_every']} pkt", "[ ]"),
        ("verbose", "on" if cfg["verbose"] else "off", "v"),
        ("export", exp, "e"),
        ("display", "frozen" if st["frozen"] else "live", "space"),
    ]
    for i, (label, value, key) in enumerate(rows):
        yy = y + 2 + i
        put(scr, yy, x, f"{label:<12}", attr("dim"))
        put(scr, yy, x + 12, f"{value:<12.12}", curses.A_BOLD)
        put(scr, yy, x + 25, f"({key})", attr("dim"))
    y2 = y + 2 + len(rows) + 1
    put(scr, y2, x, "PRE-FLIGHT", attr("dim"))
    fails = sum(1 for r in sess.check if r[0] == "fail")
    warns = sum(1 for r in sess.check if r[0] == "warn")
    if not sess.check:
        put(scr, y2 + 1, x, "not run yet (press c)", attr("dim"))
    elif fails:
        put(scr, y2 + 1, x, f"{fails} FAIL  (press c)", attr("red", curses.A_BOLD))
    elif warns:
        put(scr, y2 + 1, x, f"{warns} WARN  (press c)", attr("yellow", curses.A_BOLD))
    else:
        put(scr, y2 + 1, x, "ready  (press c)", attr("green", curses.A_BOLD))


def overlay(scr, title, lines):
    h, w = scr.getmaxyx()
    bw = min(w - 4, max([len(title)] + [len(l) for l in lines]) + 6)
    bh = min(h - 2, len(lines) + 4)
    y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
    win = curses.newwin(bh, bw, y0, x0)
    win.erase()  # newwin does not clear what is underneath; without this the old screen shows through
    win.box()
    win.addnstr(0, 2, f" {title} ", bw - 4, curses.A_BOLD)
    for i, line in enumerate(lines[:bh - 4]):
        try:
            win.addnstr(2 + i, 2, line, bw - 4)
        except curses.error:
            pass
    win.addnstr(bh - 2, 2, "press any key", bw - 4, curses.A_DIM)
    win.refresh()
    scr.timeout(-1)
    scr.getch()
    scr.timeout(100)


def prompt(scr, label, default=""):
    h, w = scr.getmaxyx()
    y = h - 1
    scr.move(y, 0)
    scr.clrtoeol()
    text = f"{label} [{default}]: " if default else f"{label}: "
    put(scr, y, 2, text, attr("cyan", curses.A_BOLD))
    curses.echo()
    curses.curs_set(1)
    scr.timeout(-1)
    try:
        raw = scr.getstr(y, 2 + len(text), 60)
        value = raw.decode(errors="ignore").strip()
    except curses.error:
        value = ""
    finally:
        curses.noecho()
        curses.curs_set(0)
        scr.timeout(100)
    return value or default


def flash(st, text):
    st["msg"] = text
    st["msg_until"] = time.time() + FLASH_SECONDS


# ------------------------------------------------------------------ input

HELP = [
    "m   toggle caller / listener",
    "p   change port            h   change host",
    "l   change latency (ms)    s   change streamid",
    "+ / -   sample interval    [ / ]   stats cadence",
    "v   verbose fields         e   export on/off (CSV or JSONL)",
    "r   reset totals and chart space   freeze   display (keeps counting)",
    "R   write an HTML or PDF report of this session",
    "c   re-run pre-flight check (stops and restarts the stream)",
    "q   quit (prints a session summary)",
    "",
    "Changing port, host, mode, latency, streamid or stats cadence reconnects.",
    "Totals carry across reconnects. Press r to start counting from zero.",
]


def handle_key(scr, sess, st, ch):
    cfg = sess.cfg
    k = chr(ch) if 0 <= ch < 256 else ""

    if k in ("q", "Q"):
        return "quit"

    if k == "m":
        cfg["mode"] = "caller" if cfg["mode"] == "listener" else "listener"
        sess.connect()
        flash(st, f"mode: {cfg['mode']}" + ("  (set a host with h for caller)" if cfg["mode"] == "caller" and not cfg["host"] else ""))

    elif k == "p":
        v = prompt(scr, "port", str(cfg["port"]))
        try:
            port = int(v)
            if not 1 <= port <= 65535:
                raise ValueError
            cfg["port"] = port
            sess.connect()
            flash(st, f"port {port}, reconnecting")
        except ValueError:
            flash(st, "port must be 1-65535")

    elif k == "h":
        v = prompt(scr, "host (blank = 0.0.0.0 / none)", cfg["host"] or "")
        cfg["host"] = v or None
        sess.connect()
        flash(st, f"host {cfg['host'] or '(none)'}, reconnecting")

    elif k == "l":
        v = prompt(scr, "latency ms (blank = default)", "" if cfg["latency"] is None else str(cfg["latency"]))
        try:
            cfg["latency"] = int(v) if v else None
            sess.connect()
            flash(st, "latency updated, reconnecting")
        except ValueError:
            flash(st, "latency must be a whole number of ms")

    elif k == "s":
        v = prompt(scr, "streamid (blank = none)", cfg["streamid"] or "")
        cfg["streamid"] = v or None
        sess.connect()
        flash(st, "streamid updated, reconnecting")

    elif k in ("+", "="):
        cfg["interval"] = min(10.0, round(cfg["interval"] + 0.5, 1))
        flash(st, f"sample interval {cfg['interval']:.1f} s")

    elif k in ("-", "_"):
        cfg["interval"] = max(0.5, round(cfg["interval"] - 0.5, 1))
        flash(st, f"sample interval {cfg['interval']:.1f} s")

    elif k in ("[", "]"):
        i = min(range(len(STATS_CHOICES)), key=lambda j: abs(STATS_CHOICES[j] - cfg["stats_every"]))
        i = max(0, i - 1) if k == "[" else min(len(STATS_CHOICES) - 1, i + 1)
        cfg["stats_every"] = STATS_CHOICES[i]
        sess.connect()
        flash(st, f"stats every {cfg['stats_every']} packets, reconnecting")

    elif k == "v":
        cfg["verbose"] = not cfg["verbose"]
        flash(st, f"verbose {'on' if cfg['verbose'] else 'off'}")

    elif k == "e":
        if sess.export_fh:
            sess.close_export()
            flash(st, "export stopped")
        else:
            default = time.strftime("srtdebug-%Y%m%d-%H%M%SZ.csv", time.gmtime())
            path = prompt(scr, "export file (.csv or .jsonl)", default)
            try:
                sess.set_export(path)
                flash(st, f"exporting to {path}")
            except OSError as e:
                flash(st, f"export failed: {e.strerror or e}")

    elif k == "r":
        sess.reset_counters()
        flash(st, "counters reset")

    elif ch == ord(" "):
        st["frozen"] = not st["frozen"]
        flash(st, "display frozen (still counting)" if st["frozen"] else "display live")

    elif k == "c":
        sess.run_check()
        overlay(scr, "pre-flight check", [
            f"[{ {'ok': 'PASS', 'warn': 'WARN', 'fail': 'FAIL', 'info': 'INFO'}[lvl] }] {name:<18} {detail}"
            + (f"   fix: {fix}" if fix and lvl in ("warn", "fail") else "")
            for lvl, name, detail, fix in sess.check
        ])

    elif k == "R":
        if not sess.rows_list:
            flash(st, "no samples yet, so there is nothing to report")
        else:
            default = time.strftime("srtdebug-report-%Y%m%d-%H%M%SZ.html", time.gmtime())
            path = prompt(scr, "report file (.html or .pdf)", default)
            try:
                written = write_session_report(sess, path)
                flash(st, f"report written: {written}")
            except (OSError, RuntimeError) as e:
                flash(st, f"report failed: {e}")

    elif k == "?":
        overlay(scr, "keys", HELP)

    return None


def loop(scr, sess):
    curses.curs_set(0)
    init_colors()
    scr.timeout(100)
    st = {"msg": "", "msg_until": 0, "frozen": False}
    if sess.error:
        flash(st, sess.error)
    while True:
        sess.pump()
        if not st["frozen"]:
            draw(scr, sess, st)
        ch = scr.getch()
        if ch == -1:
            continue
        if ch == curses.KEY_RESIZE:
            scr.clear()
            continue
        if handle_key(scr, sess, st, ch) == "quit":
            return


# ------------------------------------------------------------------ entry

def run(a):
    passphrase = core.resolve_passphrase(a)
    cfg = {
        "passphrase": passphrase, "pbkeylen": a.pbkeylen, "encrypted": bool(passphrase),
        "mode": a.mode, "host": a.host, "port": a.port, "latency": a.latency,
        "streamid": a.streamid, "stats_every": a.stats_every,
        "interval": a.interval, "verbose": a.verbose,
        "payload": getattr(a, "payload", False),
    }
    sess = Session(cfg)
    sess.check = core.check_environment(cfg["mode"], cfg["host"], cfg["port"])
    if a.export:
        try:
            sess.set_export(a.export)
        except OSError as e:
            print(f"error: cannot open export file: {e}")
            return 1
    sess.connect()
    try:
        curses.wrapper(loop, sess)
    except KeyboardInterrupt:
        pass
    finally:
        sess.close()
    print("\n".join(sess.summary_lines()))
    return 0
