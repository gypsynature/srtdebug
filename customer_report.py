"""The customer and management report for one saved run: what was streamed, what was measured on the SRT
stream, pass or fail for each check, diagrams, and the assumptions behind the result. It is written in plain
words and built from the run's own summary.json and run.csv, so it can be made again at any time.
It covers the SRT stream only, not how srtdebug itself is running."""
import csv
import datetime
import html
import json
import os
import re
import sys
import tempfile

import runhistory

HTML_NAME = "customer-report.html"
PDF_NAME = "customer-report.pdf"

ACCENT = "#1d6fb8"
GREEN = "#17803d"
AMBER = "#b54708"
RED = "#b42318"
MUTED = "#5b6770"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(run_dir):
    """The run's summary and its numbers (one row a sample, numbers only, the time column dropped)."""
    with open(os.path.join(run_dir, runhistory.SUMMARY), encoding="utf-8") as f:
        summary = json.load(f)
    rows = []
    try:
        with open(os.path.join(run_dir, runhistory.NUMBERS), encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                rows.append({k: _num(v) for k, v in r.items() if k != "time"})
    except OSError:
        pass
    return summary, rows


def _seconds(summary, rows):
    try:
        a = datetime.datetime.fromisoformat(summary["started"])
        b = datetime.datetime.fromisoformat(summary["ended"])
        return max(0.0, (b - a).total_seconds())
    except (KeyError, TypeError, ValueError):
        return float(len(rows))


def _span(seconds):
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {sec} s"
    return f"{sec} s"


def _fmt(v):
    if v is None:
        return ""
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _details(role_list, prefix):
    for st in role_list or []:
        if st.get("role", "").startswith(prefix) and st.get("details"):
            return {k: v for k, v in st["details"]}, st
    return None, None


def _checks(summary):
    rep = summary.get("report") or {}
    return rep.get("checks") or [], rep


def _destination(summary):
    s = summary.get("settings") or {}
    if summary.get("kind") == "monitor":
        if s.get("mode") == "caller":
            return f"from {s.get('host')}:{s.get('port')}", "received"
        return f"on UDP port {s.get('port')}", "received"
    return f"to {s.get('host') or s.get('url') or 'the receiver'}", "sent"


import itertools as _itertools

_CHART_IDS = _itertools.count(1)


def _chart(lines, seconds, unit):
    """An inline SVG line chart with soft area shading. lines: [(label, colour, values)]; None values leave a gap."""
    width, height, left, right, top, bottom = 800, 230, 58, 16, 14, 34
    n = max((len(v) for _, _, v in lines), default=0)
    values = [x for _, _, v in lines for x in v if x is not None]
    if n < 2 or not values:
        return '<p class="muted">No numbers were saved for this chart.</p>'
    peak = max(values) or 1.0
    uid = f"cg{next(_CHART_IDS)}"

    def x_at(i):
        return left + (width - left - right) * i / (n - 1)

    def y_at(v):
        return top + (height - top - bottom) * (1 - v / peak)

    base = height - bottom
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">', '<defs>']
    for k, (_, colour, _) in enumerate(lines):
        out.append(f'<linearGradient id="{uid}-{k}" x1="0" y1="0" x2="0" y2="1">'
                   f'<stop offset="0" stop-color="{colour}" stop-opacity="0.36"/>'
                   f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/></linearGradient>')
    out.append('</defs>')
    out.append(f'<rect x="{left}" y="{top}" width="{width - left - right}" height="{base - top}" rx="10" class="plot"/>')
    for frac in (0, 0.5, 1):
        y = top + (height - top - bottom) * (1 - frac)
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis" text-anchor="end">'
                   f'{_fmt(peak * frac)}{unit}</text>')
    out.append(f'<text x="{left}" y="{height - 8}" class="axis">start</text>')
    out.append(f'<text x="{width - right}" y="{height - 8}" class="axis" text-anchor="end">'
               f'{_span(seconds)}</text>')
    for k, (_, colour, vals) in enumerate(lines):
        pts = [(x_at(i), y_at(v)) for i, v in enumerate(vals) if v is not None]
        if len(pts) < 2:
            continue
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        if k == 0:
            area = d + f" L{pts[-1][0]:.1f},{base} L{pts[0][0]:.1f},{base} Z"
            out.append(f'<path d="{area}" fill="url(#{uid}-{k})" stroke="none"/>')
        out.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="8" stroke-opacity="0.14" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        out.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="2.4" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        ex, ey = pts[-1]
        out.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="6" fill="#ffffff" stroke="{colour}" stroke-width="2.6"/>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:{c}"></i>{html.escape(label)}</span>'
                     for label, c, _ in lines)
    return "".join(out) + f'<div class="legend">{legend}</div>'


