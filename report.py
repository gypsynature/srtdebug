"""
srtdebug report - turns a run's export (CSV or JSONL) into one self-contained
HTML dashboard: verdict, KPIs, charts and notes. No external assets, so the file
opens offline and can be sent to a customer unchanged.
"""

import calendar
import csv
import html
import html as html_mod
import itertools
import json
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time

import srtdebug as core
import thresholds

VERSION = core.VERSION
BITRATE_CV_WARN = 0.15        # bitrate variation (std dev / mean) above this is a WARN
DROPOUT_FAIL_SECONDS = 5.0    # total zero-bitrate time above this is a FAIL
TARGET_WARN_RATIO = core._T["target_warn_ratio"]
TARGET_FAIL_RATIO = core._T["target_fail_ratio"]

NUMERIC = ["elapsed_s", "bitrate_mbps", "rx_packets", "lost", "loss_pct", "retrans",
           "dropped", "belated", "rtt_ms", "buf_ms", "latency_ms", "link_bw_mbps", "flow_window",
           "av_offset_ms"]


# ------------------------------------------------------------------ data

def load_rows(path):
    rows = []
    with open(path, newline="") as f:
        items = csv.DictReader(f) if path.lower().endswith(".csv") else (
            json.loads(line) for line in f if line.strip())
        for r in items:
            try:
                row = {k: float(r[k]) for k in NUMERIC if k in r and r[k] not in ("", None)}
            except (ValueError, TypeError):
                continue
            if "elapsed_s" in row and "bitrate_mbps" in row:
                row["time"] = r.get("time", "")
                rows.append(row)
    if not rows:
        raise SystemExit(f"error: no usable samples in {path}")
    return rows


def cumulative(values, growth_only=False):
    """Total of a counter that may restart (srt reconnects). Sums each run's growth. With growth_only the
    first value is a running total from before the window, so only the growth inside the window counts."""
    total, prev = 0.0, None
    for v in values:
        if prev is None:
            if not growth_only:
                total += v
        else:
            total += v if v < prev else v - prev
        prev = v
    return total


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


STARTUP_GRACE_S = 10.0   # silence this soon after the run starts is connection set-up, not an outage


def summarize(rows, target=None, window=False, run_start=None):
    br = [r["bitrate_mbps"] for r in rows]
    elapsed = [r["elapsed_s"] for r in rows]
    steps = [b - a for a, b in zip(elapsed, elapsed[1:]) if b > a]
    interval = statistics.median(steps) if steps else 1.0
    active = [b for b in br if b >= 0.01]
    mean_active = statistics.fmean(active) if active else 0.0
    cv = (statistics.pstdev(active) / mean_active) if len(active) > 1 and mean_active else 0.0
    # silence in the first STARTUP_GRACE_S of the run is set-up (SRT filling its buffer), not an outage
    t0 = elapsed[0] if run_start is None else run_start
    startup = [r for r in rows if r["bitrate_mbps"] < 0.01 and r["elapsed_s"] - t0 < STARTUP_GRACE_S]
    dropout_s = sum(interval for r in rows if r["bitrate_mbps"] < 0.01
                    and r["elapsed_s"] - t0 >= STARTUP_GRACE_S)

    rx = cumulative([r["rx_packets"] for r in rows], window)
    lost = cumulative([r["lost"] for r in rows], window)
    retrans = cumulative([r["retrans"] for r in rows], window)
    dropped = cumulative([r["dropped"] for r in rows], window)
    loss_pct = 100.0 * lost / (rx + lost) if (rx + lost) else 0.0          # detected, incl. repaired
    unrecovered_pct = 100.0 * dropped / (rx + lost) if (rx + lost) else 0.0  # never reached the picture

    rtts = [r["rtt_ms"] for r in rows]
    bufs = [r["buf_ms"] for r in rows]
    data_mb = sum(b * interval for b in br) / 8.0  # Mbit -> MB

    reasons, verdict = [], "PASS"

    def flag(level, text):
        nonlocal verdict
        reasons.append((level, text))
        if level == "FAIL":
            verdict = "FAIL"
        elif level == "WARN" and verdict == "PASS":
            verdict = "WARN"

    if dropped > 0 and unrecovered_pct >= core.UNRECOVERED_FAIL_PCT:
        flag("FAIL", f"{dropped:,.0f} packets ({unrecovered_pct:.2f}%) were not recovered in time "
                     f"and never reached the picture")
    elif dropped > 0:
        flag("WARN", f"{dropped:,.0f} packets were not recovered in time and never reached the picture; "
                     "check the network path and the SRT latency")
    if loss_pct > 0 and dropped == 0:
        flag("INFO", f"{loss_pct:.2f}% of packets were lost in transit and all were recovered by retransmission")
    if startup:
        flag("INFO", f"{len(startup) * interval:.0f} s with no data at the start of the run "
                     f"(first {STARTUP_GRACE_S:.0f} s, while the connection settles); not counted as silence")
    if dropout_s > DROPOUT_FAIL_SECONDS:
        flag("FAIL", f"stream was down or silent for {dropout_s:.0f} s")
    elif dropout_s > 0:
        flag("WARN", f"stream was silent for {dropout_s:.0f} s")
    p95 = percentile(rtts, 95)
    if p95 >= core.RTT_BAD_MS:
        flag("FAIL", f"RTT p95 {p95:.0f} ms is above {core.RTT_BAD_MS} ms")
    elif p95 >= core.RTT_WARN_MS:
        flag("WARN", f"RTT p95 {p95:.0f} ms is above {core.RTT_WARN_MS} ms")
    # SRT headroom: the negotiated latency should cover 3 to 4 times the round trip. The receive
    # buffer is not used here: its meaning is not verified against the SRT documentation yet.
    lats = [r["latency_ms"] for r in rows if r.get("latency_ms")]
    latency = percentile(lats, 50) if lats else 0.0
    if latency and p95 * 3 > latency:
        flag("WARN", f"latency {latency:.0f} ms is under 3 x the RTT p95 ({p95:.0f} ms); raise the latency")
    if cv > BITRATE_CV_WARN:
        flag("WARN", f"bitrate varies by {cv * 100:.0f}% (std dev / mean)")
    if target and mean_active:
        ratio = mean_active / target
        if ratio < TARGET_FAIL_RATIO:
            flag("FAIL", f"average bitrate {mean_active:.2f} Mbps is under {TARGET_FAIL_RATIO:.0%} of target")
        elif ratio < TARGET_WARN_RATIO:
            flag("WARN", f"average bitrate {mean_active:.2f} Mbps is under {TARGET_WARN_RATIO:.0%} of target")
    if not reasons:
        reasons.append(("PASS", "all measurements within thresholds"))

    return {
        "samples": len(rows),
        "duration_s": elapsed[-1] - elapsed[0] + interval,
        "interval_s": interval,
        "bitrate_avg": mean_active,
        "bitrate_min": min(active) if active else 0.0,
        "bitrate_peak": max(br),
        "bitrate_cv": cv,
        "dropout_s": dropout_s,
        "latency_ms": latency,
        "rx_packets": rx,
        "lost": lost,
        "loss_pct": loss_pct,
        "retrans": retrans,
        "dropped": dropped,
        "unrecovered_pct": unrecovered_pct,
        "rtt_avg": statistics.fmean(rtts),
        "rtt_p95": p95,
        "rtt_max": max(rtts),
        "buf_avg": statistics.fmean(bufs),
        "data_mb": data_mb,
        "verdict": verdict,
        "reasons": reasons,
    }


# ------------------------------------------------------------------ svg

_uid = itertools.count(1)
RANK = {"ok": 0, "warn": 1, "bad": 2, "down": 3}
SEVERITY_TEXT = {"PASS": "Healthy", "WARN": "Degraded", "FAIL": "Failing"}
NOW_WINDOW_S = 5.0   # the header's current state looks at this much of the newest stream
ISSUE_HINTS = [
    ("packet loss", "Check the network path and the SRT latency. Loss that the receive buffer cannot recover appears here."),
    ("RTT", "Path latency is high. Check routing and congestion on the link. More SRT latency gives retransmissions more time."),
    ("silent", "The stream stopped or went quiet. Check the sender and its logs, then the link."),
    ("bitrate varies", "Output rate is unsteady. Check encoder rate control, or congestion on the link."),
    ("average bitrate", "Output is below target. Check the encoder setting and the sender's uplink capacity."),
    ("stream contents", "See Anomalies for the evidence and the suspected side."),
    ("not recovered", "Packets were lost and the retransmission came too late. They never reached the picture. Check the link and raise the SRT latency."),
    ("lost in transit", "Packets were lost on the way, and SRT's retransmission recovered every one. Harmless at this level, but it is the link working hard."),
    ("under 3 x the RTT", "SRT needs room to retransmit a lost packet before its deadline. Under three round trips leaves too little time."),
    ("NO SIGNAL", "No data has arrived for a while. The sender is not sending, or the packets are not reaching this machine."),
    ("codec change", "A stream's codec, frame rate or colour settings changed. Check the sender, since a change mid-stream can make decoders glitch."),
    ("Shadows piled", "Many pixels sit at the black floor, so shadow detail is crushed or the range tag is wrong. Check the sender's black level and range setting."),
    ("Highlights piled", "Many pixels sit at the peak value, so highlight detail is clipped. Check the source's peak level and any HDR-to-SDR conversion."),
    ("Levels below legal black", "Values fall below the black level for the signalled range. Often a full-range picture tagged as limited, so receivers clip the shadows."),
    ("Levels above legal white", "Values rise above the white level for the signalled range. Often a full-range picture tagged as limited, so receivers clip the highlights."),
    ("loudness", "Audio loudness is measured against the EBU R128 target. Adjust the sender's audio level to bring it onto the target."),
    ("PTS interval", "Presentation timestamps stopped repeating for a while. Usually a stall at the sender or a gap in the network."),
]

TR_HINTS = {
    "Sync byte errors": "Packets are not starting where the 188-byte framing says. Usually corruption on the link, or a sender that is not sending MPEG-TS cleanly.",
    "PAT timeout (500 ms)": "The Program Association Table should arrive at least every 500 ms. Missing it means the decoder loses track of the programme.",
    "PMT timeout (500 ms)": "The Program Map Table should arrive at least every 500 ms. Missing it means the decoder cannot find the video and audio PIDs.",
    "PID timeout (500 ms)": "A stream PID has gone quiet for over 500 ms. Check the sender for a stall or a dropped stream.",
    "Transport error indicator set": "A packet arrived with its transport error flag set, so something upstream flagged it as corrupt.",
    "PCR interval over 40 ms": "The clock reference should repeat at least every 40 ms (TR 101 290). Longer gaps make the decoder's clock drift.",
    "PTS interval over 700 ms": "Presentation timestamps should repeat at least every 700 ms on each stream. A longer gap usually means a stall at the sender or a network gap.",
    "SDT timeout (2 s)": "The Service Description Table should arrive every 2 s. Missing it affects only the service name, not the picture.",
    "Unreferenced PID": "A PID carries data that no PMT points to. Harmless if intended, such as private data, but worth checking.",
}
OWNER_CLASS = {"sender": "own-sender", "network": "own-network", "receiver": "own-receiver", "link": "own-link"}


def esc(value):
    return html.escape(str(value))


def _colour(value, warn, bad):
    return "#16a34a" if value < warn else "#d97706" if value < bad else "#dc2626"


