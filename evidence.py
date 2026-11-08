"""Evidence for problem pictures: a PNG of each frozen or black picture, with a plain description.

Given a stretch of MPEG-TS (a recorded file, or the rolling snapshot the monitor keeps), this finds the
frozen and black pictures using ffmpeg's detectors, decodes one frame from each, and writes:

- one PNG per event, named by its time, and
- report.html: a single page with each frame, its time (in UTC, to the millisecond), and what was detected.

What a frame here proves: the picture had this problem in the stream that was received at this point.
It does not say where the fault started (encoder, network or decoder). The description gives context only.
"""

import base64
import datetime
import html
import os
import subprocess
import time

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), "srtdebug-evidence")   # where live captures are kept
MIN_FREEZE_S = 2.0        # shorter freezes are not reported (ffmpeg's freezedetect uses 2 s too)
MAX_FRAMES = 50           # a page with more than this many pictures is hard to read; the rest are listed in the text


def _seconds_text(x):
    return "%.2f s" % x


def events(result, window_start_utc=None):
    """Problem pictures from one content.analyse() result, in time order.
    A freeze that lies entirely inside a black picture is left out: black frames are identical too, but
    'black' is the true description."""
    black = [(a, b) for a, b in result.get("black_events", []) if b is not None]
    out = []
    for a, b in black:
        out.append({"kind": "black", "start": a, "end": b, "text": "Black picture for %s (from %s)."
                    % (_seconds_text(b - a), _seconds_text(a))})
    for a, b in result.get("freeze_events", []):
        if b is None:
            b = a + MIN_FREEZE_S   # the freeze was still running when the window ended
        if b - a < MIN_FREEZE_S:
            continue
        if any(ba <= a and b <= bb + 0.05 for ba, bb in black):
            continue
        out.append({"kind": "freeze", "start": a, "end": b, "text": "Picture frozen for %s (from %s)."
                    % (_seconds_text(b - a), _seconds_text(a))})
    out.sort(key=lambda e: e["start"])
    for e in out:
        e["utc"] = _utc_text(window_start_utc, e["start"])
    return out


def _utc_text(window_start_utc, offset_s):
    if window_start_utc is None:
        return ""
    t = datetime.datetime.fromtimestamp(window_start_utc + offset_s, datetime.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S.") + "%03d UTC" % (t.microsecond // 1000)


def grab_frame(ts_path, at_s, out_png, timeout=30):
    """Decode the first frame at or after this time (seconds from the start of ts_path) and save it as PNG.
    The frame is picked inside the filter graph by its timestamp: an input seek (-ss before -i) landed on
    the wrong frame for MPEG-TS files that do not start at zero."""
    select = "select=gte(t\\,%.3f)" % max(0.0, at_s)
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", ts_path,
                            "-map", "0:v:0", "-vf", select, "-frames:v", "1", out_png],
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and os.path.exists(out_png) and os.path.getsize(out_png) > 0


def build(ts_path, outdir, result, window_start_utc=None, title="Problem pictures"):
    """Write the frames and report.html for one analysed stretch. Returns the list of entries (with 'png')."""
    os.makedirs(outdir, exist_ok=True)
    found = events(result, window_start_utc)
    entries = []
    for i, e in enumerate(found[:MAX_FRAMES]):
        # a black picture is judged in its middle; a freeze at its first frame, where it starts
        at = e["start"] + (min(0.5, (e["end"] - e["start"]) / 2) if e["kind"] == "black" else 0.1)
        name = "%02d-%s-%06.2fs.png" % (i + 1, e["kind"], e["start"])
        png = os.path.join(outdir, name)
        e = dict(e, png=name if grab_frame(ts_path, at, png) else "", frame_at=at)
        entries.append(e)
    with open(os.path.join(outdir, "report.html"), "w", encoding="utf-8") as f:
        f.write(_page(title, entries, len(found), outdir))
    return entries


def _data_uri(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def _page(title, entries, total, outdir):
    """One self-contained page: the frames are embedded, so the report can be sent as a single file."""
    rows = []
    for e in entries:
        img = ""
        if e.get("png"):
            img = '<img src="%s" alt="%s frame">' % (_data_uri(os.path.join(outdir, e["png"])), html.escape(e["kind"]))
        rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                    % (html.escape(e.get("utc") or _seconds_text(e["start"])), html.escape(e["kind"]),
                       html.escape(e["text"]), img))
    more = ""
    if total > len(entries):
        more = "<p>%d more problem pictures were found; only the first %d have frames.</p>" % (total - len(entries), len(entries))
    body = "".join(rows) or "<tr><td colspan=4>No frozen or black pictures were found in this stretch.</td></tr>"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>%s</title>"
            "<style>body{font:14px system-ui,sans-serif;margin:16px;color:#111}table{border-collapse:collapse;width:100%%}"
            "td,th{border-bottom:1px solid #ddd;padding:8px;vertical-align:top;text-align:left}"
            "img{max-width:320px;border:1px solid #ccc}</style></head><body>"
            "<h1>%s</h1><p>Each picture is the frame received at that moment. It shows the problem as it arrived; "
            "it does not show where the fault began.</p>%s"
            "<table><tr><th>When</th><th>Problem</th><th>What was detected</th><th>Frame</th></tr>%s</table></body></html>"
            % (html.escape(title), html.escape(title), more, body))


def save_live(ts_bytes, workdir, result, window_seconds, root=DEFAULT_ROOT, title="Problem pictures from the live stream"):
    """Keep the stream stretch that showed a problem, with its frames and report, in its own folder under root.
    The stretch covers the last window_seconds. Its start time is taken from the clock now, so event times
    are approximate to within a fraction of a second. Returns (folder, entries)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    folder = os.path.join(root, stamp)
    os.makedirs(folder, exist_ok=True)
    src = os.path.join(folder, "stream.ts")
    with open(src, "wb") as f:
        f.write(ts_bytes)
    entries = build(src, folder, result, window_start_utc=time.time() - window_seconds, title=title)
    return folder, entries