def _series(rows, *keys):
    for key in keys:
        vals = [r.get(key) for r in rows]
        if any(v is not None for v in vals):
            return vals
    return []


def _ring(share, pct, uid):
    """A donut showing how much of the measured result passed (white, for the coloured verdict panel)."""
    r = 44
    c = 2 * 3.14159265 * r
    dash = c * max(0.0, min(1.0, share))
    return (f'<svg class="ring" viewBox="0 0 112 112" role="img" aria-label="{pct}% of measured checks passed">'
            f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#d9f5e4"/></linearGradient></defs>'
            f'<circle cx="56" cy="56" r="{r}" fill="rgba(255,255,255,.12)" stroke="rgba(255,255,255,.26)" stroke-width="10"/>'
            f'<circle cx="56" cy="56" r="{r}" fill="none" stroke="url(#{uid})" stroke-width="10" stroke-linecap="round" '
            f'stroke-dasharray="{dash:.1f} {c:.1f}" transform="rotate(-90 56 56)"/>'
            f'<text x="56" y="63" text-anchor="middle" class="ring-pct">{pct}%</text></svg>')


def _spark(values, colour):
    """A small shaded trend line for a tile. Gaps (None) are skipped."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    W, H = 120, 34
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = [(W * i / (n - 1), H - 4 - (H - 8) * (v - lo) / span) for i, v in enumerate(values) if v is not None]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    uid = f"sp{next(_CHART_IDS)}"
    area = d + f" L{pts[-1][0]:.1f},{H} L{pts[0][0]:.1f},{H} Z"
    lx, ly = pts[-1]
    return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">'
            f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{colour}" '
            f'stop-opacity="0.38"/><stop offset="1" stop-color="{colour}" stop-opacity="0"/></linearGradient></defs>'
            f'<path d="{area}" fill="url(#{uid})"/>'
            f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="1.8" stroke-linejoin="round" '
            f'stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>')


def _strip(checks):
    """One coloured cell per measured check: a glance at the whole run."""
    cells = "".join(f'<span class="cell {c["status"]}" title="{html.escape(c["name"])}: {html.escape(str(c["value"]))}"></span>'
                    for c in checks if c["status"] in ("pass", "warn", "fail", "note"))
    return f'<div class="strip" aria-label="One cell per measured check">{cells}</div>' if cells else ""


def _rain(width=1000, height=260, seed=11):
    """Matrix-style code rain for the hero: falling columns of glyphs, head bright, trail fading."""
    import random
    rnd = random.Random(seed)
    glyphs = "01ABCDEFGHJKLMNPQRSTUVWXYZ#%&*+<>=/\\|"
    out = [f'<svg class="rain" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid slice" aria-hidden="true">']
    for col in range(0, width, 18):
        delay = round(rnd.uniform(0, 6), 2)
        dur = round(rnd.uniform(3, 7), 2)
        out.append(f'<g class="col" style="animation-delay:-{delay}s;animation-duration:{dur}s">')
        head = rnd.uniform(0, height)
        for k in range(12):
            y = (head - k * 14) % height
            op = max(0.08, 1 - k * 0.09)
            fill = "#eaf5ff" if k == 0 else "#3fa9ff"
            out.append(f'<text x="{col}" y="{y:.0f}" fill="{fill}" opacity="{op:.2f}">{rnd.choice(glyphs)}</text>')
        out.append('</g>')
    out.append('</svg>')
    return "".join(out)


def build_html(summary, rows, version=""):
    seconds = _seconds(summary, rows)
    checks, rep = _checks(summary)
    streams = rep.get("streams") or []
    vd, _ = _details(streams, "Video")
    ad, _ = _details(streams, "Audio")
    totals = summary.get("totals") or {}
    settings = summary.get("settings") or {}
    dest, verb = _destination(summary)
    is_monitor = summary.get("kind") == "monitor"

    measured = [c for c in checks if c["status"] in ("pass", "warn", "fail")]
    counts = rep.get("counts") or {}
    overall = rep.get("overall") or "not analysed"
    problems = [c for c in checks if c["status"] in ("warn", "fail", "note")]
    verdict_word = {"pass": "PASS", "warn": "PASS WITH WARNINGS", "fail": "NEEDS ATTENTION"}.get(overall, "NOT ANALYSED")
    if not any(v is not None for r in rows for v in r.values()):
        overall = "none"
        verdict_word = "NO DATA"

    # what was sent, in one sentence
    if vd:
        video = ", ".join(x for x in (vd.get("Codec"), vd.get("Resolution"), vd.get("Frame rate")) if x)
        if vd.get("Scan") and vd.get("Scan") != "Progressive":
            video += f", {vd['Scan'].lower()}"
    else:
        video = None
    audio = ", ".join(x for x in (ad.get("Codec"), ad.get("Channels"), ad.get("Sample rate")) if x) if ad else None
    what = " with ".join(x for x in (video and f"{video} video", audio and f"{audio} audio") if x) or "an SRT stream"
    headline = f"This test {verb} {what} {dest}, for {_span(seconds)}."
    if verdict_word == "NO DATA":
        headline = "No stream data was received during this run, so there is nothing to report on."

    avg_rate = [c for c in checks if c["name"] == "Average bitrate"]
    avg_rate = avg_rate[0]["value"] if avg_rate else ""
    null_share = [c for c in checks if c["name"] == "Null packets"]
    null_share = null_share[0]["value"] if null_share else ""

    rtt = _series(rows, "rtt_ms")
    rtt_vals = [v for v in rtt if v is not None]
    mean_rtt = sum(rtt_vals) / len(rtt_vals) if rtt_vals else None

    e = html.escape
    brand = "srtdebug" + (f" {version}" if version else "")

    def tile(label, value, sub="", spark=""):
        return (f'<div class="tile"><div class="tk">{e(label)}</div><div class="tv">{e(str(value))}</div>'
                f'<div class="ts">{e(sub)}</div>{spark}</div>')

    passed = counts.get("pass", 0)
    ring_html = _ring(passed / len(measured), round(100 * passed / len(measured)), f"rg{next(_CHART_IDS)}") if measured else ""
    br_vals = _series(rows, "mux_mbps", "bitrate_mbps") if is_monitor else _series(rows, "rate_mbps", "bitrate_mbps")
    tiles = "".join([
        tile("Stream time", _span(seconds), "from start to stop"),
        tile("Average bitrate", avg_rate or "not measured", "all streams together", _spark(br_vals, ACCENT)),
        tile("Worst round trip", f"{_fmt(totals.get('worst_rtt_ms'))} ms" if totals.get("worst_rtt_ms") is not None
             else "not measured", f"average {_fmt(mean_rtt)} ms" if mean_rtt is not None else "", _spark(rtt, ACCENT)),
        tile("Lost / resent / dropped", f"{totals.get('lost', 0)} / {totals.get('resent', 0)} / "
             f"{totals.get('dropped', 0)}", "packets, for this link"),
        tile("Latency setting", f"{settings.get('latency', '')} ms" if settings.get("latency") else "default",
             "encryption on" if settings.get("encrypted") else "encryption off"),
    ])

    stream_rows = ""
    for label, d in (("Video", vd), ("Audio", ad)):
        if not d:
            continue
        keys = (["Codec", "Resolution", "Frame rate", "Scan", "Chroma subsampling", "Bit depth", "Primaries",
                 "Transfer", "Range"] if label == "Video"
                else ["Codec", "Sample rate", "Channels", "Declared bit rate"])
        for k in keys:
            if d.get(k) and d.get(k) != "not signalled":
                stream_rows += f"<tr><th>{e(label)} · {e(k)}</th><td>{e(str(d[k]))}</td></tr>"
    if not stream_rows:
        stream_rows = '<tr><td class="muted">No stream details were captured for this run.</td></tr>'

    def check_row(c):
        st = c["status"]
        mark = {"pass": "PASS", "warn": "WARN", "fail": "ATTENTION", "note": "NOTE"}.get(st, "NOT CHECKED")
        cls = st.replace(" ", "-")
        return (f'<div class="chk {cls}"><span class="pill {cls}">{mark}</span>'
                f'<div><div class="cn">{e(c["name"])}</div>'
                f'<div class="cv">{e(c["value"])}</div>'
                f'<div class="cx">{e(c.get("note") or "")}</div></div></div>')

    scored = [c for c in checks if c["status"] in ("pass", "warn", "fail", "note")]
    measurements = [c for c in checks if c["status"] == "info" and c["value"] != "not carried by this stream"]
    not_scored = [c for c in checks if c["status"] not in ("pass", "warn", "fail", "note", "info")
                  or c["value"] == "not carried by this stream"]
    groups = {}
    for c in scored:
        groups.setdefault(c["group"], []).append(c)
    strip_html = _strip(checks)
    check_html = "".join(
        f'<h3>{e(g)}</h3>' + "".join(check_row(c) for c in items) for g, items in groups.items())
    not_html = "".join(f'<li><b>{e(c["name"])}</b>: {e(c["value"])}</li>' for c in not_scored)

    problem_html = "".join(
        f'<li><b>{e(c["name"])}</b> — {e(c["value"])}. {e(c.get("note") or "")}</li>' for c in problems)
    if not problem_html:
        problem_html = "<li>Every measured check passed.</li>"

    pcr_note = ""
    early = [c for c in checks if "set-up" in (c.get("value") or "")]
    if early:
        pcr_note = ("<p class=\"muted\">Some events were seen while the connection was being set up. They are "
                    "noted in the checks but not counted, because joining any stream produces a few.</p>")

    charts = []
    charts.append(("Round trip (ms)", _chart([("Round trip", ACCENT, rtt)], seconds, " ms")
                   if rtt_vals else '<p class="muted">Round trip was not recorded.</p>'))
    rate_lines = []
    if is_monitor:
        mux = _series(rows, "mux_mbps", "bitrate_mbps")
        vid = _series(rows, "video_mbps")
        aud = _series(rows, "audio_mbps")
        if mux:
            rate_lines.append(("Stream", ACCENT, mux))
        if vid and any(v for v in vid if v):
            rate_lines.append(("Video", GREEN, vid))
        if aud and any(v for v in aud if v):
            rate_lines.append(("Audio", AMBER, aud))
    else:
        rate_lines.append(("Sent", ACCENT, _series(rows, "rate_mbps", "bitrate_mbps")))
    charts.append(("Bitrate (Mb/s)", _chart(rate_lines, seconds, " Mb/s") if rate_lines and any(
        any(v is not None for v in vals) for _, _, vals in rate_lines)
        else '<p class="muted">Bitrate was not recorded.</p>'))
    charts.append(("Lost and dropped packets (running total)", _chart(
        [("Lost", AMBER, _series(rows, "lost")), ("Dropped", RED, _series(rows, "dropped"))], seconds, "")))
    chart_html = "".join(f'<section class="card"><h3>{e(t)}</h3>{body}</section>' for t, body in charts)
    measure_html = "".join(f'<tr><th>{e(c["name"])}</th><td>{e(c["value"])}</td></tr>' for c in measurements)

    assumptions = [
        "The numbers come from this computer during this test. Round trip, loss and resends are what the SRT "
        "link reported for this one connection. They describe that link, not the whole network path.",
        "Stream checks follow ETSI TR 101 290, the broadcast standard for digital transport streams. Priority 1 "
        "and 2 are the core checks. A check marked as not measured is not evaluated, and it is not counted as a pass.",
        "The first 15 seconds after connecting are reported but not counted. Joining a stream always shows a few "
        "events while it settles.",
        "The audio and video offset is estimated from the stream's timestamps. Treat it as indicative, not exact.",
        "The result describes this stream, this network path and this time. It does not show how other content, "
        "other receivers or other networks will behave.",
    ]
    if not is_monitor:
        assumptions.insert(1, "The stream analysed is the one actually sent, read from the bytes on the wire, so it "
                              "reflects any re-encoding set for this run.")
    assumption_html = "".join(f"<li>{e(a)}</li>" for a in assumptions)

    started = summary.get("started", "")
    label = report_label(summary)
    run_label = summary.get("label", "")
    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = f"{label} · srtdebug {version}".strip()

    css = """
    :root { --ink:#16191f; --muted:#5b6770; --line:#e4e7ec; --card:#ffffff; --bg:#f3f5f8; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, system-ui, "Segoe UI", Roboto, sans-serif; }
    main { max-width: 980px; margin: 0 auto; padding: 32px 24px 48px; }
    h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -.01em; }
    h2 { font-size: 18px; margin: 30px 0 12px; }
    h3 { font-size: 15px; margin: 16px 0 8px; }
    .muted { color: var(--muted); }
    .card { background: var(--card); border:1px solid var(--line); border-radius: 16px; padding: 18px 20px; margin-bottom: 14px;
            box-shadow: 0 1px 2px rgba(16,24,40,.06), 0 10px 24px -14px rgba(16,24,40,.25); }
    .card h3 { margin: 0 0 10px; }
    .verdict { border-radius:16px; padding:22px 24px; color:#fff; }
    .verdict { position: relative; overflow: hidden;
               box-shadow: 0 2px 4px rgba(16,24,40,.12), 0 16px 32px -18px rgba(16,24,40,.55); }
    .verdict::after { content: ""; position: absolute; right: -40px; top: -60px; width: 200px; height: 200px;
                      border-radius: 50%; background: radial-gradient(circle, rgba(255,255,255,.22), rgba(255,255,255,0) 70%); }
    .verdict { padding-right: 150px; }
    .verdict .ring { position: absolute; right: 22px; top: 50%; transform: translateY(-50%); width: 112px; height: 112px; }
    .verdict .ring-pct { fill: #ffffff; font-size: 21px; font-weight: 700; }
    .tile .spark { display: block; width: 100%; height: 34px; margin-top: 6px; }
    .strip { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 14px; }
    .strip .cell { width: 20px; height: 20px; border-radius: 6px; box-shadow: inset 0 1px 0 rgba(255,255,255,.35), 0 2px 4px -2px rgba(0,0,0,.35); }
    .strip .cell.pass { background: linear-gradient(180deg, #2fb36b, #137a3d); }
    .strip .cell.warn { background: linear-gradient(180deg, #e0822f, #9a4308); }
    .strip .cell.fail { background: linear-gradient(180deg, #e95e50, #a11d14); }
    .verdict.pass { background: linear-gradient(135deg, #22a55c 0%, #137a3d 100%); }
    .verdict.warn { background: linear-gradient(135deg, #d9731a 0%, #9a4308 100%); }
    .verdict.fail { background: linear-gradient(135deg, #e0483a 0%, #a11d14 100%); }
    .verdict.none { background: linear-gradient(135deg, #6b7782 0%, #46515b 100%); }
    .verdict .word { font-size: 13px; letter-spacing:.08em; font-weight:700; opacity:.9; }
    .verdict .head { font-size: 19px; margin-top: 6px; }
    .verdict .sub { margin-top: 8px; opacity:.95; }
    .tiles { display:grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-top: 14px; }
    .tile { background: linear-gradient(180deg, var(--card) 0%, rgba(127,140,160,.07) 100%); border:1px solid var(--line);
            border-top: 3px solid #0a84ff; border-radius: 14px; padding: 12px 14px;
            box-shadow: 0 1px 2px rgba(16,24,40,.06), 0 8px 18px -12px rgba(16,24,40,.3); }
    .tk { font-size: 12px; color: var(--muted); }
    .tv { font-size: 19px; font-weight: 700; margin-top: 2px; }
    .ts { font-size: 12px; color: var(--muted); margin-top: 2px; }
    table.kv { width:100%; border-collapse: collapse; }
    table.kv th, table.kv td { text-align:left; padding: 8px 10px; border-bottom:1px solid var(--line); vertical-align: top; }
    table.kv th { width: 42%; color: var(--muted); font-weight: 500; }
    .chart { width:100%; height:auto; display:block; max-height: 260px; }
    .chart .grid { stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 4; }
    .chart .plot { fill: rgba(127,140,160,.06); stroke: var(--line); stroke-width: 1; }
    .chart .axis { font-size: 12px; fill: var(--muted); }
    .legend { display:flex; gap:16px; font-size:13px; color: var(--muted); margin-top: 6px; }
    .legend i { display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:6px; vertical-align:middle; }
    .chk { display:flex; gap:12px; align-items:flex-start; padding: 9px 0; border-bottom: 1px solid var(--line); }
    .chk:last-child { border-bottom: 0; }
    .pill { font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 999px; color:#fff; min-width: 58px; text-align:center;
            background: linear-gradient(180deg, #6b7782, #46515b); box-shadow: inset 0 1px 0 rgba(255,255,255,.25); }
    .pill.pass { background: linear-gradient(180deg, #2fb36b, #137a3d); }
    .pill.warn { background: linear-gradient(180deg, #e0822f, #9a4308); }
    .pill.note { background: linear-gradient(180deg, #5b8fd6, #2d5fa6); }
    .strip .cell.note { background: linear-gradient(180deg, #5b8fd6, #2d5fa6); }
    .pill.fail { background: linear-gradient(180deg, #e95e50, #a11d14); }
    .cn { font-weight: 600; }
    .cv { font-size: 14px; }
    .cx { font-size: 13px; color: var(--muted); margin-top: 2px; }
    ul.plain { padding-left: 20px; margin: 6px 0; }
    ul.plain li { margin: 6px 0; }
    footer { margin-top: 34px; font-size: 12px; color: var(--muted); }
    .hero { position: relative; overflow: hidden; border-radius: 22px; padding: 30px 32px 34px; margin-bottom: 18px; color: #e8f0ff;
            background: radial-gradient(ellipse 60% 80% at 85% 0%, rgba(10,132,255,.55), rgba(10,132,255,0) 70%),
                        radial-gradient(ellipse 50% 70% at 0% 100%, rgba(34,165,92,.30), rgba(34,165,92,0) 70%),
                        linear-gradient(165deg, #0d1628 0%, #060a14 65%, #03050b 100%);
            box-shadow: 0 1px 2px rgba(0,0,0,.2), 0 24px 40px -22px rgba(10,60,140,.8); }
    .hero .stars { position: absolute; inset: 0; width: 100%; height: 100%; }
    .hero .kicker { position: relative; font-size: 11px; font-weight: 700; letter-spacing: .22em; text-transform: uppercase; color: #7fb8ff; }
    .hero h1 { position: relative; color: #ffffff; font-size: 34px; letter-spacing: -.01em; margin: 8px 0 4px; }
    .hero .meta { position: relative; color: #9fb3cc; font-size: 14px; }
    .hero .horizon { position: absolute; left: -10%; right: -10%; bottom: -1px; height: 26px;
                     background: linear-gradient(180deg, rgba(10,132,255,0), rgba(10,132,255,.55) 60%, rgba(127,184,255,.9));
                     filter: blur(6px); opacity: .75; }
    @media screen {
      .hero::after { content: ""; position: absolute; left: 0; right: 0; top: -40%; height: 40%; pointer-events: none;
                     background: linear-gradient(180deg, rgba(127,184,255,0), rgba(127,184,255,.12), rgba(127,184,255,0));
                     animation: sweep 6s linear infinite; }
      .verdict .ring { animation: ringin 1.1s cubic-bezier(.2,.8,.2,1) both; }
      @keyframes sweep { from { top: -40%; } to { top: 110%; } }
      @keyframes ringin { from { opacity: 0; transform: translateY(-50%) scale(.6) rotate(-90deg); }
                          to { opacity: 1; transform: translateY(-50%) scale(1) rotate(0deg); } }
      @media (prefers-reduced-motion: reduce) { .hero::after, .verdict .ring { animation: none; } }
    }
    @media print { .hero { -webkit-print-color-adjust: exact; print-color-adjust: exact; } }
    /* industrial pass: hard uppercase type, hazard stripes, scan lines and a glitch on the title */
    .hero { background: radial-gradient(ellipse 50% 90% at 0% 0%, rgba(255,138,31,.16), rgba(255,138,31,0) 70%),
                        radial-gradient(ellipse 55% 90% at 85% 100%, rgba(63,169,255,.22), rgba(63,169,255,0) 70%),
                        linear-gradient(170deg, #06142a 0%, #030814 70%, #01040a 100%);
            border: 1px solid #1d1d1d; }
    .hero .kicker { color: #ff8a1f; font-family: Bahnschrift, "Arial Narrow", Impact, "Haettenschweiler", sans-serif; letter-spacing: .3em; }
    .hero h1 { font-family: Bahnschrift, "Arial Narrow", Impact, "Haettenschweiler", "Franklin Gothic Condensed", sans-serif;
               font-size: 46px; text-transform: uppercase; letter-spacing: .04em; font-weight: 800; line-height: 1; }
    .hero .meta { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: 12px; letter-spacing: .04em; color: #7fb8ff; }
    .hero .hazard { display: none; position: absolute; left: 0; right: 0; bottom: 0; height: 12px; z-index: 1;
                    background: repeating-linear-gradient(-45deg, #ffd400 0 14px, #0a0a0a 14px 28px); }
    .hero.warn .hazard, .hero.fail .hazard { display: block; }
    .hero .horizon { display: none; }
    h2 { font-family: Bahnschrift, "Arial Narrow", Impact, sans-serif; text-transform: uppercase; letter-spacing: .12em; font-size: 15px; font-weight: 800; }
    h2::before { content: "//"; color: #ff8a1f; margin-right: 8px; }
    .card { border-top: 2px solid #3fa9ff; }
    .verdict { border-radius: 6px; }
    .verdict .word { font-family: Bahnschrift, "Arial Narrow", Impact, sans-serif; font-size: 15px; letter-spacing: .24em; }
    .verdict .head { font-family: Bahnschrift, "Arial Narrow", sans-serif; letter-spacing: .01em; font-weight: 700; }
    .tile { border-radius: 4px; border-top-color: #3fa9ff; }
    .tk { text-transform: uppercase; letter-spacing: .1em; font-size: 11px; }
    @media screen {
      .hero::before { content: ""; position: absolute; inset: 0; pointer-events: none;
                      background: repeating-linear-gradient(0deg, rgba(255,255,255,.04) 0 1px, rgba(0,0,0,0) 1px 3px); }
      .hero h1 { animation: glitch 5.5s steps(1) infinite; }
      @keyframes glitch {
        0%, 91%, 100% { text-shadow: none; transform: none; }
        92% { text-shadow: 3px 0 rgba(255,40,80,.8), -3px 0 rgba(0,220,255,.7); transform: translateX(2px); }
        94% { text-shadow: -2px 0 rgba(255,40,80,.8), 2px 0 rgba(0,220,255,.7); transform: translateX(-1px); }
      }
      @media (prefers-reduced-motion: reduce) { .hero h1 { animation: none; } }
    }
    .hero .rain { position: absolute; inset: 0; width: 100%; height: 100%; opacity: .42; }
    .hero .rain text { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
    .hero h1 { text-shadow: 0 0 14px rgba(63,169,255,.5); }
    @media screen {
      .hero .col { animation: fall linear infinite; }
      @keyframes fall { from { transform: translateY(-25%); } to { transform: translateY(25%); } }
      @media (prefers-reduced-motion: reduce) { .hero .col { animation: none; } }
    }
    @media (max-width: 720px) { .tiles { grid-template-columns: repeat(2, 1fr); } .verdict { padding-right: 24px; } .verdict .ring { display: none; } }
    @media print {
      body { background: #fff; }
      @page { margin: 14mm 12mm; }
      h2 { break-after: avoid; }
      h2.pb { break-before: page; }
      h3 { break-after: avoid; }
      .card, .tile, .tiles, .verdict, .chk, table.kv tr { break-inside: avoid; }
      .card h3 + *, .card h3 + .chart { break-before: auto; }
    }
    """
    verdict_class = overall if overall in ("pass", "warn", "fail") else "none"
    result_line = (f"{counts.get('pass', 0)} of {len(measured)} measured checks passed"
                   if measured else "No stream analysis was saved for this run.")
    if problems:
        result_line += f" · {len(problems)} need{'s' if len(problems) == 1 else ''} a look"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(label)} · SRT stream test report</title>
<style>{css}</style></head>
<body><main>
<header class="hero {verdict_class}">
  {_rain()}
  <div class="kicker">{e(brand)} · SRT stream test report</div>
  <h1>SRT stream test report</h1>
  <div class="meta">{e(label)}</div>
  <div class="hazard" aria-hidden="true"></div>
</header>

<h2 class="first">Result</h2>
<section class="verdict {verdict_class}">{ring_html}
  <div class="word">{e(verdict_word)}</div>
  <div class="head">{e(headline)}</div>
  <div class="sub">{e(result_line)}</div>
</section>
<div class="tiles">{tiles}</div>

<h2>What was streamed</h2>
<section class="card"><table class="kv">{stream_rows}</table></section>

<h2 class="pb">Measured results</h2>
{chart_html}
{f'<section class="card"><h3>Stream measurements</h3><table class="kv">{measure_html}</table></section>' if measure_html else ''}

<h2>What needs a look</h2>
<section class="card"><ul class="plain">{problem_html}</ul>{pcr_note}</section>

<h2 class="pb">Checks</h2>
<section class="card">{strip_html}{check_html or '<p class="muted">No stream checks were saved for this run.</p>'}</section>
{f'<section class="card"><h3>Not evaluated</h3><ul class="plain">{not_html}</ul></section>' if not_html else ''}

<h2 class="pb">Assumptions</h2>
<section class="card"><ul class="plain">{assumption_html}</ul></section>

<footer>{e(footer)} · report made {e(generated)}</footer>
</main></body></html>"""


def _slug(text):
    """Text safe in a file name: spaces become dashes, other punctuation is dropped."""
    return re.sub(r"[^A-Za-z0-9._-]+", "", str(text).replace(" ", "-")).strip("-.")


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _label_parts(summary):
    """The parts that name a report, read from the run: preset used (if any), video size, frame rate, and the
    start time as (year, month, day, hour, minute). The file name and the label on the report both use these."""
    preset = str((summary.get("settings") or {}).get("preset") or "").strip()
    vd, _ = _details((summary.get("report") or {}).get("streams") or [], "Video")
    vd = vd or {}
    res = str(vd.get("Resolution") or "").strip()
    m = re.search(r"\d+(?:\.\d+)?", str(vd.get("Frame rate") or ""))
    fps = ("%g" % float(m.group())) if m else ""
    when = None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", str(summary.get("started") or ""))
    if m:
        when = m.groups()
    return preset, res, fps, when


def report_name(summary, ext):
    """The file name for a customer report, e.g. srtdebug-Lab-feed-1920x1080-25fps-2026-11-09-1113.pdf"""
    preset, res, fps, when = _label_parts(summary)
    parts = ["srtdebug"]
    if preset:
        parts.append(_slug(preset))
    if res:
        parts.append(_slug(res))
    if fps:
        parts.append(fps + "fps")
    if when:
        parts.append("%s-%s-%s-%s%s" % when)
    return "-".join(parts) + "." + ext


def report_label(summary):
    """The same parts as the file name, in words, for the top of the report: Lab feed · 1920x1080 · 25 fps · 9 Nov 2026, 11:13"""
    preset, res, fps, when = _label_parts(summary)
    parts = []
    if preset:
        parts.append(preset)
    if res:
        parts.append(res)
    if fps:
        parts.append(fps + " fps")
    if when:
        y, mo, d, hh, mm = when
        parts.append("%d %s %s, %s:%s" % (int(d), _MONTHS[int(mo) - 1], y, hh, mm))
    return " · ".join(parts) or str(summary.get("started") or "")


def write_html(run_dir, version=""):
    """Writes the report next to the run's files. Returns the HTML file's path."""
    summary, rows = load(run_dir)
    text = build_html(summary, rows, version or _version())
    path = os.path.join(run_dir, HTML_NAME)
    fd, tmp = tempfile.mkstemp(dir=run_dir, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def write_pdf(run_dir):
    """Makes the PDF from the report (headless Chrome, as the other reports). Returns the PDF's path.
    Raises RuntimeError with a plain message when no browser is installed."""
    html_path = write_html(run_dir)
    pdf_path = os.path.join(run_dir, PDF_NAME)
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import report
    report.write_pdf(html_path, pdf_path)
    return pdf_path


def _version():
    try:
        import srtdebug
        return srtdebug.VERSION
    except Exception:
        return ""