def _thin(values, n=240):
    step = max(1, len(values) // n)
    return values[::step]


def sparkline_svg(values, colour):
    values = _thin(values)
    if len(values) < 2:
        return ""
    W, H = 160, 44
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = [(i * W / (n - 1), H - 4 - (v - lo) / span * (H - 10)) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,{H} " + line + f" {W},{H}"
    uid = f"sp{next(_uid)}"
    return f"""<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">
  <defs><linearGradient id="{uid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="{colour}" stop-opacity=".25"/><stop offset="1" stop-color="{colour}" stop-opacity="0"/></linearGradient></defs>
  <polygon points="{area}" fill="url(#{uid})"/>
  <polyline points="{line}" fill="none" stroke="{colour}" stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>"""


def nice_top(v):
    """Round a value up to a tidy axis maximum (1, 2, 2.5, 5 or 10 times a power of ten)."""
    if v <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(v))
    for step in (1, 2, 2.5, 5, 10):
        if step * mag >= v:
            return step * mag
    return 10 * mag


def line_svg(xs, ys, color, unit, hlines=(), ymax=None, height=240):
    W, pl, pr, pt, pb = 820, 56, 16, 14, 30
    if not xs:
        return "<p class='muted'>No data.</p>"
    x0, x1 = min(xs), max(xs)
    top = ymax or nice_top(max(max(ys), max((v for v, _, _ in hlines), default=0), 0.1) * 1.08)
    decimals = 2 if top < 1 else 1 if top < 10 else 0
    xspan = (x1 - x0) or 1.0

    def X(x):
        return pl + (x - x0) / xspan * (W - pl - pr)

    def Y(y):
        return height - pb - (max(0.0, min(y, top)) / top) * (height - pt - pb)

    step = max(1, len(xs) // 1000)
    pts = [(X(x), Y(y)) for x, y in zip(xs[::step], ys[::step])]
    base = height - pb
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pts[0][0]:.1f},{base:.1f} " + poly + f" {pts[-1][0]:.1f},{base:.1f}"
    uid = f"lg{next(_uid)}"
    grid, ticks, marks = [], [], []
    for i in range(5):
        v = top * i / 4
        y = Y(v)
        grid.append(f'<line x1="{pl}" x2="{W - pr}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        ticks.append(f'<text x="{pl - 8}" y="{y + 4:.1f}" text-anchor="end" class="tick">{v:,.{decimals}f}</text>')
    for i in range(5):
        x = x0 + xspan * i / 4
        if xspan < 60:  # short session: fractional seconds, so the labels never repeat
            label = f"{x:.1f}s"
        else:
            r = round(x)
            label = f"{r // 60:02d}:{r % 60:02d}"
        ticks.append(f'<text x="{X(x):.1f}" y="{height - 8}" text-anchor="middle" class="tick">{label}</text>')
    for i, (v, text, col) in enumerate(hlines):
        y = Y(v)
        marks.append(f'<line x1="{pl}" x2="{W - pr}" y1="{y:.1f}" y2="{y:.1f}" stroke="{col}" '
                     f'stroke-dasharray="6 5" stroke-width="1.5"/>')
        if i == 0:
            marks.append(f'<text x="{pl + 6}" y="{y + 13:.1f}" text-anchor="start" class="mark" '
                         f'fill="{col}">{esc(text)}</text>')
        else:
            marks.append(f'<text x="{W - pr - 4}" y="{y - 6:.1f}" text-anchor="end" class="mark" '
                         f'fill="{col}">{esc(text)}</text>')
    return f"""<svg viewBox="0 0 {W} {height}" class="chart" role="img" aria-label="{esc(unit)} over time">
  <defs><linearGradient id="{uid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="{color}" stop-opacity=".22"/><stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>
  {''.join(grid)}
  <polygon points="{area}" fill="url(#{uid})"/>
  <polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  {''.join(marks)}
  {''.join(ticks)}
  <text x="{pl}" y="10" class="axis">{esc(unit)}</text>
</svg>"""


def _status(r):
    if r["bitrate_mbps"] < 0.01:
        return "down"
    if r["loss_pct"] >= core.LOSS_BAD_PCT or r["rtt_ms"] >= core.RTT_BAD_MS:
        return "bad"
    if r["loss_pct"] >= core.LOSS_WARN_PCT or r["rtt_ms"] >= core.RTT_WARN_MS:
        return "warn"
    return "ok"


def status_strip(rows, bucket_s=5.0, target=None):
    """One cell per bucket_s seconds, counted back from the newest sample. Each cell gets the same verdict
    the header gives its window (summarize), so the newest cell always matches the header's current state.
    Recovered loss is information and does not colour a cell."""
    if not rows:
        return "", bucket_s
    last_t = rows[-1]["elapsed_s"]
    buckets = {}
    for r in rows:
        buckets.setdefault(int((last_t - r["elapsed_s"]) // bucket_s), []).append(r)
    out = []
    for k in range(max(buckets), -1, -1):            # oldest first, newest last
        end_s = last_t - k * bucket_s
        start_s = end_s - bucket_s
        part = buckets.get(k)
        if not part:
            out.append(f'<i class="cell none" title="{esc(core.fmt_hms(start_s))}: no samples"></i>')
            continue
        res = summarize(part, target, window=True, run_start=rows[0]["elapsed_s"])
        reasons = [r for r in res["reasons"] if r[0] != "PASS"]
        if res["verdict"] == "FAIL" and any("down or silent" in r[1] for r in reasons):
            worst = "down"
        else:
            worst = {"PASS": "ok", "WARN": "warn", "FAIL": "bad"}[res["verdict"]]
        tip = f"{core.fmt_hms(max(start_s, 0))} to {core.fmt_hms(end_s)}: {SEVERITY_TEXT[res['verdict']]}"
        if reasons:
            tip += " - " + "; ".join(r[1] for r in reasons)
        out.append(f'<i class="cell {worst}" title="{esc(tip)}"></i>')
    return f'<div class="strip" role="img" aria-label="status over time">{"".join(out)}</div>', bucket_s





# ------------------------------------------------------------------ html

CSS = """
:root {
  --bg:#f7f7f5; --card:#fdfdfc; --soft:#f1f1ee; --ink:#2b2e37; --muted:#7b808c; --line:#e7e7e2;
  --track:#ebebe6; --accent:#7b70c2; --accent-soft:#f0eef8;
  --ok:#4f9b78; --warn:#b98a45; --bad:#c26d6d; --down:#7b808c; --info:#5b8ea2;
  --own-sender:#7c3aed; --own-network:#2563eb; --own-receiver:#0d9488; --own-link:#64748b;
}
:root[data-theme="dark"] {
  --bg:#15171d; --card:#1b1e26; --soft:#21252e; --ink:#d9dce4; --muted:#8c92a2; --line:#2a2f3a;
  --track:#262b35; --accent:#a79ef0; --accent-soft:#25243a;
  --ok:#6cba95; --warn:#d6ab68; --bad:#d98c8c; --down:#8c92a2; --info:#7fb4c4;
  --own-sender:#c4b5fd; --own-network:#93c5fd; --own-receiver:#5eead4; --own-link:#cbd5e1;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin:0; background:var(--bg); color:var(--ink);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
a { color: var(--accent); text-decoration: none; }
.top { position: sticky; top: 0; z-index: 5; background: var(--card); border-bottom: 1px solid var(--line); }
.top-in { max-width: 1120px; margin: 0 auto; padding: 0 20px; height: 52px; display:flex; align-items:center; gap:14px; }
.brand { display:flex; align-items:center; gap:8px; font-weight: 700; letter-spacing: .01em; }
.brand svg { width: 22px; height: 22px; }
.crumb { color: var(--muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.spacer { flex: 1; }
.btn { border: 1px solid var(--line); background: var(--soft); color: var(--ink); border-radius: 8px;
       padding: 6px 12px; font: inherit; font-size: 13px; cursor: pointer; }
.btn:hover { border-color: var(--accent); }
.wrap { max-width: 1120px; margin: 0 auto; padding: 24px 20px 56px; }
.hero { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; flex-wrap: wrap; margin-bottom: 14px; }
h1 { font-size: 24px; line-height: 1.25; margin: 0 0 8px; letter-spacing: -.01em; }
.chips { display:flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 12px; color: var(--muted); background: var(--soft); border: 1px solid var(--line);
        border-radius: 999px; padding: 2px 10px; white-space: nowrap; }
.chip b { color: var(--ink); font-weight: 600; }
.status { display:flex; align-items:center; gap: 12px; background: var(--card); border: 1px solid var(--line);
          border-radius: 12px; padding: 10px 14px; }
.status .big { font-size: 18px; font-weight: 700; }
.status .sub { color: var(--muted); font-size: 12px; }
.dotlg { width: 14px; height: 14px; border-radius: 50%; }
.PASS .dotlg { background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 20%, transparent); }
.WARN .dotlg { background: var(--warn); box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 20%, transparent); }
.FAIL .dotlg { background: var(--bad); box-shadow: 0 0 0 4px color-mix(in srgb, var(--bad) 20%, transparent); }
.PASS .big { color: var(--ok); } .WARN .big { color: var(--warn); } .FAIL .big { color: var(--bad); }
nav.tabs { display:flex; gap: 4px; border-bottom: 1px solid var(--line); margin: 6px 0 22px; overflow-x: auto; scrollbar-width: none;
  position: sticky; top: 52px; z-index: 4; background: var(--bg); padding-top: 4px; }
nav.tabs::-webkit-scrollbar { display: none; }
/* inside the control panel's Live frame the frame is the top of the page: no separate header, and the tabs
   stick to the frame's top edge instead of 52 px below a header that is not there */
/* inside the control panel the page title is not needed, but the live video chip lives in the header:
   keep it, take it out of the header's layout, and place it just under the tab bar */
/* inside the control panel: a slim sticky bar holds only the live video chip; the tabs stick below it.
   Content scrolls under the bar, so nothing is ever drawn on top of the dashboard. */
.embedded .top { position: sticky; top: 0; z-index: 5; height: 44px; margin: 0; padding: 0 16px 0 0; border: 0;
  background: var(--bg); display: flex; align-items: center; justify-content: flex-end; }
.embedded .top-in { display: flex; align-items: center; justify-content: flex-end; width: 100%; height: 100%; }
.embedded .top-in > *:not(.viewer-chip):not(.bar-health) { display: none !important; }
.embedded .top-in > .bar-health { display: inline-flex; }
.embedded nav.tabs { top: 44px; }
.stack { display:grid; gap: 16px; }
/* the live video sits in the page flow above the content, so it pushes the dashboard down instead of covering it */
.viewer-wrap { position: static; margin: 0 0 16px; }
.viewer-wrap.min .viewer { display: none; }
.viewer-wrap.popped { display: none; }
.viewer-chip { display: inline-flex; align-items: center; background: var(--soft); color: var(--ink); border: 1px solid var(--line);
  border-radius: 999px; padding: 7px 12px; font: 600 13px/1 system-ui, sans-serif; cursor: pointer; white-space: nowrap; }
.viewer-chip:hover { border-color: var(--accent); }
.viewer-chip .lbl-pop { display: none; }
.viewer-chip.popped { color: var(--accent); border-color: var(--accent); }
.viewer-chip.popped .lbl-pop { display: inline; }
.viewer-chip.popped .lbl-min { display: none; }
.viewer { width: 100%; max-width: 760px; padding: 10px 12px 10px; background: var(--card);
          border: 1px solid var(--line); border-radius: 14px; }
.kpi, .panel { position: relative; }
.vbox { position: relative; }
/* the meter sits below the picture, never over it: the player's own controls (full screen, mute) stay clear */
.meter { margin-top: 8px; background: rgba(0,0,0,.62); color: #e5e7eb; border-radius: 8px;
         padding: 6px 8px; font: 600 11px/1.2 system-ui, sans-serif; cursor: pointer; }
.meter .mrow { display: flex; align-items: center; gap: 6px; margin: 2px 0; }
.meter .mrow b { width: 10px; }
.meter .track { flex: 1; height: 6px; background: rgba(255,255,255,.18); border-radius: 3px; overflow: hidden; }
.meter .fill { height: 100%; width: 0; background: #22c55e; }
.meter .fill.y { background: #eab308; }
.meter .fill.r { background: #ef4444; }
.meter .mdb { min-width: 58px; text-align: right; font-variant-numeric: tabular-nums; }
.meter .mstate { opacity: .85; font-weight: 500; }
.banner { padding: 10px 14px; border-radius: 10px; margin: 0 0 14px; font-weight: 600; border: 1px solid; }
td.ok { color: var(--ok, #16a34a); font-weight: 600; }
td.warn { color: var(--warn, #d97706); font-weight: 600; }
td.bad { color: var(--bad, #dc2626); font-weight: 600; }
.banner.bad { color: var(--bad); border-color: var(--bad); background: color-mix(in srgb, var(--bad) 12%, transparent); }
.banner.warn { color: var(--warn); border-color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, transparent); }
.tile-actions { position: absolute; top: 8px; right: 8px; display: flex; gap: 4px; opacity: 0; transition: opacity .15s; }
.panel > .tile-actions { top: auto; bottom: 8px; }
.kpi:hover > .tile-actions, .panel:hover > .tile-actions, .tile-actions:focus-within { opacity: 1; }
@media (hover: none) { .tile-actions { opacity: 1; } }
@media print { .tile-actions { display: none !important; } }
.only-video .top, .only-video main { display: none !important; }
.only-video .viewer-wrap { position: static; width: auto; max-width: none; padding: 0; margin: 0; box-shadow: none; }
.only-video .viewer-wrap.min .viewer { display: block; }
.only-video .viewer-chip { display: none !important; }
.only-video .viewer { width: 100%; border: 0; border-radius: 0; padding: 6px; }
.only-video .viewer video { height: calc(100vh - 210px); min-height: 160px; max-height: none; object-fit: contain; }
.vhead { display:flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; }
.vt { font-weight: 650; font-size: 15px; }
.viewer video { width: 100%; max-height: 203px; background: #000; border-radius: 10px; display: block; }
.live { display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:700; letter-spacing:.06em;
        color: var(--bad); background: color-mix(in srgb, var(--bad) 12%, transparent); padding: 3px 10px; border-radius: 999px; }
.live i { width: 8px; height: 8px; border-radius: 50%; background: var(--bad); animation: livepulse 1.6s ease-in-out infinite; }
#live-stamp { font-weight: 500; letter-spacing: 0; color: var(--muted); }
@keyframes livepulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }
nav.tabs a { color: var(--muted); padding: 10px 12px; border-bottom: 2px solid transparent; margin-bottom: -1px; white-space: nowrap; font-weight: 500; }
nav.tabs a:hover { color: var(--ink); }
nav.tabs button { background: none; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px;
  color: var(--muted); font: inherit; font-weight: 500; padding: 10px 12px; cursor: pointer; white-space: nowrap; }
nav.tabs button:hover { color: var(--ink); }
nav.tabs button.on { color: var(--ink); border-bottom-color: var(--accent); }
[hidden] { display: none !important; }
.exports { display: flex; gap: 6px; }
@media (max-width: 560px) {
  .top-in { height: auto; padding: 8px 16px; flex-wrap: wrap; row-gap: 8px; }
  .crumb { display: none; }
  .spacer { display: none; }
  .exports { flex-wrap: wrap; }
}
.btn.sm { padding: 4px 10px; font-size: 12px; }
.ev-body { min-width: 0; }
.ev-body summary { cursor: pointer; }
.ev-body summary:hover { color: var(--accent); }
.ev-table { width: 100%; border-collapse: collapse; margin: 8px 0 4px; font-size: 12.5px; }
.ev-table th, .ev-table td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--line);
  font-variant-numeric: tabular-nums; }
.ev-table th { color: var(--muted); font-weight: 500; }
.chart { position: relative; min-width: 0; }
.chart canvas.chart-canvas { display: block; cursor: crosshair; user-select: none; -webkit-user-select: none; }
.chart-tools { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 6px; flex-wrap: wrap; }
.panel > .chart-tools, .panel .chart-tools { padding-right: 76px; }  /* room for the Copy button in the panel corner */
.zoom-bar { position: sticky; z-index: 8; display: flex; justify-content: space-between; align-items: center; gap: 12px;
  margin: 0 0 14px; padding: 8px 12px; border: 1px solid var(--accent); border-radius: 10px; background: var(--card);
  box-shadow: 0 2px 8px rgba(0,0,0,.08); font-size: 13px; color: var(--ink); }
.zoom-bar[hidden] { display: none; }
.cfg td { vertical-align: top; }
.cfg td:nth-child(4), .cfg td:nth-child(5) { color: var(--muted); font-size: 13px; }
@media print { [data-tab] { display: block !important; } .exports, [data-png] { display: none !important; } }
section { scroll-margin-top: 70px; margin-bottom: 26px; }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
.xpop { position: fixed; inset: 0; z-index: 50; background: color-mix(in srgb, var(--bg) 70%, transparent);
  backdrop-filter: blur(3px); display: flex; align-items: flex-start; justify-content: center; padding: 5vh 16px; overflow-y: auto; }
.xbtn { position: absolute; top: 10px; right: 10px; }
.xbtn-2 { position: absolute; top: 10px; right: 116px; }
/* panels without a header row keep a strip at the top for their Expand and Pop out buttons */
.panel:has(> .xbtn) { padding-top: 44px; }
.pop-btn { margin-left: 6px; }
.kpi .pop-btn.kpi-pop { position: absolute; bottom: 6px; right: 6px; padding: 1px 7px; font-size: 11px; opacity: .75; }
.kpi .pop-btn.kpi-pop:hover { opacity: 1; }
.kpi:has(.kpi-pop) { padding-bottom: 30px; }
.pop-win { position: fixed; z-index: 46; width: 380px; max-width: calc(100vw - 32px); background: var(--card);
  border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 14px 40px rgba(0,0,0,.28); display: flex; flex-direction: column; }
.pop-win.pop-kpi { width: 240px; }
.pop-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 7px 10px 7px 12px;
  border-bottom: 1px solid var(--line); cursor: move; font-size: 12.5px; font-weight: 600; user-select: none; touch-action: none; }
.pop-head span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pop-x { border: 0; background: none; color: var(--muted); font-size: 18px; line-height: 1; padding: 0 4px; cursor: pointer; }
.pop-x:hover { color: var(--ink); }
.pop-body { padding: 10px 12px; overflow: auto; max-height: 60vh; }
.pop-body .panel { margin: 0; padding: 0; border: 0; box-shadow: none; background: none; }
.pop-body .tablewrap { overflow-x: auto; }
@media print { .pop-win { display: none !important; } }
.xpop-box { width: min(1100px, 100%); }
.xpop-head { display: flex; justify-content: flex-end; margin-bottom: 8px; }
.xpop-panel { margin: 0; max-height: none; overflow: visible; box-shadow: 0 18px 50px rgba(0,0,0,.25); }
.xpop-panel .tablewrap { overflow: visible; }
@media print { .xpop { display: none !important; } }
.ph { display:flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
h2 { font-size: 15px; margin: 0; font-weight: 650; }
h3 { font-size: 13px; margin: 0; font-weight: 650; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.muted { color: var(--muted); }
.small { font-size: 12px; }
.grid2 { display:grid; grid-template-columns: 1.1fr 1fr; gap: 16px; }
@media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } }
.kpis { display:grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
@media (max-width: 860px) { .kpis { grid-template-columns: repeat(2, 1fr); } }
.kpi { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px 10px; position: relative; overflow: hidden; }
.kpi .kl { color: var(--muted); font-size: 12px; font-weight: 600; }
.kpi .kv { font-size: 24px; font-weight: 680; margin-top: 2px; letter-spacing: -.01em; font-variant-numeric: tabular-nums; }
.kpi .ks { color: var(--muted); font-size: 12px; margin-top: 2px; min-height: 18px; }
.kpi .spark { display:block; width: 100%; height: 40px; margin-top: 6px; }
.kpi.warn { border-color: color-mix(in srgb, var(--warn) 55%, var(--line)); }
.kpi.bad { border-color: color-mix(in srgb, var(--bad) 55%, var(--line)); }
.kpi.bad .kv { color: var(--bad); } .kpi.warn .kv { color: var(--warn); }
.issues { list-style: none; margin: 0; padding: 0; }
.issue { display:flex; gap: 12px; align-items: flex-start; padding: 12px 0; border-top: 1px solid var(--line); }
.issue:first-child { border-top: none; padding-top: 2px; }
.issue .it { font-weight: 600; }
.issue .ih { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
.dot { width: 9px; height: 9px; border-radius: 50%; margin-top: 6px; flex: none; }
.dot.PASS, .dot.ok { background: var(--ok); } .dot.WARN, .dot.warn { background: var(--warn); }
.dot.FAIL, .dot.bad { background: var(--bad); } .dot.info { background: var(--info); }
.pill { display:inline-block; font-size: 11px; font-weight: 700; letter-spacing: .03em; padding: 2px 8px;
        border-radius: 999px; background: var(--soft); color: var(--muted); border: 1px solid var(--line); white-space: nowrap; }
.pill.PASS, .pill.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, var(--line)); }
.pill.WARN, .pill.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--line)); }
.pill.FAIL, .pill.bad, .pill.fail { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }
.pill.info { color: var(--info); }
.pill.down { color: var(--down); }
.pill.likely { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, var(--line)); }
.pill.possible { color: var(--muted); }
.pill.own-sender { color: var(--own-sender); } .pill.own-network { color: var(--own-network); }
.pill.own-receiver { color: var(--own-receiver); } .pill.own-link { color: var(--own-link); }
.pill.tech { color: var(--accent); background: var(--accent-soft); border-color: transparent; }
.empty { display:flex; gap: 10px; align-items:center; padding: 14px; border-radius: 10px; background: var(--soft); color: var(--muted); }
.empty .tick-ok { color: var(--ok); font-weight: 700; }
.gauge { margin: 0; text-align: center; }
.gauge svg { width: 100%; max-width: 240px; height: auto; }
.gauge figcaption { display:flex; flex-direction: column; margin-top: -6px; }
.gauge figcaption b { font-size: 13px; }
.gauge figcaption span { color: var(--muted); font-size: 12px; }
.gv { font: 650 28px system-ui, sans-serif; fill: var(--ink); }
.gu { font: 12px system-ui, sans-serif; fill: var(--muted); }
.gt { font: 10.5px system-ui, sans-serif; fill: var(--muted); }
.track { stroke: var(--track); }
.chart { width:100%; height:auto; display:block; }
.grid { stroke: var(--line); stroke-width: 1; }
.tick { font: 11px system-ui, sans-serif; fill: var(--muted); }
.axis { font: 600 11px system-ui, sans-serif; fill: var(--muted); }
.mark { font: 600 11px system-ui, sans-serif; }
.strip { display:flex; gap: 2px; height: 34px; overflow-x: auto; padding-bottom: 2px; }
.strip .cell { flex: 0 0 12px; height: 100%; border-radius: 2px; background: var(--ok); opacity: .9; }
.strip .cell.none { background: var(--line); opacity: .6; }
.strip .cell.warn { background: var(--warn); } .strip .cell.bad { background: var(--bad); }
.strip .cell.down { background: var(--down); }
.legend { display:flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 12px; margin-top: 10px; }
.legend i { display:inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
.tag { font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 6px; background: var(--soft, #eef); color: var(--accent, #6c5fc7); margin-left: 6px; }
th, td { text-align: left; padding: 9px 10px; border-top: 1px solid var(--line); vertical-align: top; }
thead th { border-top: none; color: var(--muted); font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: .05em; }
td.num, th.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.tablewrap { overflow-x: auto; }
.status-link { cursor: pointer; }
.tl-mark.bad { background: var(--bad, #dc2626); }
.tl-mark.info { background: var(--accent, #6c5fc7); }
.iss-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.iss-chip { display: inline-flex; align-items: center; gap: 7px; padding: 6px 11px; border-radius: 999px;
  background: var(--soft, #f1f2f7); border: 1px solid var(--line, #e4e6ee); font-size: 13px; color: var(--ink); }
.status-link:hover, .status-link:focus-visible { outline: 2px solid var(--accent, #6c5fc7); outline-offset: 2px; }
.tl { position: relative; margin: 14px 16px 6px; padding-bottom: 22px; }
.tl-marks { position: relative; height: 16px; background: var(--soft, #f1f2f7); border-radius: 4px; }
.tl-mark { position: absolute; top: 0; bottom: 0; min-width: 3px; background: var(--warn, #d97706); border-radius: 3px; }
.tl-axis { position: relative; height: 18px; border-bottom: 1px solid var(--line, #e4e6ee); }
.tl-tick { position: absolute; top: 0; height: 6px; width: 1px; background: var(--muted, #687083); }
.tl-lbl { position: absolute; top: 7px; transform: translateX(-50%); font: 11px ui-monospace, Menlo, Consolas, monospace; color: var(--muted, #687083); white-space: nowrap; }
.chipset { display:flex; flex-wrap: wrap; gap: 4px; }
.tech { font-size: 11.5px; padding: 1px 7px; border-radius: 6px; background: var(--accent-soft); color: var(--accent); font-weight: 600; white-space: nowrap; }
.tech.hdr { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
.tech.sdr { background: var(--soft); color: var(--muted); }
.tech.warnset { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
.mux { display:flex; height: 14px; border-radius: 999px; overflow: hidden; background: var(--soft); }
.mux span { display:block; height: 100%; }
.mux .v { background: var(--accent); } .mux .a { background: var(--info); } .mux .o { background: var(--line); }
.mux-legend { display:flex; gap: 16px; flex-wrap: wrap; font-size: 12.5px; color: var(--muted); margin-top: 8px; }
.mux-legend i { display:inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.kv-grid { display:grid; grid-template-columns: 180px 1fr; gap: 0; font-size: 13px; }
.kv-grid dt, .kv-grid dd { margin: 0; padding: 8px 0; border-top: 1px solid var(--line); }
.kv-grid dt { color: var(--muted); }
.kv-grid dd { font-variant-numeric: tabular-nums; word-break: break-word; }
.timeline { list-style: none; margin: 0; padding: 0; position: relative; }
.timeline li { display:grid; grid-template-columns: 84px 12px 1fr; gap: 10px; padding: 7px 0; align-items: start; font-size: 13px; }
.timeline .when { color: var(--muted); font-variant-numeric: tabular-nums; }
.timeline .tdot { width: 9px; height: 9px; border-radius: 50%; margin-top: 6px; }
.timeline .tdot.ok { background: var(--ok); } .timeline .tdot.warn { background: var(--warn); }
.timeline .tdot.bad { background: var(--bad); } .timeline .tdot.info { background: var(--info); }
/* quieter reading: more air, lighter rules, numbers that line up, less text competing for attention */
.tr-table { font-size: 13.5px; }
.tr-table th { font-weight: 600; font-size: 12px; color: var(--muted); letter-spacing: .01em; white-space: nowrap; }
.tr-table td { padding: 11px 12px; vertical-align: top; border-top: 1px solid var(--line); }
.tr-table td:nth-child(1), .tr-table td:nth-child(3), .tr-table td:nth-child(5), .tr-table td:nth-child(6) { white-space: nowrap; }
.tr-table td:nth-child(3) { font-variant-numeric: tabular-nums; font-weight: 600; }
.tr-table td:nth-child(4) { color: var(--muted); font-size: 13px; min-width: 200px; }
.tr-table td:nth-child(5), .tr-table td:nth-child(6) { color: var(--muted); font-variant-numeric: tabular-nums; font-size: 12.5px; }
.tr-table tbody tr { transition: background .12s; }
.tr-table tbody tr:hover { background: color-mix(in srgb, var(--accent) 5%, transparent); }
.soft-note { color: var(--muted); font-size: 13px; margin: 12px 14px 4px; line-height: 1.5; }
.bar-health { display: inline-flex; align-items: center; gap: 7px; padding: 5px 11px; border-radius: 999px;
  font: 600 13px/1 system-ui, sans-serif; background: var(--soft); color: var(--ink); border: 1px solid var(--line);
  margin-right: 8px; white-space: nowrap; }
.spark-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.spark-head { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; margin-bottom: 6px; }
.spark-svg { width: 100%; height: 110px; background: var(--soft); border-radius: 10px; display: block; }
.spark > .muted { margin-top: 6px; line-height: 1.45; }
@media (max-width: 640px) { .spark-row { grid-template-columns: 1fr; } }
/* layered, shaded surfaces: panels sit a step above the page, tables have a tinted header, and rules are light */
.panel, .kpi, .viewer, .spark-svg {
  box-shadow: 0 1px 2px rgba(16,18,28,.04), 0 8px 22px rgba(16,18,28,.05); }
.panel { background: linear-gradient(180deg, var(--card) 0%, color-mix(in srgb, var(--card) 97%, var(--soft)) 100%); }
table thead th { background: color-mix(in srgb, var(--soft) 80%, var(--card)); }
.ph h2, .ph h3 { color: var(--ink); letter-spacing: -.005em; }
/* result pills: colour sits on the result only, with its words, so it never relies on colour alone */
.res { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 4px; padding: 3px 10px; border-radius: 999px;
  font-weight: 600; font-size: 12.5px; line-height: 1.4; }
.res.ok { color: var(--ok); background: color-mix(in srgb, var(--ok) 12%, transparent); }
.res.warn { color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, transparent); }
.res.bad { color: var(--bad); background: color-mix(in srgb, var(--bad) 12%, transparent); }
.res.neutral { color: var(--muted); background: var(--soft); border: 1px solid var(--line); }
.tag-early { display: inline-block; margin-left: 6px; padding: 2px 8px; border-radius: 999px; font-size: 11.5px;
  font-weight: 600; color: var(--muted); background: var(--soft); border: 1px solid var(--line); vertical-align: 1px; }
button.bar-health { cursor: pointer; }
button.bar-health:hover { border-color: var(--accent); }
.bar-health::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--down); }
.bar-health.PASS::before { background: var(--ok); } .bar-health.WARN::before { background: var(--warn); }
.bar-health.FAIL::before { background: var(--bad); }
.bar-health.PASS { color: var(--ok); } .bar-health.WARN { color: var(--warn); } .bar-health.FAIL { color: var(--bad); }
footer { margin-top: 28px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); padding-top: 14px; }
@media print {
  :root, :root[data-theme] {
    --bg:#ffffff; --card:#ffffff; --soft:#f4f5f8; --ink:#1c2030; --muted:#5b6172; --line:#dfe2ea;
    --track:#e8eaf1; --accent:#5b4fc0; --accent-soft:#ece9fb;
    --ok:#15803d; --warn:#b45309; --bad:#b91c1c; --down:#4b5563; --info:#0e7490;
    --own-sender:#6d28d9; --own-network:#1d4ed8; --own-receiver:#0f766e; --own-link:#475569;
  }
  body { background: #fff; }
  .top, nav.tabs, .btn { display: none; }
  .wrap { padding: 0; max-width: none; }
  .panel, .kpi, section { break-inside: avoid; }
  .grid2 { grid-template-columns: 1fr 1fr; }
  .kpis { grid-template-columns: repeat(4, 1fr); }
}
"""


TS_PAYLOAD_RATIO = 184 / 188   # see tsanalyze: stream rate to codec rate


def _kpi(label, value, sub="", spark="", tone=""):
    return (f'<div class="kpi {tone}"><div class="kl">{esc(label)}</div>'
            f'<div class="kv">{value}</div><div class="ks">{esc(sub)}</div>{spark}</div>')


def _pill(text, cls=""):
    return f'<span class="pill {cls}">{esc(text)}</span>'


def issue_hint(text):
    for key, hint in ISSUE_HINTS:
        if key in text:
            return hint
    return ""


def stream_chips(detail, kind):
    """Turn the ffprobe detail line into scannable chips: chroma, bit depth, primaries, HDR, size."""
    if not detail:
        return '<span class="muted">-</span>'
    chips = []
    m = re.search(r"4:[0-4]:[0-4]|4:4:4", detail)
    if m:
        chips.append(f'<span class="tech">{esc(m.group(0))}</span>')
    m = re.search(r"(\d+)-bit", detail)
    if m:
        chips.append(f'<span class="tech">{esc(m.group(1))}-bit</span>')
    if "BT.2020" in detail:
        chips.append('<span class="tech">BT.2020</span>')
    elif "BT.709" in detail:
        chips.append('<span class="tech sdr">BT.709</span>')
    if "PQ" in detail or "HLG" in detail or "HDR" in detail:
        chips.append('<span class="tech hdr">HDR</span>')
    m = re.search(r"\d{3,5}x\d{3,5}", detail)
    if m:
        chips.append(f'<span class="tech sdr">{esc(m.group(0))}</span>')
    if "not signalled" in detail:
        chips.append('<span class="tech warnset">colour not signalled</span>')
    if kind == "audio" and detail and not chips:
        chips.append(f'<span class="tech sdr">{esc(detail)}</span>')
    return f'<div class="chipset">{"".join(chips)}</div>' if chips else '<span class="muted">-</span>'


def mux_composition(p):
    mux = p.get("mux_avg") or 0
    if mux <= 0:
        return ""
    vr = p.get("video_recent", p.get("video_avg") or 0)
    ar = p.get("audio_recent", p.get("audio_avg") or 0)
    mux = p.get("mux_recent", mux)
    v = min(1.0, vr / mux) if mux else 0.0
    a = min(1.0 - v, ar / mux) if mux else 0.0
    o = max(0.0, 1.0 - v - a)
    return f"""<div class="mux" role="img" aria-label="mux composition">
  <span class="v" style="width:{v * 100:.1f}%"></span><span class="a" style="width:{a * 100:.1f}%"></span><span class="o" style="width:{o * 100:.1f}%"></span></div>
<div class="mux-legend">
  <span><i class="v"></i>Video {vr:.2f} Mbps ({v * 100:.0f}%)</span>
  <span><i class="a"></i>Audio {ar * 1000:,.0f} kbps ({a * 100:.0f}%)</span>
  <span><i class="o"></i>Other and null {o * 100:.0f}%</span>
  <span>Mux {mux:.2f} Mbps, null packets {p.get('null_avg', 0):.1f}%</span></div>"""


def _codec_of(p, kind):
    """Name of the first video or audio stream's codec, e.g. 'H.264/AVC High', from the probe
    when it has one, else the PMT stream type."""
    for st in p.get("streams") or []:
        if st.get("kind") == kind:
            for label, value in st.get("fields") or []:
                if label == "Codec" and value:
                    return value
            return st.get("codec") or ""
    return ""


def _field_of(p, kind, label):
    """One value from the probe fields of the first stream of this kind, e.g. the 'Format' row."""
    for st in p.get("streams") or []:
        if st.get("kind") == kind:
            for k, value in st.get("fields") or []:
                if k == label:
                    return value
            return ""
    return ""


def payload_section(p):
    """Stream contents: composition, integrity, codec chips."""
    rm = p.get("rate_mode")
    vcodec = _codec_of(p, "video") or "video"
    vformat = _field_of(p, "video", "Format")
    acodec = _codec_of(p, "audio") or "audio"
    rm_val = rm["mode"] if rm else "not enough data"
    rm_sub = (f"spread {rm['spread_pct']:.0f}% of the median (10th to 90th percentile); "
              "how the rate behaves on the wire, not the encoder setting") if rm else "needs 10 s of data"
    cards = "".join([
        _kpi("Mux rate", f"{p.get('mux_recent', p['mux_avg']):.2f} <small>Mbps</small>",
             f"last 30 s · whole run {p['mux_avg']:.2f}; peak {p['mux_peak']:.2f}"),
        _kpi("Rate mode", rm_val, rm_sub),
        _kpi(f"Video · {esc(vcodec)}",
             f"≈{p.get('video_recent', p['video_avg']) * TS_PAYLOAD_RATIO:.2f} <small>Mbps</small>",
             f"{esc(vformat) + ' · ' if vformat else ''}last 30 s, stream "
             f"{p.get('video_recent', p['video_avg']):.2f} incl. TS headers · whole run {p['video_avg']:.2f}"),
        _kpi(f"Audio · {esc(acodec)}",
             f"≈{p.get('audio_recent', p['audio_avg']) * TS_PAYLOAD_RATIO * 1000:,.0f} <small>kbps</small>",
             f"last 30 s, stream {p.get('audio_recent', p['audio_avg']) * 1000:,.0f} kbps incl. TS headers · "
             f"whole run {p['audio_avg'] * 1000:,.0f}"),
        _kpi("Null packets", f"{p['null_avg']:.1f}%", "average share",
             tone="warn" if p["null_avg"] > 5 else ""),
        _kpi("Continuity errors", f"{p['cc_errors']:,}", "lost or reordered packets",
             tone="bad" if p["cc_errors"] else ""),
        _kpi("PCR breaks", f"{p['pcr_disc']:,}", "clock reference discontinuities",
             tone="bad" if p["pcr_disc"] else ""),
        _kpi("Table changes", f"{p['table_changes']:,}", "PAT/PMT restarts",
             tone="warn" if p["table_changes"] else ""),
        _kpi("Codec detail", f'<span style="font-size:16px">{esc(p["probe_status"] or "n/a")}</span>',
             "from ffprobe on a snapshot"),
    ])
    rows = "".join(
        f"<tr><td class='num'>0x{st['pid']:04X}</td>"
        f"<td>{_pill(st['kind'], 'tech' if st['kind'] == 'video' else '')}</td>"
        f"<td>{esc(st['codec'])}</td><td>{stream_chips(st['detail'], st['kind'])}</td></tr>"
        for st in p["streams"]) or "<tr><td colspan='4' class='muted'>No program tables found yet.</td></tr>"
    detail_cards = []
    for st in p["streams"]:
        fields = st.get("fields") or []
        if not fields:
            continue
        kind = st.get("kind", "")
        rows_html = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in fields)
        detail_cards.append(
            f'<div class="panel"><div class="ph"><h3>PID 0x{st["pid"]:04X} · {esc(kind)}</h3></div>'
            f'<dl class="kv-grid">{rows_html}</dl></div>')
    details = ""
    if detail_cards:
        details = ('<div class="ph" style="margin-top:6px"><h2>Stream details</h2>'
                   '<span class="muted small">every field the stream reports, in plain words</span></div>'
                   f'<div class="grid2">{"".join(detail_cards)}</div>'
                   '<p class="muted small">Primaries set the colour gamut, transfer the brightness curve '
                   '(HLG and PQ are HDR), matrix the YCbCr conversion, and range whether levels are limited (TV) '
                   'or full (PC). "not signalled" means the stream does not say, so players guess.</p>')
    return f"""<section id="stream"><div class="ph"><h2>Stream contents</h2>
  <span class="muted small">{esc(p['verdict'])}</span></div>
  <div class="panel">{mux_composition(p)}</div>
  {sender_card(p)}
  {tr_table(p)}
  {pcr_card(p)}
  {content_card(p)}
  <div class="kpis">{cards}</div>
  {pid_table_html(p)}
  <div class="panel tablewrap"><table><thead><tr><th>PID</th><th>Kind</th><th>Codec</th><th>Format</th></tr></thead>
  <tbody>{rows}</tbody></table></div>
  {details}</section>"""


def pid_table_html(p):
    """Every PID seen on the stream: its role, what its descriptors say, and its average rate and share."""
    from tsanalyze import codec_name
    pids = p.get("pids") or []
    if not pids:
        return ""
    items = []
    for r in pids:
        raw = r.get("descriptors") or []
        raw_rows = "".join(
            f"<tr><td class='num'>{esc(d['tag'])}</td><td>{esc(d.get('name', ''))}</td>"
            f"<td class='num'>{d['len']}</td><td class='num muted' style='word-break:break-all'>{esc(d['hex'])}</td></tr>"
            for d in raw)
        raw_html = (f"<table class='pid-raw'><thead><tr><th>Tag</th><th>Means</th><th>Bytes</th><th>Data (hex)</th></tr>"
                    f"</thead><tbody>{raw_rows}</tbody></table>") if raw else \
            "<p class='muted small'>No descriptors on this PID.</p>"
        st = r.get("stream_type")
        facts = [
            ("Stream type", f"0x{st:02X} · {codec_name(st)}" if st is not None else "not in any PMT"),
            ("Programme", str(r["program"]) if r.get("program") is not None else "not in any PMT"),
            ("PCR on this PID", "yes" if r.get("pcr") else "no"),
            ("Rate on the wire", f"last 30 s {r['kbps']:,.1f} kbps; whole run {r.get('kbps_run', 0):,.1f} kbps; "
                                 f"{r['share']:.1f}% of the stream"),
            ("Adaptation field", f"{r.get('stuffing_pct', 0):.1f}% of this PID's bytes (stuffing and PCR)"),
            ("Repeated packets", f"{r.get('dup_pct', 0):.2f}% of this PID's packets repeat the one before"),
        ]
        facts_html = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts)
        items.append(
            f'<details class="pid-row" data-key="pid-{r["pid"]}"><summary>'
            f'<span class="num">0x{r["pid"]:04X}</span><span>{esc(r["role"])}</span>'
            f'<span class="muted">{esc(r["desc"])}</span>'
            f'<span class="num">{r["kbps"]:,.1f}</span><span class="num">{r["share"]:.1f}%</span></summary>'
            f'<div class="pid-body"><dl class="kv-grid">{facts_html}</dl>{raw_html}</div></details>')
    return ('<div class="ph" style="margin-top:6px"><h2>All PIDs</h2>'
            '<span class="muted small">every packet identifier seen this run. Open a row for the definitions it declares.</span></div>'
            '<div class="panel pid-table">'
            '<div class="pid-head"><span>PID</span><span>Role</span><span>What the stream says</span>'
            '<span>kbps (last 30 s)</span><span>Share</span></div>'
            f'{"".join(items)}</div>'
            '<style>.pid-head,.pid-row summary{display:grid;grid-template-columns:90px 1.4fr 2fr 80px 70px;'
            'gap:10px;align-items:center;padding:8px 10px}.pid-head{font-weight:600;font-size:12px;'
            'text-transform:uppercase;letter-spacing:.04em;color:var(--muted,#64748b);border-bottom:1px solid var(--line,#e2e5ea)}'
            '.pid-row{border-bottom:1px solid var(--line,#e2e5ea)}.pid-row summary{cursor:pointer;list-style:none}'
            '.pid-row summary::-webkit-details-marker{display:none}.pid-row summary:hover{background:rgba(127,127,127,.06)}'
            '.pid-body{padding:4px 12px 14px 12px}.pid-raw{width:100%;margin-top:8px}'
            '@media (max-width:700px){.pid-head{display:none}.pid-row summary{grid-template-columns:1fr 1fr}}</style>')


def pcr_card(p):
    """Spacing between consecutive PCRs on each PID: the clock the decoder locks to."""
    rows = p.get("pcr_intervals") or []
    if not rows:
        return ""
    def ms(v):
        return "–" if v is None else f"{v:.2f}"
    body = []
    early_notes = "".join(
        f'<p class="soft-note">0x{r["pid"]:04X}: {r["early_count"]} interval{"s" if r["early_count"] != 1 else ""} '
        f'in the first 15 s after connecting, longest {r["early_max_ms"]:.1f} ms. '
        'Kept out of the figures above, since connecting can disturb the first few seconds.</p>'
        for r in rows if r.get("early_count"))
    for r in rows:
        over = r["max_ms"] is not None and r["max_ms"] > 40.0
        body.append(f"<tr><td class='num'>0x{r['pid']:04X}</td><td class='num'>{r['count']:,}</td>"
                    f"<td class='num'>{ms(r['mean_ms'])}</td><td class='num'>{ms(r['median_ms'])}</td>"
                    f"<td class='num'>{ms(r['max_ms'])}{' · over 40 ms' if over else ''}</td></tr>")
    return ('<div class="panel"><div class="ph"><h3>PCR intervals</h3></div>'
            '<p class="muted small">Time between consecutive PCRs on each PID, in milliseconds. MPEG-2 allows up to '
            '40 ms between PCRs (TR 101 290). Mean and max cover the whole run; the median covers the most recent intervals.</p>'
            '<div class="tablewrap"><table><thead><tr><th>PID</th><th>Intervals</th><th>Mean ms</th>'
            '<th>Median ms</th><th>Max ms</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>{early_notes}</div>')


def content_card(p):
    """Audio and video content, from the latest check (every 15 s on the newest 4 s of stream)."""
    c = p.get("content")
    if not c:
        return ('<div class="panel"><div class="ph"><h3>Audio and video content</h3></div>'
                '<p class="muted small">Waiting for the first content check (about 10 s after the stream starts).</p></div>')
    if c.get("error"):
        return (f'<div class="panel"><div class="ph"><h3>Audio and video content</h3></div>'
                f'<p class="muted small">Content check could not run: {esc(c["error"])}</p></div>')
    def val(x, fmt):
        return "no audio" if x is None else fmt.format(x)
    facts = [
        ("Loudness", val(c.get("lufs"), "{:.1f} LUFS (EBU R128 target -23)")),
        ("True peak", val(c.get("true_peak"), "{:.1f} dBTP (limit -1)")),
        ("Black picture", f"{c.get('black_s', 0):.1f} s in the window"),
        ("Frozen picture", "none" if not c.get("freezes") else f"{max(c['freezes']):.1f} s"),
        ("Silent audio", f"{c.get('silence_s', 0):.1f} s in the window"),
    ]
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts)
    issues = "".join(f"<li>{esc(t)}</li>" for _, t in c.get("findings", [])) or "<li>No problems in the last window</li>"
    return (f'<div class="panel"><div class="ph"><h3>Audio and video content</h3>'
            f'<span class="muted small">checked {esc(c.get("time", ""))}</span></div>'
            f'<dl class="kv-grid">{rows}</dl><ul>{issues}</ul>'
            '<p class="muted small">Measured on the newest 4 s of stream with ffmpeg: EBU R128 loudness and true peak, '
            'black, freeze and silence detection.</p></div>')


# plain words for what a fired check means, keyed by the check's number (the hover text has the full rule)
TR_DISCREPANCY = {
    "1.1–1.2": "packets not aligned to the 188-byte framing",
    "1.3": "PAT not arriving every 500 ms",
    "1.4": "packets missing or out of order on a PID",
    "1.5": "PMT not arriving every 500 ms",
    "1.6": "a PID went quiet for over 500 ms",
    "2.1": "packets flagged corrupt upstream",
    "2.2": "a table section failed its CRC-32",
    "2.3": "clock reference jumped over 100 ms with no discontinuity flag",
    "2.3a": "clock reference repeated more than 40 ms apart",
    "2.3b": "clock reference stepped backwards with no flag",
    "2.5": "timestamps stopped for over 700 ms",
    "2.6": "scrambled packets but no CAT seen",
    "3.1": "NIT not arriving every 10 s",
    "3.4": "a PID carries data that nothing points to",
    "3.5": "SDT not arriving every 2 s",
    "3.6": "EIT not arriving every 2 s",
    "3.8": "TDT not arriving every 30 s",
}


def tr_table(p):
    """ETSI TR 101 290 checks from the transport stream: priority 1 is critical, 2 is serious,
    3 is informational. Each count is an episode (a timeout that started, or a sync loss)."""
    items = p.get("tr101290") or []
    if not items:
        return ""
    rows = []
    absent = []           # tables this stream never carries: listed last
    measured = 0
    for it in items:
        n = it["count"]
        tip = esc(it.get("hint") or TR_HINTS.get(it["name"], ""))
        if n is None:
            continue          # not measured by this tool: explained in the footer, not listed
        number = it["name"].split(" ", 1)[0]
        if it.get("carried") is False:
            absent.append(f'<tr title="{tip}"><td>P{it["priority"]}</td><td>{esc(it["name"])}</td>'
                          f'<td class="muted" colspan="4">not carried by this stream</td></tr>')
            continue
        measured += 1
        result_html = None
        if n == 0:
            tone, result, what = "ok", "OK", "—"
        else:
            tone = "neutral" if it["priority"] == 3 else ("bad" if it["priority"] == 1 else "warn")
            result = f"{n} seen"
            what = TR_DISCREPANCY.get(number, "see the hover text")
            if it.get("late") == 0 and (it.get("early") or 0) > 0:
                # every episode fell in the first 15 s after connecting and none came later: likely set-up
                what += ". Only in the first 15 s after connecting, so not persistent."
        first = esc(it.get("first") or "—")
        last = esc(it.get("last") or "—")
        rows.append(f'<tr title="{tip}"><td>P{it["priority"]}</td><td>{esc(it["name"])}</td>'
                    f'<td><span class="res {tone}">{result_html or esc(result)}</span></td><td>{esc(what)}</td>'
                    f'<td>{first}</td><td>{last}</td></tr>')
    total = len(items)
    cw = p.get("connect_window") or {}
    early, late, win = cw.get("early", 0), cw.get("late", 0), cw.get("window_s", 15.0)
    connect_note = ""
    if early and not late:
        connect_note = (f'<p class="soft-note">{early} episode{"s" if early != 1 else ""} in the first {win:.0f} s '
                        f'after the stream started, and none after. That is consistent with the connection settling.</p>')
    elif early:
        connect_note = (f'<p class="soft-note">{early} episode{"s" if early != 1 else ""} in the first {win:.0f} s '
                        f'after the stream started, and {late} later.</p>')
    return ('<div class="panel tablewrap"><div class="ph"><h3>Transport stream checks (ETSI TR 101 290)</h3></div>'
            f'<p class="muted small">{measured} of {total} indicators measured. "Not carried" means the stream never '
            'sent that table, so there was nothing to check. Hover a row for what it means.</p>'
            '<table class="tr-table"><thead><tr><th>Priority</th><th>Check</th><th>Result</th><th>What it means here</th>'
            '<th>First</th><th>Last</th></tr></thead>'
            f'<tbody>{"".join(rows + absent)}</tbody></table>{connect_note}'
            '<p class="muted small">Not measured, and why: PCR accuracy needs a precise clock at the receiver, '
            'which network jitter hides; SI repetition, buffer models, empty buffer, data delay and RST need '
            'decoder and broadcast-profile models.</p></div>')


def sender_card(p):
    """The muxer name the stream carries. It is a clue, not proof of which software sent it."""
    sdt = p.get("sdt") or {}
    if not (sdt.get("provider") or sdt.get("service")):
        return ""
    return (f'<div class="panel"><div class="ph"><h3>Likely encoder</h3></div>'
            f'<dl class="kv-grid"><dt>Service name</dt>'
            f'<dd>{esc(sdt.get("provider") or "?")} / {esc(sdt.get("service") or "?")}<br>'
            '<span class="muted small">The name the sender wrote into the stream (SDT). It usually '
            'names the software that encoded or muxed it, so it is the likely encoder. The sender can '
            'change it, so treat it as a clue.</span></dd></dl></div>')


def _run_epoch(ts_text, run_start):
    """'HH:MM:SS UTC' on the run's date as epoch seconds; the next day if it would fall before the start."""
    base = calendar.timegm(time.strptime(run_start[:19], "%Y-%m-%d %H:%M:%S"))
    t = calendar.timegm(time.strptime(f"{run_start[:10]} {ts_text[:8]}", "%Y-%m-%d %H:%M:%S"))
    return t + 86400 if t < base - 3600 else t


def _timeline_strip(items, caption="When each problem happened, UTC. Hover a marker for details."):
    """Horizontal UTC timeline. items: (start_epoch, end_epoch, level, label)."""
    if not items:
        return ""
    t0 = min(i[0] for i in items)
    t1 = max(i[1] for i in items) + 1.0
    span = max(t1 - t0, 1.0)
    step = next((x for x in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                 if span / x <= 8), 3600)
    ticks = []
    x = math.ceil(t0 / step) * step
    while x <= t1:
        left = (x - t0) / span * 100
        ticks.append(f'<div class="tl-tick" style="left:{left:.2f}%"></div>'
                     f'<div class="tl-lbl" style="left:{left:.2f}%">{_clock(x)}</div>')
        x += step
    marks = []
    for start, end, lvl, label in items:
        left = (start - t0) / span * 100
        width = max(0.6, (end - start) / span * 100)
        marks.append(f'<div class="tl-mark {lvl}" style="left:{left:.2f}%;width:{width:.2f}%" '
                     f'title="{esc(label)}"></div>')
    return (f'<div class="tl"><div class="tl-marks">{"".join(marks)}</div>'
            f'<div class="tl-axis">{"".join(ticks)}</div>'
            f'<div class="muted small">{esc(caption)}</div></div>')


def rtt_pattern_findings(rows, meta):
    """Repeated RTT spikes above the warning threshold. Reported when the spikes recur at a steady
    interval, and each is checked against the video bitrate bursts: a burst that fills the sender's
    uplink queue shows up as RTT, so spikes that line up with bursts point at the sender."""
    thr = core.RTT_WARN_MS
    pts = []
    for r in rows:
        if r.get("rtt_ms") is None:
            continue
        pts.append((r["elapsed_s"], r["rtt_ms"], r.get("mux_mbps") or 0.0, r.get("time")))
    if len(pts) < 10:
        return []
    exc, cur = [], None
    for t, rtt, _, tm in pts:
        if rtt >= thr:
            if cur is None:
                cur = {"t0": t, "t1": t, "peak": rtt, "time0": tm, "time1": tm}
            else:
                cur.update(t1=t, peak=max(cur["peak"], rtt), time1=tm)
        elif cur is not None:
            exc.append(cur)
            cur = None
    if cur is not None:
        exc.append(cur)
    if len(exc) < 3:
        return []
    starts = [e["t0"] for e in exc]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    mean_gap = statistics.mean(gaps)
    if mean_gap < 5 or statistics.pstdev(gaps) / mean_gap > 0.35:
        return []                                   # not regular enough to call it periodic
    mux_vals = [m for _, _, m, _ in pts if m > 0]
    med_mux = statistics.median(mux_vals) if mux_vals else 0.0
    bursts = [t for t, _, m, _ in pts if med_mux and m > 1.5 * med_mux]
    matched = sum(1 for e in exc if any(e["t0"] - 3 <= b <= e["t1"] + 1 for b in bursts))
    share = matched / len(exc)
    gap = statistics.median(gaps)
    peak = max(e["peak"] for e in exc)
    owner, conf = ("sender", "likely") if share >= 0.6 else ("network", "possible")
    if share >= 0.6:
        cause = "The spikes follow the video bitrate bursts, which fill the sender's uplink queue."
    else:
        cause = "The spikes do not follow the video bursts, so the cause is more likely on the path."
    started = meta.get("started") or time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    start_utc = end_utc = None
    try:
        if exc[0]["time0"]:
            start_utc = _run_epoch(exc[0]["time0"], started)
        if exc[-1]["time1"]:
            end_utc = _run_epoch(exc[-1]["time1"], started)
    except (ValueError, TypeError):
        start_utc = end_utc = None
    return [{"t": int(exc[0]["t0"]), "t_end": int(exc[-1]["t1"]), "owner": owner, "confidence": conf,
             "what": f"RTT above {thr:.0f} ms about every {gap:.0f} s",
             "evidence": (f"{len(exc)} spikes, peak {peak:.0f} ms, {min(gaps):.0f} to {max(gaps):.0f} s apart. "
                          f"{matched} of {len(exc)} line up with a video bitrate burst (mux above 1.5 x its median). "
                          + cause),
             "count": len(exc), "utc": start_utc, "utc_end": end_utc}]


def _clock(epoch):
    return time.strftime("%H:%M:%S", time.gmtime(epoch))


def anomaly_timeline(findings):
    """A horizontal UTC time strip: each anomaly is a marker, with clock ticks underneath."""
    timed = [f for f in findings if f.get("utc") is not None]
    if not timed:
        return ""
    t0 = min(f["utc"] for f in timed)
    t1 = max((f.get("utc_end") or f["utc"]) for f in timed) + 1.0
    span = max(t1 - t0, 1.0)
    step = next((s for s in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
                 if span / s <= 8), 3600)
    ticks = []
    x = math.ceil(t0 / step) * step
    while x <= t1:
        left = (x - t0) / span * 100
        ticks.append(f'<div class="tl-tick" style="left:{left:.2f}%"></div>'
                     f'<div class="tl-lbl" style="left:{left:.2f}%">{_clock(x)}</div>')
        x += step
    marks = []
    for f in timed:
        left = (f["utc"] - t0) / span * 100
        width = max(0.6, ((f.get("utc_end") or f["utc"]) + 1.0 - f["utc"]) / span * 100)
        marks.append(f'<div class="tl-mark" style="left:{left:.2f}%;width:{width:.2f}%" '
                     f'title="{esc(f["what"])}"></div>')
    return (f'<div class="tl"><div class="tl-marks">{"".join(marks)}</div>'
            f'<div class="tl-axis">{"".join(ticks)}</div>'
            f'<div class="muted small">UTC, {step} s between ticks</div></div>')


def anomalies_section(p):
    rows = []
    for f in p["findings"]:
        tip = esc(f"{f['what']}. {f['evidence']}")
        if f.get("utc") is not None:
            a = _clock(f["utc"])
            b = _clock(f["utc_end"]) if f.get("utc_end") is not None else a
            span = f"{a} UTC" if a == b else f"{a}–{b} UTC"
        else:
            span = f"{f['t']}s" if f["t"] == f["t_end"] else f"{f['t']}-{f['t_end']}s"
        rows.append(
            f"<tr title=\"{tip}\"><td class='num'>{span}</td><td class='num'>{f['count']}x</td>"
            f"<td>{_pill(f.get('who', f['owner']), OWNER_CLASS.get(f['owner'], ''))}</td>"
            f"<td>{_pill(f['confidence'], f['confidence'])}</td>"
            f"<td><b>{esc(f['what'])}</b><div class='muted small'>{esc(f['evidence'])}</div></td></tr>")
    body = "".join(rows) or ("<tr><td colspan='5'><div class='empty'><span class='tick-ok'>OK</span>"
                              "No anomalies detected in the transport stream.</div></td></tr>")
    return f"""<section id="anomalies"><div class="ph"><h2>Anomalies</h2>
  <span class="muted small">Inferred from one end of the link. Each row gives its evidence and confidence.</span></div>
  <div class="panel tablewrap">{anomaly_timeline(p["findings"])}<table><thead><tr><th>When</th><th>Count</th>
  <th>Suspected side</th><th>Confidence</th><th>What and evidence</th></tr></thead><tbody>{body}</tbody></table></div></section>"""


def _event_kind(text):
    t = text.lower()
    if any(k in t for k in ("lost", "stopped", "no data", "fail", "error", "timeout", "down")):
        return "bad"
    if any(k in t for k in ("warn", "silent", "restart", "changed", "reconnect")):
        return "warn"
    if any(k in t for k in ("accepted", "connected", "started", "ready")):
        return "ok"
    return "info"



CHART_POINTS = 2000   # longest series sent to the page; longer runs are thinned evenly


def _keep_index(n):
    if n <= CHART_POINTS:
        return list(range(n))
    step = -(-n // CHART_POINTS)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


def _time_of_day(hms):
    try:
        h, m, sec = (int(p) for p in hms.split()[0].split(":"))
        return h * 3600 + m * 60 + sec
    except (ValueError, AttributeError):
        return None


def chart_html(title, xs, times, series, unit, hlines=(), height=260, xmin=0.0):
    """A zoomable chart. The data goes in an attribute and the browser draws it on a canvas.
    The x axis is wall-clock time: t0 is the time of day at x = 0 (the start of the run)."""
    base = _time_of_day(times[0]) if times else None
    t0 = (base - xs[0]) if (base is not None and xs) else 0.0
    idx = _keep_index(len(xs))
    data = {"title": title, "unit": unit, "height": height, "xmin": xmin, "t0": t0,
            "x": [xs[i] for i in idx], "t": [times[i] for i in idx],
            "series": [{"name": n, "color": c, "y": [ys[i] for i in idx]} for n, c, ys in series],
            "hlines": [{"v": v, "label": lbl, "color": c} for v, lbl, c in hlines]}
    attr = html_mod.escape(json.dumps(data, separators=(",", ":")), quote=True)
    return (f'<div class="chart" data-chart="{attr}" data-title="{esc(title)}">'
            '<div class="chart-tools"><span class="muted small">Times in UTC</span>'
            '<button class="btn sm" type="button" data-reset>Reset zoom</button></div></div>')


def _mux_panel(xs, times, rows):
    series = [("Mux", "#334155", [r.get("mux_mbps", 0) for r in rows]),
              ("Video", "#6c5fc7", [r.get("video_mbps", 0) for r in rows]),
              ("Audio", "#0e7490", [r.get("audio_mbps", 0) for r in rows])]
    return ('<div class="panel"><div class="ph"><h3>Stream bitrate</h3>'
            '<button class="btn sm" type="button" data-png>PNG</button></div>'
            + chart_html("Stream bitrate", xs, times, series, "Mbps") + "</div>")


def _av_panel(xs, times, av):
    lim = [(AV_DETECT_MS[1], "audio leads limit", "#d97706"), (AV_DETECT_MS[0], "audio lags limit", "#d97706")]
    note = ("Timestamp offset from the PES headers: video minus audio, smoothed over five samples. "
            "Positive means audio leads. It shows how the sender stamped the streams; it cannot see content "
            "captured out of sync. For that, send a flash-and-beep test pattern from the sender.")
    return ('<div class="panel"><div class="ph"><h3>A/V offset</h3>'
            '<button class="btn sm" type="button" data-png>PNG</button></div>'
            + chart_html("A/V offset", xs, times, [("Offset", "#6c5fc7", av["series"])], "ms", hlines=lim)
            + f'<p class="muted small">{esc(note)}</p></div>')


def _picture_table(frames):
    if not frames:
        return ""
    rows_html = "".join(
        f"<tr><td>{esc(f.get('time', ''))}</td><td>{f['min']}</td><td>{f['max']}</td>"
        f"<td>{f['above_pct']:.2f}%</td><td>{f['below_pct']:.2f}%</td>"
        f"<td>{f['clip_high_pct']:.2f}%</td><td>{f['clip_low_pct']:.2f}%</td></tr>"
        for f in frames[-12:])
    return ('<h3 style="margin:18px 0 8px">Sampled picture levels</h3>'
            '<div class="panel tablewrap"><table class="cfg"><thead><tr><th>Sampled</th><th>Min code</th>'
            '<th>Max code</th><th>Above legal</th><th>Below legal</th><th>Piled at peak</th>'
            '<th>Piled at floor</th></tr></thead>'
            f'<tbody>{rows_html}</tbody></table></div>')


def config_section(findings, frames=None):
    """Config checks: signalling and profile mismatches, each with what it does and what to check."""
    counts = {"fail": 0, "warn": 0, "info": 0}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1
    if findings:
        head = f"{counts['fail']} fail · {counts['warn']} warn · {counts['info']} info"
        rows_html = "".join(
            f"<tr title=\"{esc(f['why'])}\"><td>{_pill(f['level'].upper(), f['level'])}</td><td>{esc(_pid_text(f['pid']))}</td>"
            f"<td><b>{esc(f['title'])}</b></td><td>{esc(f['why'])}</td><td>{esc(f['check'])}</td></tr>"
            for f in findings)
        body = ('<div class="panel tablewrap"><table class="cfg"><thead><tr><th>Level</th><th>Stream</th>'
                '<th>Finding</th><th>Why it matters</th><th>Check at the sender</th></tr></thead>'
                f'<tbody>{rows_html}</tbody></table></div>')
    else:
        head = "no findings"
        body = ('<div class="panel"><div class="empty"><span class="tick-ok">OK</span>No signalling or '
                'profile mismatches in the stream metadata.</div></div>')
    note = ('<p class="muted small">Metadata checks read the stream headers. Picture checks sample a few '
            'decoded frames for clipping and out-of-range levels. Banding from 8-bit material is not detected: '
            'it cannot be seen reliably after encoding.</p>')
    return (f'<section id="config"><div class="ph"><h2>Config checks</h2><span class="muted small">{head}</span>'
            f'</div>{body}{_picture_table(frames)}{note}</section>')


def _all_findings(payload):
    return list(payload.get("config") or []) + list(payload.get("picture_findings") or [])


def _pid_text(pid):
    if pid is None:
        return "Picture"
    try:
        return f"PID 0x{int(pid):04X}"
    except (TypeError, ValueError):
        return f"PID {pid}"


def _event_item(ev, expand=False):
    """One timeline row. Events that carry a detail list (codec changes) expand to show the fields."""
    ts, text = ev[0], ev[2]
    detail = ev[3] if len(ev) > 3 else None
    dot = f'<span class="tdot {_event_kind(text)}"></span>'
    tip = esc(issue_hint(text))
    if not detail:
        return f'<li title="{tip}"><span class="when">{esc(ts)}</span>{dot}<span>{esc(text)}</span></li>'
    rows = "".join(
        f"<tr><td>{esc(_pid_text(pid))}</td><td>{esc(field)}</td>"
        f"<td>{esc(before)}</td><td>{esc(after)}</td></tr>"
        for pid, field, before, after in detail)
    table = ('<table class="ev-table"><thead><tr><th>Stream</th><th>Field</th><th>Before</th>'
             f'<th>After</th></tr></thead><tbody>{rows}</tbody></table>')
    key = esc(f"{ts}|{text}")
    return (f'<li title="{tip}"><span class="when">{esc(ts)}</span>{dot}<div class="ev-body">'
            f'<details data-key="{key}"{" open" if expand else ""}><summary>{esc(text)}</summary>{table}</details>'
            '</div></li>')


def _tab(html, sid):
    """Mark one page section as a tab panel."""
    return html.replace(f'<section id="{sid}"', f'<section id="{sid}" data-tab="{sid}"', 1)


CHART_JS = r"""<script>
(function () {
  // one zoom range for every chart on the page: all charts share the run's time axis
  var zoom = null;
  function drawAll() { document.querySelectorAll('.chart').forEach(function (el) { draw(el); }); }
  function setZoomBar() {
    var bar = document.getElementById('zoom-bar');
    if (!bar) { return; }
    if (!zoom) { bar.hidden = true; return; }
    var first = document.querySelector('.chart');
    var t0 = first ? JSON.parse(first.getAttribute('data-chart')).t0 : 0;
    var span = zoom[1] - zoom[0];
    document.getElementById('zoom-text').textContent = 'Zoomed in: ' + clockOf(zoom[0], t0) + ' to ' +
      clockOf(zoom[1], t0) + ' UTC on all charts (' + (span >= 100 ? span.toFixed(0) : span.toFixed(1)) + ' s)';
    var nav = document.querySelector('nav.tabs');
    var stuck = nav ? (parseFloat(getComputedStyle(nav).top) || 0) + nav.offsetHeight : 0;
    bar.style.top = stuck + 'px';
    bar.hidden = false;
  }
  function clearZoom() { zoom = null; setZoomBar(); drawAll(); }
  function css(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }
  function p2(v) { return (v < 10 ? '0' : '') + v; }
  // x is seconds since the start of the run; labels are the wall-clock time of day (HH:MM:SS)
  function clockOf(sec, t0) {
    var s = ((Math.round(t0 + sec) % 86400) + 86400) % 86400;
    return p2(Math.floor(s / 3600)) + ':' + p2(Math.floor((s % 3600) / 60)) + ':' + p2(s % 60);
  }
  function niceStep(span, target) {
    var raw = span / target, mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), f = raw / mag;
    return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  }
  function ticks(lo, hi, n) {
    var step = niceStep((hi - lo) || 1, n), out = [];
    for (var v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) { out.push(+v.toFixed(10)); }
    return out;
  }
  function fmtNum(v, step) { return step >= 1 ? v.toFixed(0) : step >= 0.1 ? v.toFixed(1) : v.toFixed(2); }
  function nearest(x, sec) {
    var a = 0, b = x.length - 1;
    while (a < b) { var m = (a + b) >> 1; if (x[m] < sec) { a = m + 1; } else { b = m; } }
    if (a > 0 && Math.abs(x[a - 1] - sec) < Math.abs(x[a] - sec)) { a--; }
    return a;
  }
  function draw(el) {
    var data = JSON.parse(el.getAttribute('data-chart'));
    var W = el.clientWidth;
    if (!W || !data.x.length) { return; }
    var H = data.height || 260, dpr = window.devicePixelRatio || 1;
    var canvas = el.querySelector('canvas.chart-canvas');
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.className = 'chart-canvas';
      el.insertBefore(canvas, el.firstChild);
    }
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    var g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    var ink = css('--ink', '#111'), muted = css('--muted', '#64748b'),
        grid = css('--line', '#e2e8f0'), card = css('--card', '#ffffff'), accent = css('--accent', '#6c5fc7');
    var font = getComputedStyle(el).fontFamily || 'sans-serif';
    var x = data.x, n = x.length;
    var start = data.xmin != null ? Math.min(data.xmin, x[0]) : x[0];
    var full = [start, x[n - 1] > start ? x[n - 1] : start + 1];
    var view = zoom || full;
    if (view[1] <= view[0]) { view = full; }
    var pl = 56, pr = 18, pt = 34, pb = 30, cw = W - pl - pr, ch = H - pt - pb;
    var first = nearest(x, view[0]), last = nearest(x, view[1]);
    var hi = 0, i;
    data.series.forEach(function (s) { for (i = first; i <= last; i++) { if (s.y[i] > hi) { hi = s.y[i]; } } });
    data.hlines.forEach(function (h) { if (h.v > hi) { hi = h.v; } });
    hi = hi > 0 ? hi * 1.08 : 1;
    function X(v) { return pl + (v - view[0]) / ((view[1] - view[0]) || 1) * cw; }
    function Y(v) { return pt + ch - v / hi * ch; }
    el._geom = { pl: pl, cw: cw, view: view };

    g.fillStyle = card; g.fillRect(0, 0, W, H);
    g.font = '12px ' + font; g.lineWidth = 1;
    var yStep = niceStep(hi, 4);
    ticks(0, hi, 4).forEach(function (v) {
      var y = Y(v);
      g.strokeStyle = grid; g.beginPath(); g.moveTo(pl, y); g.lineTo(W - pr, y); g.stroke();
      g.fillStyle = muted; g.textAlign = 'right'; g.textBaseline = 'middle';
      g.fillText(fmtNum(v, yStep), pl - 8, y);
    });
    ticks(view[0], view[1], Math.max(2, Math.floor(cw / 110))).forEach(function (v) {
      if (v < view[0] || v > view[1]) { return; }
      g.fillStyle = muted; g.textAlign = 'center'; g.textBaseline = 'top';
      g.fillText(clockOf(v, data.t0), X(v), pt + ch + 8);
    });
    g.textAlign = 'left'; g.textBaseline = 'middle'; g.fillStyle = muted;
    g.fillText(data.unit, 8, 13);
    var lx = pl;
    data.series.forEach(function (s) {
      g.fillStyle = s.color; g.fillRect(lx, 8, 10, 10);
      g.fillStyle = ink; g.fillText(s.name, lx + 14, 13);
      lx += 14 + g.measureText(s.name).width + 22;
    });
    data.hlines.forEach(function (h) {
      g.save(); g.setLineDash([6, 4]); g.strokeStyle = h.color; g.beginPath();
      g.moveTo(pl, Y(h.v)); g.lineTo(W - pr, Y(h.v)); g.stroke(); g.restore();
      g.fillStyle = h.color; g.textAlign = 'right'; g.textBaseline = 'bottom';
      g.fillText(h.label, W - pr, Y(h.v) - 3);
    });
    if (first <= last) {
      data.series.forEach(function (s, idx) {
        if (idx === 0) {
          g.save(); g.globalAlpha = 0.14; g.beginPath();
          for (i = first; i <= last; i++) { if (i === first) { g.moveTo(X(x[i]), Y(s.y[i])); } else { g.lineTo(X(x[i]), Y(s.y[i])); } }
          g.lineTo(X(x[last]), Y(0)); g.lineTo(X(x[first]), Y(0)); g.closePath();
          g.fillStyle = s.color; g.fill(); g.restore();
        }
        g.beginPath();
        for (i = first; i <= last; i++) { if (i === first) { g.moveTo(X(x[i]), Y(s.y[i])); } else { g.lineTo(X(x[i]), Y(s.y[i])); } }
        g.strokeStyle = s.color; g.lineWidth = 2; g.stroke();
      });
    }
    if (el._hover != null && first <= last) {
      var sec = view[0] + (el._hover - pl) / cw * (view[1] - view[0]);
      var j = nearest(x, sec);
      if (j < first) { j = first; }
      if (j > last) { j = last; }
      var jx = X(x[j]);
      g.save(); g.strokeStyle = muted; g.setLineDash([3, 3]); g.beginPath();
      g.moveTo(jx, pt); g.lineTo(jx, pt + ch); g.stroke(); g.restore();
      data.series.forEach(function (s) {
        g.fillStyle = s.color; g.beginPath(); g.arc(jx, Y(s.y[j]), 4, 0, Math.PI * 2); g.fill();
      });
      var lines = [(data.t[j] || clockOf(x[j], data.t0))];
      data.series.forEach(function (s) { lines.push(s.name + ':  ' + (+s.y[j]).toFixed(2) + ' ' + data.unit); });
      g.font = '12px ' + font;
      var tw = Math.max.apply(null, lines.map(function (t) { return g.measureText(t).width; })) + 20;
      var th = lines.length * 18 + 10;
      var tx = jx + 12 + tw > W ? jx - 12 - tw : jx + 12, ty = pt + 4;
      g.fillStyle = card; g.fillRect(tx, ty, tw, th);
      g.strokeStyle = grid; g.strokeRect(tx, ty, tw, th);
      g.fillStyle = ink; g.textAlign = 'left'; g.textBaseline = 'middle';
      lines.forEach(function (t, q) { g.fillText(t, tx + 10, ty + 14 + q * 18); });
    }
    if (el._drag) {
      var a = Math.min(el._drag.x0, el._drag.x1), b = Math.max(el._drag.x0, el._drag.x1);
      g.save(); g.globalAlpha = 0.15; g.fillStyle = accent; g.fillRect(a, pt, b - a, ch); g.restore();
    }
  }
  function attach(el) {
    if (el._attached) { return; }
    el._attached = true;
    el.addEventListener('mousemove', function (e) {
      el._hover = e.clientX - el.getBoundingClientRect().left;
      if (el._drag) { el._drag.x1 = el._hover; }
      draw(el);
    });
    el.addEventListener('mouseleave', function () { el._hover = null; draw(el); });
    el.addEventListener('mousedown', function (e) {
      if (e.button !== 0) { return; }
      var px = e.clientX - el.getBoundingClientRect().left;
      el._drag = { x0: px, x1: px };
      e.preventDefault();
    });
    el.addEventListener('dblclick', clearZoom);
  }
  function all() {
    document.querySelectorAll('.chart').forEach(function (el) { attach(el); draw(el); });
    setZoomBar();
  }
  document.addEventListener('mouseup', function () {
    document.querySelectorAll('.chart').forEach(function (el) {
      var d = el._drag;
      el._drag = null;
      if (!d) { return; }
      if (Math.abs(d.x1 - d.x0) < 6 || !el._geom) { draw(el); return; }
      var geo = el._geom, v0 = geo.view;
      function val(px) { return v0[0] + (px - geo.pl) / geo.cw * (v0[1] - v0[0]); }
      zoom = [val(Math.min(d.x0, d.x1)), val(Math.max(d.x0, d.x1))];
      setZoomBar();
      drawAll();
    });
  });
  document.addEventListener('click', function (e) {
    var r = e.target.closest('[data-reset]');
    if (r || e.target.closest('#zoom-reset')) { clearZoom(); }
    if (e.target.closest('#theme')) { setTimeout(all, 0); }
  });
  window.addEventListener('resize', all);
  window.srtCharts = all;
  window.srtChartPng = function (panel) {
    var c = panel.querySelector('canvas.chart-canvas');
    if (!c) { return; }
    var h = panel.querySelector('h3');
    var a = document.createElement('a');
    a.href = c.toDataURL('image/png');
    a.download = 'srtdebug-' + (h ? h.textContent : 'chart').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-') + '.png';
    document.body.appendChild(a); a.click(); a.remove();
  };
  all();
})();
</script>"""

TILE_JS = r"""<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
(function () {
  // Every KPI tile and panel gets Copy (text) and PNG (picture) actions. Charts already have their own PNG.
  function decorate(root) {
    (root || document).querySelectorAll('.kpi, .panel').forEach(function (el) {
      if (el.getAttribute('data-decorated')) { return; }
      el.setAttribute('data-decorated', '1');
      var bar = document.createElement('div');
      bar.className = 'tile-actions';
      var png = el.querySelector('[data-png]') ? '' : '<button class="btn sm" type="button" data-tile="png">PNG</button>';
      bar.innerHTML = '<button class="btn sm" type="button" data-tile="copy">Copy</button>' + png;
      el.appendChild(bar);
    });
  }
  window.srtDecorate = decorate;
  function flash(btn, text) {
    var old = btn.getAttribute('data-label') || btn.textContent;
    btn.setAttribute('data-label', old);
    btn.textContent = text;
    setTimeout(function () { btn.textContent = old; }, 1500);
  }
  function slug(el) {
    var h = el.querySelector('h2, h3, .kl');
    return ((h ? h.textContent : 'tile').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')) || 'tile';
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-tile]');
    if (!btn) { return; }
    var tile = btn.closest('.kpi, .panel');
    if (!tile) { return; }
    var bar = tile.querySelector('.tile-actions');
    if (btn.getAttribute('data-tile') === 'copy') {
      bar.style.display = 'none';
      var text = tile.innerText.replace(/\n{2,}/g, '\n').trim();
      bar.style.display = '';
      var fallback = function () {
        var ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); flash(btn, 'Copied'); } catch (err) { flash(btn, 'Copy failed'); }
        ta.remove();
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { flash(btn, 'Copied'); }, fallback);
      } else { fallback(); }
      return;
    }
    if (!window.html2canvas) { flash(btn, 'Needs internet'); return; }
    bar.style.visibility = 'hidden';
    window.html2canvas(tile, { backgroundColor: getComputedStyle(document.body).backgroundColor, scale: 2 })
      .then(function (c) {
        bar.style.visibility = '';
        var a = document.createElement('a');
        a.href = c.toDataURL('image/png');
        a.download = slug(tile) + '.png';
        document.body.appendChild(a); a.click(); a.remove();
      }, function () { bar.style.visibility = ''; flash(btn, 'PNG failed'); });
  });
  decorate();
})();
</script>"""


UI_JS = r"""<script>
(function () {
  function tabButtons() { return [].slice.call(document.querySelectorAll('[data-go]')); }
  function tabPanels() { return [].slice.call(document.querySelectorAll('[data-tab]')); }
  var current = 'overview';
  function show(id) {
    var known = tabButtons().some(function (b) { return b.getAttribute('data-go') === id; });
    current = known ? id : 'overview';
    tabButtons().forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-go') === current); });
    tabPanels().forEach(function (p) { p.hidden = p.getAttribute('data-tab') !== current; });
    if (location.hash !== '#' + current) { history.replaceState(null, '', '#' + current); }
    if (window.srtCharts) { window.srtCharts(); }
  }
  window.srtShow = show;
  window.srtCurrent = function () { return current; };
  window.srtShowIssues = function () {
    show('overview');
    var el = document.getElementById('issues');
    if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  };

  function savePng(panel) {
    var svg = panel && panel.querySelector('svg');
    if (!svg) { return; }
    var styles = getComputedStyle(document.documentElement);
    var vb = svg.viewBox && svg.viewBox.baseVal;
    var box = svg.getBoundingClientRect();
    var w = (vb && vb.width) || box.width, h = (vb && vb.height) || box.height;
    var scale = 2, pad = 24, W = Math.round((w + pad) * scale), H = Math.round(h * scale);
    var clone = svg.cloneNode(true);
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('viewBox', '0 0 ' + (w + pad) + ' ' + h);
    clone.setAttribute('width', W);
    clone.setAttribute('height', H);
    clone.setAttribute('font-family', getComputedStyle(svg).fontFamily);
    var xml = new XMLSerializer().serializeToString(clone).replace(/var\((--[\w-]+)\)/g, function (m, name) {
      return styles.getPropertyValue(name).trim() || '#888';
    });
    var heading = panel.querySelector('h3');
    var name = 'srtdebug-' + (heading ? heading.textContent : 'chart').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-');
    var img = new Image();
    img.onload = function () {
      var canvas = document.createElement('canvas');
      canvas.width = W; canvas.height = H;
      var ctx = canvas.getContext('2d');
      ctx.fillStyle = styles.getPropertyValue('--card').trim() || '#ffffff';
      ctx.fillRect(0, 0, W, H);
      ctx.drawImage(img, 0, 0, W, H);
      canvas.toBlob(function (blob) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name + '.png';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
      }, 'image/png');
    };
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
  }

  // Expand: show one panel full-width in a layer above the page, so a wide table can be read without
  // scrolling the whole report. Canvas charts are left out; their drawing does not survive a copy.
  var expandable = function (panel) {
    return !panel.querySelector('canvas');
  };
  var lastFocus = null;
  function addButton(host, attr, label, title, cls) {
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'btn sm ' + cls; btn.setAttribute(attr, '');
    btn.setAttribute('aria-label', title); btn.title = title; btn.textContent = label;
    host.appendChild(btn);
  }
  function decorate() {
    document.querySelectorAll('.panel').forEach(function (panel) {
      if (!expandable(panel)) { return; }
      if (!panel.querySelector('[data-expand]')) {
        var ph = panel.querySelector('.ph');
        addButton(ph || panel, 'data-expand', 'Expand', 'Expand to full width', ph ? '' : 'xbtn');
      }
      if (!panel.querySelector('[data-pop]')) {
        var head = panel.querySelector('.ph') || panel;
        addButton(head, 'data-pop', 'Pop out', 'Open this panel in its own window', 'pop-btn' + (panel.querySelector('.ph') ? '' : ' xbtn-2'));
      }
    });
    document.querySelectorAll('.kpi').forEach(function (tile) {
      if (tile.querySelector('canvas') || tile.querySelector('[data-pop]')) { return; }
      addButton(tile, 'data-pop', 'Pop out', 'Open this tile in its own window', 'pop-btn kpi-pop');
    });
    refreshPops();
  }

  // Pop-outs: small floating windows that stay on screen while the page scrolls. Several can be open at
  // once; each one is drawn from the live page and redrawn on every refresh.
  var pops = [];
  function tileKey(el) {
    var h = el.querySelector('h2, h3, .kl');
    return ((h ? h.textContent : el.textContent) || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  }
  function findTile(key) {
    var found = null;
    document.querySelectorAll('.panel, .kpi').forEach(function (el) {
      if (!found && tileKey(el) === key) { found = el; }
    });
    return found;
  }
  function bodyCopy(el) {
    var copy = el.cloneNode(true);
    copy.querySelectorAll('[data-expand],[data-pop],[data-png],.tile-actions').forEach(function (b) { b.remove(); });
    copy.classList.remove('xbtn');
    return copy;
  }
  function refreshPops() {
    pops = pops.filter(function (p) {
      if (p.child) {
        if (p.child.closed) { return false; }
        var root = p.child.document && p.child.document.getElementById('pop-root');
        var srcC = findTile(p.key);
        if (root) {
          root.innerHTML = '';
          if (srcC) { root.appendChild(p.child.document.importNode(bodyCopy(srcC), true)); }
          else { root.innerHTML = '<p class="muted">This item is not in the current report.</p>'; }
        }
        return true;
      }
      if (!document.body.contains(p.win)) { return false; }
      var src = findTile(p.key);
      if (!src) { p.body.innerHTML = '<p class="muted small">This item is not in the current report.</p>'; return true; }
      p.body.innerHTML = '';
      p.body.appendChild(bodyCopy(src));
      return true;
    });
  }
  // A separate browser window: it can be moved anywhere, including another screen. It is redrawn on each
  // live refresh and closes with its own window. Returns false if the browser blocked the window.
  function popOutWindow(el, key) {
    var child = null;
    try { child = window.open('', 'srtdebug-pop-' + Date.now(), 'width=480,height=600,resizable=yes,scrollbars=yes'); }
    catch (e) { child = null; }
    if (!child) { return false; }
    var css = [].map.call(document.querySelectorAll('style'), function (st) { return st.textContent; }).join('\n');
    var theme = document.documentElement.getAttribute('data-theme');
    var safeKey = String(key).replace(/&/g, '&amp;').replace(/</g, '&lt;');
    child.document.open();
    child.document.write('<!doctype html><html lang="en"' + (theme ? ' data-theme="' + theme + '"' : '') + '>' +
      '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<title>' + safeKey + ' · srtdebug</title><style>' + css + '</style>' +
      '<style>' +
      '.pop-standalone{padding:8px;background:var(--bg);min-height:100vh;margin:0} ' +
      '.pop-standalone .kpi{padding:8px 10px;margin:0;max-width:none} ' +
      '.pop-standalone .kpi .kl{font-size:11px;margin-bottom:2px} ' +
      '.pop-standalone .kpi .kv{font-size:20px;line-height:1.15} ' +
      '.pop-standalone .kpi .ks{font-size:11px;margin-top:2px} ' +
      '.pop-standalone .panel{padding:8px 10px;margin:0 0 8px;box-shadow:none;max-width:none} ' +
      '.pop-standalone .panel .ph{margin-bottom:4px} ' +
      '.pop-standalone h2,.pop-standalone h3{margin:0;font-size:13px} ' +
      '.pop-standalone .tablewrap{overflow-x:auto} ' +
      '.pop-standalone .spark,.pop-standalone svg{max-height:120px}</style></head>' +
      '<body class="pop-standalone"><div id="pop-root"></div></body></html>');
    child.document.close();
    pops.push({key: key, child: child});
    refreshPops();
    return true;
  }

  function popOut(el) {
    var key = tileKey(el);
    var existing = pops.filter(function (p) { return p.key === key; })[0];
    if (existing) {
      if (existing.child) { existing.child.focus(); } else { existing.win.style.zIndex = String(46 + pops.length); }
      return;
    }
    if (pops.length >= 6) { return; }
    if (popOutWindow(el, key)) { return; }
    var win = document.createElement('div');
    win.className = 'pop-win' + (el.classList.contains('kpi') ? ' pop-kpi' : '');
    var offset = pops.length * 28;
    win.style.top = (96 + offset) + 'px';
    win.style.right = (16 + offset) + 'px';
    var head = document.createElement('div'); head.className = 'pop-head';
    var title = document.createElement('span'); title.textContent = key;
    var close = document.createElement('button');
    close.type = 'button'; close.className = 'pop-x'; close.setAttribute('aria-label', 'Close this pop-out');
    close.textContent = '\u00d7';
    head.appendChild(title); head.appendChild(close);
    var body = document.createElement('div'); body.className = 'pop-body';
    win.appendChild(head); win.appendChild(body);
    document.body.appendChild(win);
    var pop = {key: key, win: win, body: body};
    pops.push(pop);
    body.appendChild(bodyCopy(el));
    close.addEventListener('click', function () {
      win.remove();
      pops = pops.filter(function (p) { return p !== pop; });
    });
    // drag by the header; keeps the window on screen
    head.addEventListener('pointerdown', function (e) {
      if (e.target === close) { return; }
      e.preventDefault();
      var rect = win.getBoundingClientRect();
      var dx = e.clientX - rect.left, dy = e.clientY - rect.top;
      win.style.right = 'auto';
      win.style.zIndex = String(60 + pops.length);
      // free movement: the window can be dragged anywhere on screen, even partly past the edge; the header
      // always stays within reach so it can be grabbed again
      function move(ev) {
        var w = win.offsetWidth;
        win.style.left = Math.max(40 - w, Math.min(innerWidth - 40, ev.clientX - dx)) + 'px';
        win.style.top = Math.max(0, Math.min(innerHeight - 30, ev.clientY - dy)) + 'px';
      }
      function up() {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
      }
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
    });
    win.addEventListener('pointerdown', function () { win.style.zIndex = String(60 + pops.indexOf(pop)); });
  }
  window.srtPanelDecorate = decorate;
  function closeExpanded() {
    var layer = document.getElementById('xpop');
    if (layer) { layer.remove(); }
    document.removeEventListener('keydown', onKey);
    if (lastFocus && lastFocus.focus) { lastFocus.focus(); }
  }
  function onKey(e) { if (e.key === 'Escape') { closeExpanded(); } }
  function openExpanded(panel) {
    closeExpanded();
    lastFocus = document.activeElement;
    var layer = document.createElement('div');
    layer.id = 'xpop'; layer.className = 'xpop'; layer.setAttribute('role', 'dialog');
    layer.setAttribute('aria-modal', 'true');
    var box = document.createElement('div'); box.className = 'xpop-box';
    var head = document.createElement('div'); head.className = 'xpop-head';
    var close = document.createElement('button');
    close.type = 'button'; close.className = 'btn sm'; close.textContent = 'Close (Esc)';
    close.addEventListener('click', closeExpanded);
    head.appendChild(close);
    var copy = panel.cloneNode(true);
    copy.querySelectorAll('[data-expand]').forEach(function (b) { b.remove(); });
    copy.classList.add('xpop-panel');
    box.appendChild(head); box.appendChild(copy);
    layer.appendChild(box);
    layer.addEventListener('click', function (e) { if (e.target === layer) { closeExpanded(); } });
    document.body.appendChild(layer);
    document.addEventListener('keydown', onKey);
    close.focus();
  }

  document.addEventListener('click', function (e) {
    var pop = e.target.closest('[data-pop]');
    if (pop) { popOut(pop.closest('.panel, .kpi')); return; }
    var ex = e.target.closest('[data-expand]');
    if (ex) { openExpanded(ex.closest('.panel')); return; }
    var go = e.target.closest('[data-go]');
    if (go) { show(go.getAttribute('data-go')); return; }
    var png = e.target.closest('[data-png]');
    if (png) {
      var panel = png.closest('.panel');
      if (panel && panel.querySelector('canvas.chart-canvas') && window.srtChartPng) { window.srtChartPng(panel); }
      else { savePng(panel); }
    }
  });
  if (window.self !== window.top) { document.documentElement.classList.add('embedded'); }
  show((location.hash || '#overview').slice(1));
  decorate();
})();
</script>"""


# A/V limits from ITU-R BT.1359 (detectable: audio 45 ms early, 125 ms late; acceptable: 90 / 185 ms).
# The offset is the sender's timestamps, so these limits apply to stamping, not to captured content.
AV_DETECT_MS = (-125.0, 45.0)
AV_ACCEPT_MS = (-185.0, 90.0)
AV_DRIFT_WARN_MS = 40.0


def av_stats(rows):
    """A/V offset per sample (video PTS minus audio PTS; positive means audio leads), smoothed
    over five samples to remove the frame-quantisation noise. None when the stream has no audio."""
    raw = [r.get("av_offset_ms") for r in rows]
    known = [v for v in raw if v is not None]
    if not known:
        return None
    filled, last = [], known[0]
    for v in raw:
        last = v if v is not None else last
        filled.append(last)
    roll = [statistics.median(filled[max(0, i - 4):i + 1]) for i in range(len(filled))]
    base = statistics.median(roll[:60])
    worst = max(range(len(roll)), key=lambda i: abs(roll[i] - base))
    return {"median": statistics.median(roll), "baseline": base, "drift": abs(roll[worst] - base),
            "drift_at": rows[worst]["time"], "series": roll}


def av_findings(av):
    """(level, text) pairs for the issues list."""
    out = []
    m = av["median"]
    if m > AV_ACCEPT_MS[1] or m < AV_ACCEPT_MS[0]:
        out.append(("FAIL", f"A/V offset {m:+.0f} ms is beyond the acceptability limit "
                            f"({AV_ACCEPT_MS[0]:+.0f} to {AV_ACCEPT_MS[1]:+.0f} ms)"))
    elif m > AV_DETECT_MS[1]:
        out.append(("WARN", f"audio leads video by {m:.0f} ms: above the {AV_DETECT_MS[1]:.0f} ms detectability limit"))
    elif m < AV_DETECT_MS[0]:
        out.append(("WARN", f"audio lags video by {-m:.0f} ms: beyond the {-AV_DETECT_MS[0]:.0f} ms detectability limit"))
    if av["drift"] > AV_DRIFT_WARN_MS:
        out.append(("WARN", f"A/V offset moved {av['drift']:.0f} ms from its starting value (at {av['drift_at']})"))
    return out


def current_state(rows, alarm=10.0):
    """What the stream is doing at the end of the run.
    receiving: data now. gap: silent, but shorter than the alarm (bursty streams have these).
    no_signal: silent for at least the alarm time."""
    if not rows:
        return {"state": "waiting", "receiving": False, "since": None, "silent_s": 0.0, "mbps": 0.0}
    last = rows[-1]["bitrate_mbps"]
    if last >= 0.01:
        return {"state": "receiving", "receiving": True, "since": None, "silent_s": 0.0, "mbps": last}
    i = len(rows) - 1
    while i > 0 and rows[i - 1]["bitrate_mbps"] < 0.01:
        i -= 1
    last_active = rows[i - 1]["elapsed_s"] if i > 0 else rows[0]["elapsed_s"]
    silent = max(0.0, rows[-1]["elapsed_s"] - last_active)
    state = "no_signal" if silent >= alarm else "gap"
    return {"state": state, "receiving": False, "since": rows[i]["time"], "silent_s": silent, "mbps": 0.0}


def _lat_value(lat):
    ms = lat["negotiated"] or lat["local"]
    return f"{ms} <small>ms</small>" if ms is not None else "default"


def _lat_sub(lat):
    if lat["peer"]:
        return f"peer set {lat['peer']} ms · local {lat['local']} ms"
    if lat["negotiated"]:
        return "negotiated; peer at or below local"
    return "local setting; peer not seen yet"


def latency_view(rows, configured):
    """Compare the latency set on this side with the one SRT negotiated.

    SRT runs both directions at the larger of the two sides' latency settings, so the
    negotiated receiver delay tells us the peer's setting whenever it is above ours."""
    neg = max((int(r.get("latency_ms") or 0) for r in rows), default=0)
    if not neg:
        return {"local": configured, "negotiated": None, "peer": None, "effective": configured}
    peer = neg if configured is not None and neg > configured + 1 else None
    return {"local": configured, "negotiated": neg, "peer": peer,
            "effective": max(configured or 0, neg)}


# when the live video is on and this monitor uses more than this share of the whole PC's CPU, or this much
# memory, the load row suggests running the check without the video
USAGE_NOTE_CPU_SHARE_PCT = 25.0
USAGE_NOTE_MEM_MB = 1024.0


def _spark(points, label, unit, color):
    """A small line chart of one reading over time, from zero, so a slow climb is visible.
    points: (seconds since the monitor started, value)."""
    pts = [(t, v, w) for t, v, w in points if v is not None]
    if len(pts) < 2:
        return ""
    t0, t1 = pts[0][0], pts[-1][0]
    span = max(t1 - t0, 1.0)
    top = max(max(v for _, v, _ in pts), 1e-9)
    W, H, pad = 600, 110, 6

    def fmt(x):
        return f"{x:,.0f}" if x >= 10 else f"{x:.1f}"

    line = " ".join(f"{pad + (t - t0) / span * (W - 2 * pad):.1f},{H - pad - v / top * (H - 2 * pad):.1f}"
                    for t, v, _ in pts)
    # gridlines at 0, half and top, with their values labelled on the left
    grid_y = [H - pad - f * (H - 2 * pad) for f in (0.0, 0.5, 1.0)]
    grid = "".join(f'<line x1="0" x2="{W}" y1="{y:.1f}" y2="{y:.1f}" stroke="currentColor" stroke-opacity=".12" '
                   f'stroke-width="1" vector-effect="non-scaling-stroke"/>' for y in grid_y)
    y_labels = "".join(f"<span>{esc(fmt(v))}</span>" for v in (top, top / 2, 0.0))

    def clock(w, t):
        if w is not None:
            return time.strftime("%H:%M:%S", time.gmtime(w))
        return f"-{(t1 - t):.0f} s"

    mid = pts[len(pts) // 2]
    x_labels = "".join(f"<span>{esc(clock(p[2], p[0]))}</span>" for p in (pts[0], mid, pts[-1]))
    latest, peak = pts[-1][1], top
    return (f'<div class="spark"><div class="spark-head"><span>{esc(label)}</span>'
            f'<span class="muted small">now {latest:.1f} {unit} · peak {peak:.1f} {unit}</span></div>'
            f'<div style="display:grid;grid-template-columns:56px 1fr;align-items:stretch">'
            f'<div style="display:flex;flex-direction:column;justify-content:space-between;text-align:right;'
            f'padding-right:8px;font-size:11px;color:var(--muted,#64748b)">{y_labels}</div>'
            f'<svg class="spark-svg" viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" '
            f'aria-label="{esc(label)} over the last {span / 60:.0f} minutes">{grid}'
            f'<polyline fill="none" stroke="{color}" stroke-width="2" vector-effect="non-scaling-stroke" '
            f'points="{line}"/></svg></div>'
            f'<div style="display:flex;justify-content:space-between;margin-left:56px;font-size:11px;'
            f'color:var(--muted,#64748b);font-variant-numeric:tabular-nums">{x_labels}</div>'
            f'<div class="muted small">Last {span / 60:.0f} min, one reading every 2 s, times in UTC. A line that keeps '
            f'climbing points to a leak or a load that is growing.</div></div>')


def build_html(rows, meta, checks=None, events=None, payload=None, live=False, viewer=None, expand=False,
               usage=None, usage_hist=None):
    s = summarize(rows, meta.get("target_mbps"))
    target = meta.get("target_mbps")
    if payload is not None and rows:
        extra = rtt_pattern_findings(rows, meta)
        if extra:
            payload = dict(payload, findings=list(payload.get("findings") or []) + extra)
    if payload and any(f["confidence"] == "likely" for f in payload["findings"]):
        # a likely payload anomaly must not leave the badge at PASS
        if s["verdict"] == "PASS":
            s["verdict"] = "WARN"
        s["reasons"] = [r for r in s["reasons"] if r[0] != "PASS"] + [
            ("WARN", f"stream contents: {payload['verdict']} (see Anomalies)")]
    lat = latency_view(rows, meta.get("latency"))
    latency = lat["effective"]
    if latency is not None and s["rtt_p95"] > 0 and 3 * s["rtt_p95"] > latency:
        # SRT needs roughly 3-4 x RTT of latency to recover losses; below that, loss goes uncorrected
        if s["verdict"] == "PASS":
            s["verdict"] = "WARN"
        s["reasons"] = [r for r in s["reasons"] if r[0] != "PASS"] + [
            ("WARN", f"latency {latency} ms is below 3 x RTT p95 ({s['rtt_p95']:.1f} ms): "
                     f"raise the latency to at least {3 * s['rtt_p95']:.0f} ms")]
    alarm = meta.get("silence_alarm") or 10.0
    now = current_state(rows, alarm)
    if now["state"] == "no_signal":
        # the stream is gone right now: that is the headline, whatever the run averages say
        s["verdict"] = "FAIL"
        s["reasons"] = [("FAIL", f"NO SIGNAL since {now['since']}: no data for {now['silent_s']:.0f} s. "
                                 f"Check the sender and the network path")] + [r for r in s["reasons"] if r[0] != "PASS"]
    # A/V and latency figures come from samples where a stream was present
    av = av_stats([r for r in rows if r["bitrate_mbps"] >= 0.01])   # statistics and KPI
    av_full = av_stats(rows)                                         # chart, one value per sample
    if av:
        for lvl, text in av_findings(av):
            if lvl == "FAIL":
                s["verdict"] = "FAIL"
            elif s["verdict"] == "PASS":
                s["verdict"] = "WARN"
            s["reasons"] = [r for r in s["reasons"] if r[0] != "PASS"] + [(lvl, text)]
    # stream configuration problems set the verdict; colour and level findings are noted, not counted
    cfg_issues = [("FAIL" if f["level"] == "fail" else "WARN", f"{_pid_text(f['pid'])}: {f['title']}")
                  for f in ((payload.get("config") or []) if payload else []) if f["level"] in ("fail", "warn")]
    pic_issues = [("INFO", f"Colour levels: {f['title']}. Noted here; it does not change the verdict.")
                  for f in ((payload.get("picture_findings") or []) if payload else [])
                  if f["level"] in ("fail", "warn")]
    if cfg_issues and s["verdict"] == "PASS":
        s["verdict"] = "WARN"
    v = s["verdict"]
    issues = [r for r in s["reasons"] if r[0] != "PASS"] + cfg_issues + pic_issues
    # the header shows the state now (the last 5 seconds of the stream); the whole run's history stays in Issues
    last_t = rows[-1]["elapsed_s"] if rows else 0
    recent = [r for r in rows if r["elapsed_s"] > last_t - NOW_WINDOW_S]
    now_s = summarize(recent, meta.get("target_mbps"), window=True, run_start=rows[0]["elapsed_s"]) if recent else s
    v_now = now_s["verdict"]
    now_issues = [r for r in now_s["reasons"] if r[0] != "PASS"]
    started = meta.get("started") or time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    endpoint = ""
    if not meta.get("redact") and (meta.get("mode") or meta.get("port")):
        port = meta.get("port")
        endpoint = f"{meta.get('mode') or ''} {meta.get('host') or '0.0.0.0'}" + (f":{port}" if port else "")
        endpoint = endpoint.strip()
    streamid = meta.get("streamid") if not meta.get("redact") else None
    title = esc(meta.get("title") or "SRT stream report")
    customer = esc(meta.get("customer") or "")

    xs = [r["elapsed_s"] for r in rows]
    times = [r["time"] for r in rows]
    bitrate = [r["bitrate_mbps"] for r in rows]
    loss = [r["loss_pct"] for r in rows]
    rtt = [r["rtt_ms"] for r in rows]

    bitrate_lines = [(s["bitrate_avg"], f"avg {s['bitrate_avg']:.2f}", "#64748b")]
    if target:
        bitrate_lines.append((target, f"target {target:g}", "#7c3aed"))

    loss_tone = "bad" if s["loss_pct"] >= core.LOSS_BAD_PCT else "warn" if s["loss_pct"] >= core.LOSS_WARN_PCT else ""
    rtt_tone = "bad" if s["rtt_p95"] >= core.RTT_BAD_MS else "warn" if s["rtt_p95"] >= core.RTT_WARN_MS else ""
    if now["state"] == "receiving":
        now_kpi = _kpi("Now", f"{now['mbps']:.2f} <small>Mbps</small>", "receiving", tone="")
    elif now["state"] == "gap":
        now_kpi = _kpi("Now", "0.00 <small>Mbps</small>", f"gap: no data for {now['silent_s']:.0f} s", tone="warn")
    elif now["state"] == "no_signal":
        now_kpi = _kpi("Now", "No signal", f"0 Mbps since {now['since']}", tone="bad")
    else:
        now_kpi = _kpi("Now", "Waiting", "no stream data yet", tone="")
    if now["state"] == "no_signal":
        banner = (f'<div class="banner bad"><b>NO SIGNAL</b> since {esc(now["since"])} · no data for '
                  f'{now["silent_s"]:.0f} s · check the sender and the network path</div>')
    elif now["state"] == "gap":
        banner = (f'<div class="banner warn">No data for {now["silent_s"]:.0f} s. The SRT connection is still '
                  f'up; short gaps can be normal on a bursty stream.</div>')
    else:
        banner = ""
    kpis = "".join([
        now_kpi,
        _kpi("Average bitrate", f"{s['bitrate_avg']:.2f} <small>Mbps</small>",
             f"min {s['bitrate_min']:.2f} · peak {s['bitrate_peak']:.2f}", sparkline_svg(bitrate, "#6c5fc7")),
        _kpi("Packet loss", f"{s['loss_pct']:.2f}%", f"{s['lost']:,.0f} of {s['rx_packets'] + s['lost']:,.0f} packets",
             sparkline_svg(loss, "#dc2626"), loss_tone),
        _kpi("RTT p95", f"{s['rtt_p95']:.1f} <small>ms</small>", f"avg {s['rtt_avg']:.1f} · max {s['rtt_max']:.1f}",
             sparkline_svg(rtt, "#0e7490"), rtt_tone),
        _kpi("Latency", _lat_value(lat), _lat_sub(lat)),
        *([_kpi("A/V offset", f"{av['median']:+.0f} <small>ms</small>",
                f"{'audio leads' if av['median'] > 0 else 'audio lags'} · drift {av['drift']:.0f} ms",
                tone="bad" if any(l == "FAIL" for l, _ in av_findings(av)) else
                "warn" if av_findings(av) else "")] if av else []),
        _kpi("Silent time", f"{s['dropout_s']:.0f} <small>s</small>", "zero-bitrate periods",
             tone="bad" if s["dropout_s"] > DROPOUT_FAIL_SECONDS else "warn" if s["dropout_s"] > 0 else ""),
        _kpi("Packets received", f"{s['data_mb']:,.1f} <small>MB</small>", f"{s['rx_packets']:,.0f} packets total"),
        _kpi("Retransmitted", f"{s['retrans']:,.0f}", "recovered by SRT ARQ"),
        _kpi("Dropped", f"{s['dropped']:,.0f}", "too late to recover"),
    ])

    # problems placed on a horizontal UTC timeline where their time is known, and listed as chips
    run_start = meta.get("started") or time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    timed = []
    for ev in (events or []):
        kind = _event_kind(ev[2])
        if kind in ("bad", "warn"):
            try:
                e = _run_epoch(ev[0], run_start)
            except (ValueError, TypeError):
                continue
            timed.append((e, e + 1.0, kind, f"{ev[0]} · {ev[2]}"))
    for f in ((payload or {}).get("findings") or []):
        if f.get("utc") is not None:
            timed.append((f["utc"], (f.get("utc_end") or f["utc"]) + 1.0, "warn", f["what"]))
    if issues:
        chips = "".join(
            f'<span class="iss-chip"><span class="dot {lvl}"></span><span title="{esc(issue_hint(text))}">'
            f'{esc(text)}</span></span>'
            for lvl, text in issues)
        issue_block = _timeline_strip(timed) + f'<div class="iss-chips">{chips}</div>'
    else:
        issue_block = ('<div class="empty"><span class="tick-ok">OK</span>'
                       'All measurements are within thresholds.</div>')

    strip, cell_size = status_strip(rows, target=meta.get("target_mbps"))
    cell_text = f"each cell covers {cell_size:g} s"

    overview = f"""<section id="overview">
  {banner}
  <div class="kpis">{kpis}</div>
  <div class="panel" id="issues"><div class="ph"><h2>Issues</h2><span class="muted small">{len(issues)} open</span></div>{issue_block}</div>
  <div class="panel"><div class="ph"><h2>Stream status</h2><span class="muted small">{esc(cell_text)}</span></div>
    {strip}
    <div class="legend"><span><i style="background:var(--ok)"></i>healthy</span>
      <span><i style="background:var(--warn)"></i>unrecovered loss or RTT above warning</span>
      <span><i style="background:var(--bad)"></i>above failure threshold</span>
      <span><i style="background:var(--down)"></i>no bitrate (silent)</span></div></div>
</section>"""

    link = f"""<section id="link"><div class="ph"><h2>Link performance</h2>
  <span class="muted small">Times in UTC · sampled every {s['interval_s']:g} s · drag across a chart to zoom, double-click to reset</span></div>
  <div class="panel"><div class="ph"><h3>Bitrate</h3><button class="btn sm" type="button" data-png>PNG</button></div>
    {chart_html('Bitrate', xs, times, [('Bitrate', '#6c5fc7', bitrate)], 'Mbps', hlines=bitrate_lines)}</div>
  <div class="stack">
    <div class="panel"><div class="ph"><h3>Packet loss</h3><button class="btn sm" type="button" data-png>PNG</button></div>
      {chart_html('Packet loss', xs, times, [('Loss', '#dc2626', loss)], '% loss')}</div>
    <div class="panel"><div class="ph"><h3>Round-trip time</h3><button class="btn sm" type="button" data-png>PNG</button></div>
      {chart_html('Round-trip time', xs, times, [('RTT', '#0e7490', rtt)], 'ms', hlines=[(core.RTT_WARN_MS, 'warn', '#d97706')])}</div>
  </div></section>"""

    payload_html = payload_section(payload) if payload else ""
    extra = ""
    if payload and any(r.get("mux_mbps") for r in rows):
        extra += _mux_panel(xs, times, rows)
    if payload and av_full:
        extra += _av_panel(xs, times, av_full)
    if payload and extra:
        payload_html = payload_html.rsplit("</section>", 1)[0] + extra + "</section>"
    anomalies_html = anomalies_section(payload) if payload else ""
    config_html = config_section(_all_findings(payload), payload.get("picture") if payload else None) if payload else ""

    checks_html = ""
    if checks:
        lbl = {"ok": ("PASS", "ok"), "warn": ("WARN", "warn"), "fail": ("FAIL", "fail"), "info": ("INFO", "info")}
        body = []
        for lvl, name, detail, fix in checks:
            text, cls = lbl[lvl]
            fix_html = (f"<div class='muted small'>Fix: {esc(fix)}</div>"
                        if fix and lvl in ("warn", "fail") else "")
            body.append(f"<tr><td>{_pill(text, cls)}</td><td><b>{esc(name)}</b>{fix_html}</td>"
                        f"<td>{esc(detail)}</td></tr>")
        checks_html = (f'<section id="checks"><div class="ph"><h2>Pre-flight checks</h2></div>'
                       f'<div class="panel tablewrap"><table><tbody>{"".join(body)}</tbody></table></div></section>')

    events_html = ""
    if events:
        items = "".join(_event_item(ev, expand) for ev in reversed(events[-50:]))
        ticks = []
        for ev in events:
            try:
                e = _run_epoch(ev[0], run_start)
            except (ValueError, TypeError):
                continue
            ticks.append((e, e + 1.0, _event_kind(ev[2]), f"{ev[0]} · {ev[2]}"))
        strip = _timeline_strip(ticks, caption="Every event on the run's clock, UTC. Hover a tick for details.")
        events_html = (f'<section id="events"><div class="ph"><h2>Events</h2>'
                       f'<span class="muted small">newest first</span></div>'
                       f'<div class="panel">{strip}<ul class="timeline">{items}</ul></div></section>')

    info_rows = [("Started", started),
                 ("Thresholds", f"loss {core.LOSS_WARN_PCT}% warn / {core.LOSS_BAD_PCT}% fail; "
                                f"RTT {core.RTT_WARN_MS} ms warn / {core.RTT_BAD_MS} ms fail"),
                 ("Sample interval", f"{s['interval_s']:g} s")]
    if endpoint:
        info_rows.append(("Endpoint", endpoint))
    if streamid:
        info_rows.append(("Stream ID", streamid))
    if lat["local"] is not None or lat["negotiated"]:
        info_rows.append(("Latency set here", f"{lat['local']} ms" if lat["local"] is not None else "default"))
    if lat["negotiated"]:
        note = (f" (peer set {lat['peer']} ms; the larger value applies to both sides)" if lat["peer"]
                else " (matches the peer)" if lat["negotiated"] == lat["local"] else "")
        info_rows.append(("Negotiated latency", f"{lat['negotiated']} ms{note}"))
    else:
        info_rows.append(("Peer latency", "not seen yet (the stream has not reported a negotiated delay)"))
    if target:
        info_rows.append(("Target bitrate", f"{target:g} Mbps"))
    if meta.get("encrypted"):
        key = f"AES-{meta['pbkeylen'] * 8}" if meta.get("pbkeylen") else "AES (SRT default key length)"
        info_rows.append(("Encryption", f"{key}, passphrase set (not recorded here)"))
    else:
        info_rows.append(("Encryption", "none"))
    if usage:
        cpu = usage.get("cpu_pct")
        share = (cpu / usage["cores"]) if cpu is not None else 0.0      # share of the whole PC's CPU
        cpu_text = "measuring..." if cpu is None else f"{share:.1f}% of this PC's CPU"
        note = ""
        if usage.get("video") and (share >= USAGE_NOTE_CPU_SHARE_PCT or usage["rss_mb"] >= USAGE_NOTE_MEM_MB):
            note = (f" Live video is adding a lot of load ({share:.0f}% of this PC's CPU). Keep that in mind, and "
                    f"consider running the check without the video: uncheck \"Live video in the dashboard\" on "
                    f"the Decode stream tab.")
        info_rows.append(("This monitor's load",
                          f"CPU {cpu_text}; memory {usage['rss_mb']:.0f} MB. Counts this monitor and its "
                          f"helpers only ({usage['processes']} processes in all), sampled every 2 s at {usage['time']}."
                          + note))
    info = "".join(f"<dt>{esc(k)}</dt><dd>{esc(val)}</dd>" for k, val in info_rows)
    # thresholds: the values this report was judged against, and how they differ from the defaults
    trows = ""
    for label, val, default, why in thresholds.describe(core._T):
        tag = ' <span class="tag">changed</span>' if val != default else ""
        trows += (f'<tr><td>{esc(label)}</td><td><strong>{esc(val)}</strong>{tag}</td>'
                  f'<td class="muted">{esc(default)}</td><td class="muted">{esc(why)}</td></tr>')
    thr_html = (f'<div class="panel"><h3>Thresholds used for this report</h3>'
                f'<p class="muted small">These levels decided the verdict, the warnings and the failures above. '
                f'To change them, open the control panel and use the Health thresholds card. The change applies '
                f'to the next monitor you start and to reports made after it.</p>'
                f'<div class="tablewrap"><table><thead><tr><th>Threshold</th><th>Used</th>'
                f'<th>Default</th><th>What it does</th></tr></thead><tbody>{trows}</tbody></table></div></div>')
    usage_charts = ""
    if usage_hist:
        usage_charts = ('<div class="spark-row">'
                        + _spark([(t, c / max(1, (usage or {}).get("cores", 1)), w) for t, c, _, w in usage_hist],
                                 "CPU of this monitor, % of this PC", "%",
                                 "var(--accent)")
                        + _spark([(t, m, w) for t, _, m, w in usage_hist], "Memory of this monitor", "MB", "var(--info)")
                        + '</div>')
    details_html = (f'<section id="details"><div class="ph"><h2>Run details</h2></div>'
                    f'<div class="panel">{usage_charts}<dl class="kv-grid">{info}</dl></div>{thr_html}</section>')

    nav_items = [("overview", "Overview"), ("link", "Link")]
    if payload:
        nav_items += [("stream", "Stream"), ("anomalies", "Anomalies"), ("config", "Config")]
    if checks:
        nav_items.append(("checks", "Checks"))
    if events:
        nav_items.append(("events", "Events"))
    nav_items.append(("details", "Run details and thresholds"))
    nav = "".join(f'<button type="button" data-go="{sid}">{label}</button>' for sid, label in nav_items)

    sub_line = []
    if customer:
        sub_line.append(f'<span class="chip">Prepared for <b>{customer}</b></span>')
    if endpoint:
        sub_line.append(f'<span class="chip">{esc(endpoint)}</span>')
    if streamid:
        sub_line.append(f'<span class="chip">streamid <b>{esc(streamid)}</b></span>')
    sub_line.append(f'<span class="chip">{esc(started)}</span>')
    sub_line.append(f'<span class="chip">{core.fmt_hms(s["duration_s"])}</span>')
    if target:
        sub_line.append(f'<span class="chip">target <b>{target:g} Mbps</b></span>')

    live_pill = ('<span class="live" title="Updates every 2 seconds from the running monitor">'
                 '<i></i>LIVE <span id="live-stamp">connected</span></span>') if live else ""
    viewer_band = ""
    viewer_chip = ""
    if live and viewer is not None:
        text = " · ".join(x for x in (viewer.get("state"), viewer.get("note")) if x)
        if viewer.get("errors"):
            text += " · last ffmpeg message: " + viewer["errors"][-1]
        viewer_chip = ('<button class="viewer-chip" id="viewer-chip" type="button">'
                       '<span class="lbl-min">&#9654; Live video</span>'
                       '<span class="lbl-pop">&#9654; Live video is popped out &middot; bring back</span></button>')
        viewer_band = f"""<div class="viewer-wrap min" id="viewer-wrap"><section class="viewer" id="viewer">
  <div class="vhead"><div><div class="vt">Live video</div>
    <div class="muted small" id="viewer-state">{esc(text)}</div></div>
    <div style="display:flex;gap:6px"><button class="btn sm" id="jumplive" type="button">Jump to live</button>
    <button class="btn sm" id="sound" type="button" aria-pressed="false">Sound off</button>
    <button class="btn sm" id="popout" type="button">Pop out</button>
    <button class="btn sm" id="minimize" type="button" aria-label="Minimise the video">&ndash;</button></div></div>
  <div class="vbox"><video id="vid" controls muted autoplay playsinline></video>
    <div class="meter" id="meter" title="Audio level. The sound is not played here; the meter only reads it.">
      <div class="mrow"><span class="mstate">click to start the meter</span></div>
      <div class="mrow"><b>L</b><div class="track"><div class="fill" id="mL-fill"></div></div><span class="mdb" id="mL-db">-</span></div>
      <div class="mrow"><b>R</b><div class="track"><div class="fill" id="mR-fill"></div></div><span class="mdb" id="mR-db">-</span></div>
    </div></div>
  <div class="muted small">About 6 to 10 s behind the stream</div>
</section></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"></script>
<script>
(function () {{
  var v = document.getElementById('vid'), src = '/video/index.m3u8';
  // Audio meter: reads the level of the sound without playing it (nothing is connected to the speakers).
  var meterEl = document.getElementById('meter'), mctx = null, aL = null, aR = null, bufL = null, bufR = null;
  var gainNode = null, soundOn = false;
  function setBar(id, db) {{
    var f = document.getElementById(id + '-fill');
    f.style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + '%';
    f.className = 'fill' + (db > -3 ? ' r' : db > -9 ? ' y' : '');
    document.getElementById(id + '-db').textContent = db <= -60 ? '-inf' : db.toFixed(1) + ' dB';
  }}
  function peakDb(an, buf) {{
    an.getFloatTimeDomainData(buf);
    var p = 0;
    for (var i = 0; i < buf.length; i++) {{ var x = Math.abs(buf[i]); if (x > p) {{ p = x; }} }}
    return p > 0 ? 20 * Math.log(p) / Math.LN10 : -120;
  }}
  function meterTick() {{
    if (mctx && mctx.state === 'running') {{
      var l = peakDb(aL, bufL), r = peakDb(aR, bufR);
      setBar('mL', l); setBar('mR', r);
      meterEl.querySelector('.mstate').textContent = (l < -60 && r < -60) ? 'no audio' : 'audio present';
    }}
    requestAnimationFrame(meterTick);
  }}
  function startMeter() {{
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) {{ meterEl.querySelector('.mstate').textContent = 'meter not supported here'; return; }}
    if (!mctx) {{
      mctx = new AC();
      v.muted = false;   // the meter reads the element; the gain node below keeps the speakers silent
      var src = mctx.createMediaElementSource(v), split = mctx.createChannelSplitter(2);
      aL = mctx.createAnalyser(); aR = mctx.createAnalyser();
      aL.fftSize = 1024; aR.fftSize = 1024;
      bufL = new Float32Array(aL.fftSize); bufR = new Float32Array(aR.fftSize);
      src.connect(split); split.connect(aL, 0); split.connect(aR, 1);
      // the sound output: the element's audio only reaches the speakers through this gain node
      gainNode = mctx.createGain(); gainNode.gain.value = soundOn ? 1 : 0;
      src.connect(gainNode); gainNode.connect(mctx.destination);
      requestAnimationFrame(meterTick);
    }}
    if (mctx.state !== 'running') {{ mctx.resume(); }}
  }}
  meterEl.addEventListener('click', startMeter);
  // sound is off until asked for: browsers only autoplay muted video. The button sits above the
  // meter, not under it, so it can always be reached.
  function setSound(on) {{
    soundOn = on;
    v.muted = false;   // the element stays live for the meter; the gain node controls the speakers
    startMeter();
    if (gainNode) {{ gainNode.gain.value = on ? 1 : 0; }}
    var b = document.getElementById('sound');
    b.textContent = on ? 'Sound on' : 'Sound off';
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  }}
  document.getElementById('sound').addEventListener('click', function () {{ setSound(!soundOn); }});
  v.addEventListener('play', startMeter);
  // the pop-out window asks for ?video=1 (the tab script rewrites the #hash, so a hash cannot be used)
  if (new URLSearchParams(location.search).get('video') === '1') {{ document.body.classList.add('only-video'); }}
  var wrap = document.getElementById('viewer-wrap'), popWin = null;
  var chip = document.getElementById('viewer-chip');
  function popped(on) {{ wrap.classList.toggle('popped', on); chip.classList.toggle('popped', on); }}
  function openWindow() {{
    popWin = window.open('/view?video=1', 'srtdebug-video', 'popup=yes,width=520,height=460');
    if (!popWin) {{ return; }}
    popped(true);
    var t = setInterval(function () {{
      if (!popWin || popWin.closed) {{ clearInterval(t); popWin = null; popped(false); }}
    }}, 1000);
  }}
  v.addEventListener('enterpictureinpicture', function () {{ popped(true); }});
  v.addEventListener('leavepictureinpicture', function () {{ popped(false); }});
  // Pop out opens its own window, which carries the video, its controls and the level meter. (Browser
  // picture-in-picture shows only the picture, so the meter would be lost.)
  document.getElementById('popout').addEventListener('click', function () {{ openWindow(); }});
  document.getElementById('minimize').addEventListener('click', function () {{ wrap.classList.add('min'); }});
  chip.addEventListener('click', function () {{
    if (wrap.classList.contains('popped')) {{
      if (document.pictureInPictureElement) {{ document.exitPictureInPicture(); }}
      if (popWin && !popWin.closed) {{ popWin.close(); }}
      popWin = null; popped(false);
    }} else {{ wrap.classList.toggle('min'); }}
  }});
  function attach() {{
    if (window.Hls && Hls.isSupported()) {{
      var h = new Hls({{liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 4, maxLiveSyncPlaybackRate: 1.2}});
      document.getElementById('jumplive').addEventListener('click', function () {{
        if (h.liveSyncPosition) {{ v.currentTime = h.liveSyncPosition; }}
      }});
      // A stall (the picture not moving for 6 s while playing) restarts loading at the live edge
      var lastT = -1, lastMove = Date.now();
      setInterval(function () {{
        if (v.paused) {{ lastMove = Date.now(); return; }}
        if (v.currentTime !== lastT) {{ lastT = v.currentTime; lastMove = Date.now(); return; }}
        if (Date.now() - lastMove > 6000) {{
          h.stopLoad(); h.startLoad(-1); lastMove = Date.now();
        }}
      }}, 2000);
      h.on(Hls.Events.MANIFEST_PARSED, function () {{ v.play(); }});
      h.loadSource(src);
      h.attachMedia(v);
      h.on(Hls.Events.ERROR, function (e, d) {{
        if (d.fatal) {{ setTimeout(function () {{ h.loadSource(src); }}, 2000); }}
      }});
    }} else if (v.canPlayType('application/vnd.apple.mpegurl')) {{
      v.src = src;
    }}
  }}
  if (window.Hls) {{ attach(); }}
  else {{
    var s = document.querySelector('script[src*="hls.min.js"]');
    s.addEventListener('load', attach);
    s.addEventListener('error', function () {{
      document.getElementById('viewer-state').textContent =
        'The player script could not load from cdnjs.cloudflare.com. This machine needs internet access for it.';
    }});
  }}
}})();
</script>"""
    live_js = ""
    if live:
        live_js = """<script>
(function () {
  function refresh() {
    fetch('/live', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) { throw new Error(r.status); }
      return r.text();
    }).then(function (text) {
      var doc = new DOMParser().parseFromString(text, 'text/html');
      var main = doc.querySelector('main'), status = doc.querySelector('.status');
      if (main) {
        var openKeys = [].slice.call(document.querySelectorAll('details[open][data-key]'))
          .map(function (d) { return d.getAttribute('data-key'); });
        document.querySelector('main').innerHTML = main.innerHTML;
        document.querySelectorAll('details[data-key]').forEach(function (d) {
          if (openKeys.indexOf(d.getAttribute('data-key')) >= 0) { d.open = true; }
        });
        if (window.srtShow) { window.srtShow(window.srtCurrent()); }
        if (window.srtPanelDecorate) { window.srtPanelDecorate(); }
      }
      // the health pill sits in the header, outside main, so it is swapped on its own each refresh
      var newPill = doc.querySelector('.bar-health'), oldPill = document.querySelector('.bar-health');
      if (newPill && oldPill) { oldPill.outerHTML = newPill.outerHTML; }
      document.getElementById('live-stamp').textContent = 'updated ' + new Date().toLocaleTimeString();
      var vs = doc.getElementById('viewer-state'), vc = document.getElementById('viewer-state');
      if (vs && vc) { vc.textContent = vs.textContent; }
    }).catch(function () {
      document.getElementById('live-stamp').textContent = 'connection lost, retrying';
    });
  }
  setInterval(refresh, 2000);
})();
</script>
"""
    exports_html = ""
    if live:
        exports_html = ('<div class="exports"><a class="btn" href="/export.csv" download="srtdebug-samples.csv">'
                        'CSV</a><a class="btn" href="/export.pdf" download="srtdebug-report.pdf">PDF</a></div>')
    headline = SEVERITY_TEXT[v_now]
    # the health verdict, pinned in the live dashboard's top bar so it is visible while scrolling
    if v_now != "PASS":
        summary = f"{len(now_issues)} open issue{'s' if len(now_issues) != 1 else ''} in the last 5 seconds · click to see them"
    elif issues:
        summary = (f"healthy over the last 5 seconds · {len(issues)} issue{'s' if len(issues) != 1 else ''} "
                   "earlier in this run · click to see them")
    else:
        summary = "No open issues"
    # live: the one health indicator is this pill, in the top bar; clicking it lists the open issues
    bar_health = (f'<button class="bar-health {v_now}" type="button" onclick="srtShowIssues()" '
                  f'title="{esc(summary)}. Click to see the open issues.">{esc(headline)}</button>') if live else ""
    # static reports have no top bar, so they keep the status box in the page header
    status_box = "" if live else (
        f'<div class="status {v_now} status-link" role="button" tabindex="0" title="Click to see the open issues"'
        f' onclick="srtShowIssues()" onkeydown="if (event.key === \'Enter\') srtShowIssues()">'
        f'<span class="dotlg"></span><div><div class="big">{headline}</div>'
        f'<div class="sub">{esc(summary)}</div></div></div>')
    logo =('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-6 4 12 2-6h6" fill="none" '
            'stroke="var(--accent)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - SRT report</title>
<style>{CSS}</style></head>
<body>
<header class="top"><div class="top-in">
  <div class="brand">{logo}<span>srtdebug</span></div>
  <div class="crumb">Reports / {title}</div>
  {live_pill}
  {exports_html}
  <div class="spacer"></div>
  {bar_health}{viewer_chip}
  <button class="btn" id="theme" type="button" aria-label="Toggle light and dark theme">Toggle theme</button>
</div></header>
{viewer_band}
<main class="wrap">
  <div class="hero">
    <div style="min-width:0">
      <h1>{title}</h1>
      <div class="chips">{''.join(sub_line)}</div>
    </div>
    {status_box}
  </div>
  <nav class="tabs">{nav}</nav>
  <div class="zoom-bar" id="zoom-bar" hidden role="status"><span id="zoom-text"></span>
    <button class="btn sm" type="button" id="zoom-reset">Reset zoom</button></div>
  {_tab(overview, "overview")}
  {_tab(link, "link")}
  {_tab(payload_html, "stream")}
  {_tab(anomalies_html, "anomalies")}
  {_tab(config_html, "config")}
  {_tab(checks_html, "checks")}
  {_tab(events_html, "events")}
  {_tab(details_html, "details")}
  <footer>srtdebug {VERSION}</footer>
</main>
<script>
(function () {{
  var root = document.documentElement, btn = document.getElementById('theme');
  function isDark() {{
    var t = root.getAttribute('data-theme');
    return t === 'dark';
  }}
  btn.addEventListener('click', function () {{ root.setAttribute('data-theme', isDark() ? 'light' : 'dark'); }});
}})();
</script>
{CHART_JS}
{UI_JS}
{TILE_JS}
{live_js}</body></html>
"""



def write_report(out_path, rows, meta, checks=None, events=None, payload=None, expand=False):
    doc = build_html(rows, meta, checks=checks, events=events, payload=payload, expand=expand)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, out_path)
    return out_path


PDF_BROWSERS = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


# Windows browsers, reached from WSL through /mnt/c (WSL can run .exe files directly)
WIN_BROWSERS = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
]


def find_browser():
    """First Chrome, Chromium or Edge that exists: a name on PATH, a full install path, or,
    under WSL, a Windows install."""
    for name in PDF_BROWSERS:
        if os.path.isabs(name):
            if os.path.isfile(name) and os.access(name, os.X_OK):
                return name
        else:
            path = shutil.which(name)
            if path:
                return path
    for path in WIN_BROWSERS:
        if os.path.isfile(path):
            return path
    return None


def _write_pdf_windows(browser, html_path, pdf_path):
    """WSL: print with a Windows browser. Its files must sit in a Windows folder, so the report is
    copied to the Windows temp folder and the PDF is copied back."""
    def run(cmd, **kw):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kw)
    try:
        win_tmp = run(["cmd.exe", "/c", "echo %TEMP%"], cwd="/mnt/c").stdout.strip()
        lin_tmp = run(["wslpath", "-u", win_tmp]).stdout.strip()
        if not lin_tmp or not os.path.isdir(lin_tmp):
            raise RuntimeError(f"could not find the Windows temp folder ({win_tmp or 'no answer from cmd.exe'})")
        work = tempfile.mkdtemp(prefix="srtdebug-pdf-", dir=lin_tmp)
        shutil.copy(html_path, os.path.join(work, "report.html"))
        html_w = run(["wslpath", "-w", os.path.join(work, "report.html")]).stdout.strip()
        pdf_work = os.path.join(work, "report.pdf")
        pdf_w = run(["wslpath", "-w", pdf_work]).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"PDF conversion with the Windows browser failed: {e}")
    cmd = [browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
           f"--print-to-pdf={pdf_w}", "file:///" + html_w.replace("\\", "/")]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("the Windows browser did not finish within 90 seconds; try again, or use the HTML report")
    except OSError as e:
        raise RuntimeError(f"could not start the Windows browser: {e}")
    if p.returncode != 0 or not os.path.exists(pdf_work):
        raise RuntimeError(f"PDF conversion failed (exit {p.returncode}): {(p.stderr or '').strip()[-300:]}")
    shutil.copy(pdf_work, pdf_path)
    shutil.rmtree(work, ignore_errors=True)
    return pdf_path


def write_pdf(html_path, pdf_path):
    """Print the HTML report to PDF with headless Chrome or Chromium (same layout as the HTML)."""
    browser = find_browser()
    if not browser:
        raise RuntimeError("PDF output needs Chrome, Chromium or Edge, and none was found. Searched: "
                           + ", ".join(PDF_BROWSERS + WIN_BROWSERS) + ". Install Chrome (or sudo apt install chromium-browser), "
                           "or open the .html report and use Print > Save as PDF.")
    if browser.endswith(".exe"):
        return _write_pdf_windows(browser, html_path, pdf_path)
    # Same flags as before: a fresh --user-data-dir made headless Chrome hang, so none is set.
    # The timeout stops a stuck browser from holding the request open.
    cmd = [browser, "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
           f"--print-to-pdf={os.path.abspath(pdf_path)}", "file://" + os.path.abspath(html_path)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        raise RuntimeError("the browser did not finish within 90 seconds; try again, or use the HTML report")
    except OSError as e:
        raise RuntimeError(f"PDF conversion failed: {e}")
    if p.returncode != 0 or not os.path.exists(pdf_path):
        raise RuntimeError(f"PDF conversion failed (exit {p.returncode}): {(p.stderr or '').strip()[-300:]}")
    return pdf_path


def write_output(out_path, rows, meta, checks=None, events=None, payload=None):
    """Write the report as HTML, or as PDF when out_path ends in .pdf."""
    if out_path.lower().endswith(".pdf"):
        tmp_dir = tempfile.mkdtemp(prefix="srtdebug-report-")
        html_path = os.path.join(tmp_dir, "report.html")
        try:
            write_report(html_path, rows, meta, checks=checks, events=events, payload=payload, expand=True)
            return write_pdf(html_path, out_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return write_report(out_path, rows, meta, checks=checks, events=events, payload=payload)


def report_from_file(source, out_path, meta):
    rows = load_rows(source)
    return write_output(out_path, rows, meta)
