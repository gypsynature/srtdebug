"""The saved-runs dashboard and its small JSON API, served by the control panel.

Routes (all on the panel's own localhost address):
    GET  /history               the dashboard page
    GET  /history/list          runs and space figures, filtered
    GET  /history/run?id=...    one run's summary and numbers, for its graph
    GET  /history/export?ids=   the chosen runs as one zip file
    POST /history/delete        {"ids": [...]}
    POST /history/move          {"ids": [...], "dest": "/absolute/folder"}

Run ids are paths relative to the history folder. Nothing outside it is read,
changed or deleted.

Standard library only. The charts are drawn in the page itself, so nothing
else needs to be installed.
"""

import csv
import json
import os
import tempfile
import urllib.parse

import runhistory

MAX_POINTS = 600


def _rel(run_dir, root):
    return os.path.relpath(run_dir, root).replace(os.sep, "/")


def _ids(query, name="ids"):
    raw = query.get(name, [""])[0] if isinstance(query, dict) else ""
    return [i for i in raw.split("|") if i]


def _full(rel_id, root):
    return os.path.join(root, *rel_id.split("/"))


def list_payload(query, root):
    q = {k: v[0] for k, v in query.items() if v}
    min_rtt = q.get("min_rtt", "").strip()
    try:
        min_rtt = float(min_rtt) if min_rtt else None
    except ValueError:
        min_rtt = None
    rows = runhistory.list_runs(
        root,
        date_from=q.get("date_from") or None,
        date_to=q.get("date_to") or None,
        kind=q.get("kind") or None,
        text=q.get("text") or None,
        host=q.get("host") or None,
        min_latency_ms=min_rtt,
        has_drops=True if q.get("drops") == "1" else None,
    )
    import customer_report
    runs = []
    for r in rows:
        t = r.get("totals") or {}
        rel = _rel(r["path"], root)
        size = runhistory.folder_size(r["path"])
        runs.append({
            "id": rel,
            "kind": r.get("kind"),
            "label": r.get("label"),
            # the same words as the report's header and its PDF file name
            "shown_label": customer_report.report_label(r),
            "file_name": customer_report.report_name(r, "pdf"),
            "host": r.get("host"),
            "started": r.get("started"),
            "ended": r.get("ended"),
            "status": r.get("status"),
            "result": r.get("result"),
            "samples": r.get("samples"),
            "lost": t.get("lost"),
            "resent": t.get("resent"),
            "dropped": t.get("dropped"),
            "worst_rtt_ms": t.get("worst_rtt_ms"),
            "size_human": runhistory.human_size(size),
        })
    return 200, {
        "runs": runs,
        "usage": runhistory.usage(root),
        "limit": runhistory.limit_status(runhistory.usage(root)["runs"]),
    }


def run_payload(query, root):
    ids = _ids(query, "id")
    if not ids:
        return 400, {"error": "no run given"}
    run_dir = _full(ids[0], root)
    if not runhistory._inside(run_dir, root) or not os.path.exists(os.path.join(run_dir, runhistory.SUMMARY)):
        return 404, {"error": "not a saved run"}
    with open(os.path.join(run_dir, runhistory.SUMMARY), encoding="utf-8") as f:
        summary = json.load(f)
    return 200, {"id": ids[0], "summary": summary, "series": _series(run_dir),
                 "files": sorted(_files(run_dir))}


def _files(run_dir):
    out = []
    for dirpath, _d, names in os.walk(run_dir):
        for n in names:
            out.append(os.path.relpath(os.path.join(dirpath, n), run_dir).replace(os.sep, "/"))
    return out


def _series(run_dir):
    """Numbers for the graph, thinned to at most MAX_POINTS rows."""
    path = os.path.join(run_dir, runhistory.NUMBERS)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    stride = max(1, len(rows) // MAX_POINTS + (1 if len(rows) % MAX_POINTS else 0))
    out = []
    for i, row in enumerate(rows):
        if i % stride:
            continue
        out.append({"t": row.get("time", ""),
                    "rtt": _num(row.get("rtt_ms")),
                    "lost": _num(row.get("lost")),
                    "retrans": _num(row.get("retrans")),
                    "dropped": _num(row.get("dropped"))})
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def export_zip(query, root):
    ids = _ids(query)
    if not ids:
        return 400, None, "no runs chosen"
    dirs = [_full(i, root) for i in ids]
    fd, path = tempfile.mkstemp(prefix="srtdebug-runs-", suffix=".zip")
    os.close(fd)
    try:
        runhistory.export_runs(dirs, path, root)
        with open(path, "rb") as f:
            return 200, f.read(), ""
    except (ValueError, OSError) as e:
        return 400, None, str(e)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def delete_payload(body, root):
    freed = 0
    done = []
    errors = []
    for rel_id in body.get("ids") or []:
        try:
            freed += runhistory.delete_run(_full(str(rel_id), root), root)
            done.append(rel_id)
        except (ValueError, OSError) as e:
            errors.append("%s: %s" % (rel_id, e))
    return 200, {"deleted": len(done), "freed": runhistory.human_size(freed), "errors": errors}


def move_payload(body, root):
    dest = str(body.get("dest") or "").strip()
    if not dest or not os.path.isabs(dest):
        return 400, {"error": "Give a full folder path, for example /home/you/old-runs."}
    moved = 0
    errors = []
    for rel_id in body.get("ids") or []:
        try:
            runhistory.move_run(_full(str(rel_id), root), dest, root)
            moved += 1
        except (ValueError, OSError) as e:
            errors.append("%s: %s" % (rel_id, e))
    return 200, {"moved": moved, "errors": errors}


def handle_get(path, query, root=runhistory.DEFAULT_ROOT):
    """Returns (code, content_type, body, filename) or None when the path is not ours."""
    if path == "/history":
        return 200, "text/html; charset=utf-8", PAGE, ""
    if path == "/history/list":
        code, obj = list_payload(query, root)
        return code, "application/json", json.dumps(obj), ""
    if path == "/history/run":
        code, obj = run_payload(query, root)
        return code, "application/json", json.dumps(obj), ""
    if path == "/history/customer":
        return customer_download(query, root)
    if path == "/history/export":
        code, data, err = export_zip(query, root)
        if data is None:
            return code, "text/plain; charset=utf-8", err, ""
        return code, "application/zip", data, "srtdebug-runs.zip"
    return None


def customer_download(query, root):
    """The customer report of one saved run, as a PDF (or HTML with format=html). Made from the run's own files."""
    import customer_report
    ids = _ids(query, "id")
    if not ids:
        return 400, "text/plain; charset=utf-8", "no run given", ""
    run_dir = _full(ids[0], root)
    if not runhistory._inside(run_dir, root) or not os.path.exists(os.path.join(run_dir, runhistory.SUMMARY)):
        return 404, "text/plain; charset=utf-8", "not a saved run", ""
    as_html = (query.get("format", ["pdf"])[0] or "pdf") == "html"
    try:
        path = customer_report.write_html(run_dir) if as_html else customer_report.write_pdf(run_dir)
    except RuntimeError as e:
        return 500, "text/plain; charset=utf-8", str(e), ""
    with open(path, "rb") as f:
        data = f.read()
    summary, _ = customer_report.load(run_dir)
    name = customer_report.report_name(summary, "html" if as_html else "pdf")
    if as_html:
        return 200, "text/html; charset=utf-8", data, name
    return 200, "application/pdf", data, name


def handle_post(path, body, root=runhistory.DEFAULT_ROOT):
    if path == "/history/delete":
        return delete_payload(body, root)
    if path == "/history/move":
        return move_payload(body, root)
    return None


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>srtdebug history</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect width='24' height='24' rx='5' fill='%230b1220'/><path d='M3 12h4l2-6 4 12 2-6h6' fill='none' stroke='%230a84ff' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>
:root { --ink:#111418; --muted:#5f6672; --line:#e4e6eb; --soft:#f4f5f8; --accent:#0a84ff;
        --accent-soft:#e8f1fd; --warn:#b54708; --bad:#d92d20; --bg:#f2f3f6; --card:#ffffff; --shadow:0 2px 10px rgba(16,24,40,.06); }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --ink:#f2f4f7; --muted:#9aa2b1; --line:#262b35; --soft:#1c2029;
    --accent:#0a84ff; --accent-soft:#0f2a4a; --warn:#ffb340; --bad:#ff453a; --bg:#0b0d11; --card:#15181e; --shadow:0 2px 12px rgba(0,0,0,.35); }
}
:root[data-theme="dark"] { --ink:#f2f4f7; --muted:#9aa2b1; --line:#262b35; --soft:#1c2029;
  --accent:#0a84ff; --accent-soft:#0f2a4a; --warn:#ffb340; --bad:#ff453a; --bg:#0b0d11; --card:#15181e; --shadow:0 2px 12px rgba(0,0,0,.35); }
body { background:var(--bg); color:var(--ink); margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 56px; }
h1 { font-size:30px; font-weight:700; letter-spacing:-0.02em; margin:0 0 4px; } h2 { font-size:17px; margin:0 0 8px; }
.muted { color:var(--muted); }
.card { background:var(--card); border:0; border-radius:18px; padding:20px 22px; margin:16px 0; box-shadow:var(--shadow); }
.pill { display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:600; background:var(--soft); color:var(--muted); }
.pill.bad { background:color-mix(in srgb, var(--bad) 14%, transparent); color:var(--bad); }
.banner { border-left:3px solid var(--accent); background:var(--soft); padding:10px 14px; border-radius:6px; margin:12px 0; }
.banner.warning { border-color:var(--warn); } .banner.limit { border-color:var(--bad); }
form.filters { display:flex; flex-wrap:wrap; gap:8px; align-items:end; }
label { font-size:13px; color:var(--muted); display:flex; flex-direction:column; gap:3px; }
input, select { font:inherit; color:var(--ink); background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:6px 8px; }
button { font:inherit; color:var(--ink); background:var(--card); border:1px solid var(--line); border-radius:12px; padding:8px 16px; cursor:pointer; box-shadow:0 1px 2px rgba(16,24,40,.05); }
button:hover { border-color:var(--accent); }
button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
button.danger { color:var(--bad); }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:13px; }
tr.sel { background:var(--accent-soft); } tr.run { cursor:pointer; } tr.run:hover { background:var(--soft); }
.bad { color:var(--bad); } .warn { color:var(--warn); }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
svg { width:100%; height:240px; display:block; background:var(--soft); border-radius:8px; }
.legend span { margin-right:14px; font-size:13px; }
.kv { display:grid; grid-template-columns:max-content 1fr; gap:4px 14px; font-size:14px; }
.kv div:nth-child(odd) { color:var(--muted); }
.rep-head { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-top:16px; }
.rep-head h2 { margin:0; }
.badge { font-size:12px; font-weight:700; letter-spacing:.04em; padding:4px 10px; border-radius:999px; color:#fff; }
.badge.pass { background:var(--ok, #17803d); }
.badge.warn { background:var(--warn, #b54708); }
.badge.fail { background:var(--bad, #d92d20); }
.checks { display:flex; flex-direction:column; gap:4px; margin-top:10px; font-size:13px; }
.check-row { display:grid; grid-template-columns:86px 1.4fr 1fr; gap:10px; padding:6px 8px; border-radius:8px; }
.check-row.pass { background:transparent; }
.check-row.warn { background:rgba(181,71,8,.10); }
.check-row.fail { background:rgba(217,45,32,.12); }
.check-row.info, .check-row.not-measured { color:var(--muted); }
.check-row .st { font-weight:700; text-transform:uppercase; font-size:11px; letter-spacing:.04em; }
.check-row.pass .st { color:var(--ok, #17803d); }
.check-row.warn .st { color:var(--warn, #b54708); }
.check-row.fail .st { color:var(--bad, #d92d20); }
.cmp-pick { margin-top:8px; padding:8px 10px; border-radius:10px; border:1px solid var(--line, #d9dee3); background:var(--card, #fff); color:inherit; font:inherit; }
table.cmp { width:100%; border-collapse:collapse; margin-top:10px; font-size:14px; }
table.cmp th, table.cmp td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line, #e4e6eb); vertical-align:top; }
table.cmp td.lbl { color:var(--muted); width:28%; }
.rep-hero { display:grid; grid-template-columns: 220px 1fr; gap:16px; margin-top:16px; padding:18px; border-radius:16px; background:var(--card-2, #f4f5f8); }
.rep-hero.pass { border-left:6px solid var(--ok, #17803d); }
.rep-hero.warn { border-left:6px solid var(--warn, #b54708); }
.rep-hero.fail { border-left:6px solid var(--bad, #d92d20); }
.verdict { font-size:34px; font-weight:800; letter-spacing:-.01em; }
.rep-hero.pass .verdict { color:var(--ok, #17803d); }
.rep-hero.warn .verdict { color:var(--warn, #b54708); }
.rep-hero.fail .verdict { color:var(--bad, #d92d20); }
.verdict-sub { margin-top:4px; color:var(--muted); font-size:14px; }
.tiles { display:grid; grid-template-columns: repeat(4, 1fr); gap:10px; }
.tile { padding:10px 12px; border-radius:12px; background:var(--card, #fff); }
.tile-k { font-size:12px; color:var(--muted); }
.tile-v { font-size:20px; font-weight:700; margin-top:2px; }
.tile-s { font-size:12px; color:var(--muted); margin-top:2px; }
.rep-h { margin:22px 0 8px; font-size:16px; }
.attn { display:flex; gap:12px; align-items:flex-start; padding:10px 12px; border-radius:12px; margin-bottom:8px; }
.attn.warn { background:rgba(181,71,8,.10); }
.attn.fail { background:rgba(217,45,32,.12); }
.pill { font-size:11px; font-weight:700; text-transform:uppercase; padding:3px 8px; border-radius:999px; color:#fff; white-space:nowrap; }
.pill.warn { background:var(--warn, #b54708); }
.pill.fail { background:var(--bad, #d92d20); }
.attn-name { font-weight:650; }
.attn-value { font-size:14px; margin-top:2px; }
.attn-note { font-size:13px; color:var(--muted); margin-top:4px; }
.stream-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap:12px; }
.scard { padding:14px; border-radius:16px; background:var(--card-2, #f4f5f8); }
.scard.video { border-top:4px solid var(--accent, #1d6fb8); }
.scard.audio { border-top:4px solid var(--ok, #17803d); }
.scard-kind { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.scard-head { font-size:17px; font-weight:700; margin:4px 0 10px; }
.scard-foot { font-size:12px; color:var(--muted); margin-top:8px; }
.facts { display:grid; grid-template-columns: repeat(2, 1fr); gap:8px; }
.fact-k { font-size:11px; color:var(--muted); }
.fact-v { font-size:14px; }
.bar { height:6px; border-radius:999px; background:var(--line, #e4e6eb); margin-top:12px; overflow:hidden; }
.bar-fill { height:100%; background:var(--accent, #1d6fb8); border-radius:999px; }
details.grp { margin-top:8px; border:1px solid var(--line, #e4e6eb); border-radius:12px; padding:8px 12px; }
details.grp summary { cursor:pointer; font-weight:600; font-size:14px; }
.chk { display:grid; grid-template-columns:70px 1.5fr 1fr; gap:10px; padding:5px 4px; font-size:13px; }
.chk.pass .st { color:var(--ok, #17803d); }
.chk.warn .st { color:var(--warn, #b54708); }
.chk.fail .st { color:var(--bad, #d92d20); }
.chk.info, .chk.not-measured { color:var(--muted); }
.chk .st { font-weight:700; text-transform:uppercase; font-size:11px; }
@media (max-width: 700px) { .rep-hero { grid-template-columns: 1fr; } .tiles { grid-template-columns: repeat(2, 1fr); } }
.stream-card { margin-top:12px; padding:12px 14px; border:1px solid var(--line, #e4e6eb); border-radius:12px; }
.stream-title { font-weight:600; margin-bottom:8px; font-size:14px; }
pre { white-space:pre-wrap; word-break:break-word; background:var(--soft); padding:10px; border-radius:6px; font-size:13px; }
tr.picked td { background: rgba(10,132,255,.10); }
tr.picked td:first-child { box-shadow: inset 3px 0 0 #0a84ff; }
.top { position: sticky; top: 0; z-index: 5; background: var(--card); border-bottom: 1px solid var(--line); }
.top-in { max-width: 1120px; margin: 0 auto; padding: 0 20px; height: 56px; display: flex; align-items: center; gap: 12px; }
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--ink); text-decoration: none; }
.brand svg { width: 22px; height: 22px; }
.crumb { color: var(--muted); font-size: 13px; }
.spacer { flex: 1; }
a.btn { display: inline-flex; align-items: center; padding: 8px 14px; border-radius: 12px; border: 1px solid var(--line);
        background: var(--card); color: var(--ink); font-weight: 600; font-size: 14px; text-decoration: none; }
a.btn:hover { border-color: var(--accent); }
button.btn { display: inline-flex; align-items: center; padding: 8px 14px; border-radius: 12px; border: 1px solid var(--line);
             background: var(--card); color: var(--ink); font: inherit; font-weight: 600; font-size: 14px; cursor: pointer; }
button.btn:hover { border-color: var(--accent); }
.tpill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700; padding: 4px 11px; border-radius: 999px; border: 1px solid var(--line); }
.tled { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
.tpill.on .tled { background: #17803d; box-shadow: 0 0 0 4px rgba(23,128,61,.18); }
.hist-main { max-width: 1120px; margin: 0 auto; padding: 18px 20px 40px; }
</style></head>
<body>
<header class="top"><div class="top-in">
  <a class="brand" href="/"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-6 4 12 2-6h6" fill="none" stroke="var(--accent)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>srtdebug</span></a>
  <div class="crumb">History</div>
  <div class="spacer"></div>
  <a class="btn" href="/">&lsaquo; Start</a>
  <a class="btn" href="/#decode">Decode stream</a>
  <a class="btn" href="/#encode">Encode stream</a>
  <span class="tpill" id="tpill"><span class="tled"></span><span id="tpilltext">Checking</span></span>
  <a class="btn" href="/#help" aria-label="How this tool works">?</a>
  <button class="btn" id="tquit" type="button" title="Stop srtdebug. It also stops by itself a few minutes after this window is closed.">Quit</button>
</div></header>
<main class="hist-main">
<h1>History</h1>
<p class="muted">Every feed and monitor run is saved here automatically. Only this computer can see it.</p>

<div id="banner" class="banner" hidden></div>

<div class="card">
  <form class="filters" id="filters">
    <label>From <input type="date" name="date_from"></label>
    <label>To <input type="date" name="date_to"></label>
    <label>Type
      <select name="kind"><option value="">Any</option><option value="feed">Feed</option><option value="monitor">Monitor</option></select>
    </label>
    <label>Host contains <input type="text" name="host" size="16"></label>
    <label>Search <input type="text" name="text" size="16" placeholder="label or result"></label>
    <label>Worst round trip at least (ms) <input type="number" name="min_rtt" min="0" step="10" style="width:9em"></label>
    <label><span><input type="checkbox" name="drops" value="1"> Only runs with dropped packets</span></label>
    <button type="submit" class="primary">Search</button>
    <button type="button" id="reset">Clear</button>
  </form>
</div>

<div class="card">
  <div class="muted" id="usage"></div>
  <table>
    <thead><tr><th><input type="checkbox" id="pickall" title="Select or clear every run in this list" aria-label="Select every run in this list"></th><th>Started</th><th>Type</th><th>Label</th><th>Result</th><th>Worst RTT</th><th>Lost</th><th>Dropped</th><th>Size</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <p class="muted" id="empty" hidden>No runs match these filters.</p>
  <div class="actions">
    <button id="export">Export selected</button>
    <button id="move">Move selected</button>
    <button id="delete" class="danger">Delete selected</button>
  </div>
  <p class="muted" id="status"></p>
</div>

<div class="card" id="detail" hidden>
  <h2 id="dtitle"></h2>
  <div class="kv" id="dkv"></div>
  <div id="dreport"></div>
  <h2 style="margin-top:14px">Compare with another run</h2>
  <select id="cmp" class="cmp-pick"></select>
  <table id="cmptab" class="cmp" hidden></table>
  <h2 style="margin-top:14px">Round trip and drops over time</h2>
  <svg id="chart" viewBox="0 0 800 240" preserveAspectRatio="xMidYMid meet"></svg>
  <p class="legend"><span style="color:var(--accent)">&#9632; round trip (ms)</span><span style="color:var(--bad)">&#9632; dropped packets (running total)</span></p>
  <h2>Files in this run</h2>
  <div class="muted" id="dfiles"></div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let runs = [];
let shown = null;

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function fmt(v) { return v === null || v === undefined ? "" : v; }
function say(text) { $("status").textContent = text || ""; }
function when(s) { return s ? s.replace("T", " ") : ""; }

// the header box follows the rows: ticked when every listed run is ticked
const picked = new Set();
function setPicked(id, on, tr) {
  if (on) { picked.add(id); } else { picked.delete(id); }
  if (tr) { tr.classList.toggle("picked", on); }
}

function syncPickAll() {
  const picks = [...document.querySelectorAll("input.pick")];
  $("pickall").checked = picks.length > 0 && picks.every(b => b.checked);
}

$("pickall").addEventListener("change", () => {
  for (const b of document.querySelectorAll("input.pick")) {
    b.checked = $("pickall").checked;
    setPicked(b.value, b.checked, b.closest("tr"));
  }
});

function selectedIds() {
  return [...document.querySelectorAll("input.pick:checked")].map(b => b.value);
}

async function postJson(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

function banner(usage, limit) {
  const b = $("banner");
  if (!limit || limit.state === "ok") { b.hidden = true; return; }
  b.hidden = false;
  b.className = "banner " + limit.state;
  b.textContent = limit.message + " Use Move, Export or Delete below to make room. New runs keep saving either way.";
}

async function load() {
  const q = new URLSearchParams(new FormData($("filters")));
  const r = await fetch("/history/list?" + q.toString());
  const data = await r.json();
  runs = data.runs;
  const u = data.usage;
  $("usage").textContent = u.runs + " of " + u.limit + " runs saved, using " + u.used_human +
    ". Drive: " + u.drive_human + "." + (u.wsl_note ? " " + u.wsl_note : "");
  banner(u, data.limit);
  // rows are rebuilt on every refresh: the ticks are kept in picked so they survive it
  const tbody = $("rows");
  tbody.textContent = "";
  $("empty").hidden = runs.length > 0;
  for (const run of runs) {
    const tr = el("tr", null, "run" + (shown === run.id ? " sel" : ""));
    const pick = el("input", null, "pick");
    pick.type = "checkbox"; pick.value = run.id; pick.checked = picked.has(run.id);
    if (pick.checked) { tr.classList.add("picked"); }
    pick.addEventListener("click", (e) => e.stopPropagation());
    pick.addEventListener("change", () => { setPicked(run.id, pick.checked, tr); syncPickAll(); });
    const c0 = el("td"); c0.appendChild(pick);
    tr.appendChild(c0);
    tr.appendChild(el("td", when(run.started)));
    tr.appendChild(el("td", run.kind));
    const lblCell = el("td", run.shown_label || run.label);
    lblCell.title = run.file_name || "";
    tr.appendChild(lblCell);
    tr.appendChild(el("td", run.status + (run.result ? " - " + run.result : ""), run.status === "ended early" ? "warn" : ""));
    tr.appendChild(el("td", run.worst_rtt_ms === null ? "" : run.worst_rtt_ms + " ms"));
    tr.appendChild(el("td", fmt(run.lost)));
    tr.appendChild(el("td", fmt(run.dropped), run.dropped ? "bad" : ""));
    tr.appendChild(el("td", run.size_human));
    // a click on the row ticks it (ready to export or delete) and opens or folds it; the box itself unticks
    tr.addEventListener("click", () => {
      pick.checked = true;
      setPicked(run.id, pick.checked, tr);
      syncPickAll();
      toggleRun(run.id);
    });
    tbody.appendChild(tr);
  }
  syncPickAll();
  say("");
}

// clicking a run opens it; clicking the open run again folds it away
function toggleRun(id) {
  if (shown === id && !$("detail").hidden) {
    shown = null;
    $("detail").hidden = true;
    load();
    return;
  }
  show(id);
}

async function show(id) {
  shown = id;
  const r = await fetch("/history/run?id=" + encodeURIComponent(id));
  const data = await r.json();
  if (!r.ok) { say(data.error || "Could not open that run."); return; }
  const s = data.summary;
  const t = s.totals || {};
  $("dtitle").textContent = s.label || id;
  const kv = $("dkv");
  kv.textContent = "";
  const rows = [["Started", when(s.started)], ["Ended", when(s.ended) || "still running"],
    ["Status", s.status + (s.result ? " - " + s.result : "")], ["Type", s.kind],
    ["Settings", Object.entries(s.settings || {}).map(([k, v]) => k + " " + v).join(", ")],
    ["Samples", s.samples], ["Worst round trip", t.worst_rtt_ms === null || t.worst_rtt_ms === undefined ? "" : t.worst_rtt_ms + " ms"],
    ["Lost / resent / dropped", [t.lost, t.resent, t.dropped].join(" / ")]];
  for (const [k, v] of rows) { kv.appendChild(el("div", k)); kv.appendChild(el("div", String(fmt(v)))); }
  drawReport(s.report);
  fillCompare();
  drawChart(data.series);
  $("dfiles").textContent = data.files.join("  ·  ");
  $("detail").hidden = false;
  load();
}

function checkValue(s, name) {
  const c = s.report && (s.report.checks || []).find(x => x.name === name);
  return c ? c.value : "";
}

function streamSummary(s, kind) {
  const st = s.report && (s.report.streams || []).find(x => x.role.indexOf(kind) === 0 && x.details && x.details.length);
  if (!st) { return ""; }
  const want = kind === "Video" ? ["Codec", "Resolution", "Frame rate"] : ["Codec", "Channels", "Sample rate"];
  return want.map(k => { const d = st.details.find(p => p[0] === k); return d ? d[1] : ""; }).filter(Boolean).join(" · ");
}

function fillCompare() {
  const sel = $("cmp");
  sel.textContent = "";
  const first = el("option", "Choose a run to compare with");
  first.value = "";
  sel.appendChild(first);
  for (const r of runs) {
    if (r.id === shown) { continue; }
    const o = el("option", (r.label || r.id) + " · " + when(r.started));
    o.value = r.id;
    sel.appendChild(o);
  }
  $("cmptab").hidden = true;
}

async function compareWith(id) {
  if (!id) { $("cmptab").hidden = true; return; }
  const [a, b] = await Promise.all([
    fetch("/history/run?id=" + encodeURIComponent(shown)).then(r => r.json()),
    fetch("/history/run?id=" + encodeURIComponent(id)).then(r => r.json())]);
  const tab = $("cmptab");
  tab.textContent = "";
  const head = el("tr", null);
  head.appendChild(el("th", ""));
  head.appendChild(el("th", (a.summary.label || shown)));
  head.appendChild(el("th", (b.summary.label || id)));
  tab.appendChild(el("thead", null)).appendChild(head);
  const body = el("tbody", null);
  const t = (s) => s.totals || {};
  const rows = [
    ["Started", s => when(s.started)],
    ["Samples", s => fmt(s.samples)],
    ["Worst round trip", s => t(s).worst_rtt_ms == null ? "" : t(s).worst_rtt_ms + " ms"],
    ["Lost / resent / dropped", s => [t(s).lost, t(s).resent, t(s).dropped].map(fmt).join(" / ")],
    ["Stream health", s => s.report ? verdictWord(s.report.overall) + " (" + (s.report.counts.pass || 0) + " pass, " +
      (s.report.counts.warn || 0) + " warn, " + (s.report.counts.fail || 0) + " attention)" : "no report"],
    ["Average bitrate", s => checkValue(s, "Average bitrate")],
    ["Video", s => streamSummary(s, "Video")],
    ["Audio", s => streamSummary(s, "Audio")]];
  for (const [label, get] of rows) {
    const tr = el("tr", null);
    tr.appendChild(el("td", label, "lbl"));
    tr.appendChild(el("td", String(get(a.summary) ?? "")));
    tr.appendChild(el("td", String(get(b.summary) ?? "")));
    body.appendChild(tr);
  }
  tab.appendChild(body);
  tab.hidden = false;
}

function checkRank(ch) {
  return ch.value === "not carried by this stream" ? 2 : ch.status === "not measured" ? 1 : 0;
}

function valueOf(checks, name) {
  const c = (checks || []).find(x => x.name === name);
  return c ? c.value : "";
}

function tile(parent, label, value, sub) {
  const t = el("div", "", "tile");
  t.appendChild(el("div", label, "tile-k"));
  t.appendChild(el("div", value, "tile-v"));
  if (sub) { t.appendChild(el("div", sub, "tile-s")); }
  parent.appendChild(t);
}

function factRow(pairs) {
  const wrap = el("div", "", "facts");
  for (const [k, v] of pairs) {
    if (!v) { continue; }
    const f = el("div", "", "fact");
    f.appendChild(el("div", k, "fact-k"));
    f.appendChild(el("div", String(v), "fact-v"));
    wrap.appendChild(f);
  }
  return wrap;
}

function streamCard(st) {
  const d = {};
  for (const [k, v] of st.details) { d[k] = v; }
  const isVideo = st.role.indexOf("Video") === 0;
  const card = el("div", "", "scard " + (isVideo ? "video" : "audio"));
  card.appendChild(el("div", isVideo ? "Video" : "Audio", "scard-kind"));
  const head = isVideo ? [d["Codec"], d["Resolution"]] : [d["Codec"], d["Channels"]];
  card.appendChild(el("div", head.filter(Boolean).join(" · ") || st.role, "scard-head"));
  if (isVideo) {
    const colour = [d["Primaries"], d["Transfer"]].filter(x => x && x !== "not signalled").join(", ");
    card.appendChild(factRow([["Frame rate", d["Frame rate"]], ["Scan", d["Scan"]], ["Chroma", d["Chroma subsampling"]],
      ["Bit depth", d["Bit depth"]], ["Colour", colour], ["Range", d["Range"] !== "not signalled" ? d["Range"] : ""]]));
  } else {
    card.appendChild(factRow([["Sample rate", d["Sample rate"]], ["Channels", d["Channels"]],
      ["Declared rate", d["Declared bit rate"]]]));
  }
  const bar = el("div", "", "bar");
  const fill = el("div", "", "bar-fill");
  fill.style.width = Math.min(100, Number(st.share) || 0) + "%";
  bar.appendChild(fill);
  card.appendChild(bar);
  card.appendChild(el("div", st.kbps + " kb/s · " + st.share + "% of the stream · PID " + st.pid, "scard-foot"));
  return card;
}

// the same wording as the customer report: a failing check is ATTENTION, never FAIL
function verdictWord(overall) {
  return {pass: "PASS", warn: "PASS WITH WARNINGS", fail: "NEEDS ATTENTION"}[overall] || "NOT ANALYSED";
}
function checkWord(status) {
  return {pass: "PASS", warn: "WARN", fail: "ATTENTION", note: "NOTE"}[status] || "NOT CHECKED";
}

function drawReport(report) {
  const box = $("dreport");
  box.textContent = "";
  if (!report) {
    box.appendChild(el("p", "No stream report was saved for this run. It is written by runs that analyse the stream.", "muted"));
    return;
  }
  const checks = report.checks || [];
  const c = report.counts || {};
  const issues = checks.filter(x => x.status === "warn" || x.status === "fail");
  const measured = checks.filter(x => x.status === "pass" || x.status === "warn" || x.status === "fail").length;

  // verdict banner with headline numbers
  const hero = el("div", "", "rep-hero " + report.overall);
  const verdict = el("div", "", "verdict-box");
  verdict.appendChild(el("div", verdictWord(report.overall), "verdict"));
  verdict.appendChild(el("div", issues.length ? issues.length + (issues.length === 1 ? " thing" : " things") + " to look at"
    : "No problems found", "verdict-sub"));
  hero.appendChild(verdict);
  const tiles = el("div", "", "tiles");
  tile(tiles, "Checks passed", (c["pass"] || 0) + " of " + measured, "measured checks");
  tile(tiles, "Average bitrate", valueOf(checks, "Average bitrate") || "not measured", "all streams");
  tile(tiles, "Null packets", valueOf(checks, "Null packets") || "not measured", "padding share");
  tile(tiles, "Analysed for", (report.duration_s || 0) + " s", "stream time");
  hero.appendChild(tiles);
  box.appendChild(hero);

  // what to look at first
  box.appendChild(el("h3", "Needs attention", "rep-h"));
  if (!issues.length) {
    box.appendChild(el("p", "Every measured check passed.", "muted"));
  } else {
    for (const ch of issues) {
      const row = el("div", "", "attn " + ch.status);
      row.appendChild(el("span", ch.status === "fail" ? "Attention" : "Warn", "pill " + ch.status));
      const txt = el("div", "");
      txt.appendChild(el("div", ch.name, "attn-name"));
      txt.appendChild(el("div", ch.value, "attn-value"));
      if (ch.note) { txt.appendChild(el("div", ch.note, "attn-note")); }
      row.appendChild(txt);
      box.appendChild(row);
    }
  }

  // streams: cards for video and audio, a table for everything else
  box.appendChild(el("h3", "Streams", "rep-h"));
  const cards = el("div", "", "stream-grid");
  const others = [];
  for (const st of report.streams || []) {
    if (st.details && st.details.length && /^(Video|Audio)/.test(st.role)) { cards.appendChild(streamCard(st)); }
    else { others.push(st); }
  }
  box.appendChild(cards);
  if (others.length) {
    const tab = el("table", "", "cmp");
    const head = el("tr", null);
    for (const h of ["PID", "What it carries", "Rate", "Share"]) { head.appendChild(el("th", h)); }
    tab.appendChild(el("thead", null)).appendChild(head);
    const body = el("tbody", null);
    for (const st of others) {
      const tr = el("tr", null);
      tr.appendChild(el("td", String(st.pid)));
      tr.appendChild(el("td", st.role + (st.desc ? " · " + st.desc : "")));
      tr.appendChild(el("td", st.kbps + " kb/s"));
      tr.appendChild(el("td", st.share + "%"));
      body.appendChild(tr);
    }
    tab.appendChild(body);
    box.appendChild(tab);
  }

  // every check, grouped and folded; groups with problems open
  box.appendChild(el("h3", "All checks", "rep-h"));
  const groups = new Map();
  for (const ch of checks) {
    if (!groups.has(ch.group)) { groups.set(ch.group, []); }
    groups.get(ch.group).push(ch);
  }
  for (const [g, list] of groups) {
    const det = document.createElement("details");
    det.className = "grp";
    det.open = list.some(x => x.status === "warn" || x.status === "fail");
    const count = (st) => list.filter(x => x.status === st).length;
    const sum = document.createElement("summary");
    sum.textContent = g + " · " + count("pass") + " pass · " + count("warn") + " warn · " + count("fail") + " attention" +
      (count("not measured") ? " · " + count("not measured") + " not measured" : "");
    det.appendChild(sum);
    for (const ch of list.slice().sort((a, b) => checkRank(a) - checkRank(b))) {
      const row = el("div", "", "chk " + ch.status.replace(" ", "-"));
      row.appendChild(el("span", checkWord(ch.status), "st"));
      row.appendChild(el("span", ch.name, "nm"));
      row.appendChild(el("span", ch.value, "vl"));
      if (ch.note) { row.title = ch.note; }
      det.appendChild(row);
    }
    box.appendChild(det);
  }
}

$("cmp").addEventListener("change", () => compareWith($("cmp").value));

function drawChart(series) {
  const svg = $("chart");
  svg.textContent = "";
  const W = 800, H = 240, P = 30;
  if (!series.length) {
    const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
    t.setAttribute("x", 16); t.setAttribute("y", 30); t.setAttribute("fill", "currentColor");
    t.textContent = "No numbers were saved for this run.";
    svg.appendChild(t);
    return;
  }
  const rtts = series.map(p => p.rtt).filter(v => v !== null);
  const drops = series.map(p => p.dropped).filter(v => v !== null);
  const maxR = Math.max(1, ...rtts), maxD = Math.max(1, ...drops);
  const x = (i) => P + (series.length === 1 ? 0 : i * (W - 2 * P) / (series.length - 1));
  const yR = (v) => H - P - v / maxR * (H - 2 * P);
  const yD = (v) => H - P - v / maxD * (H - 2 * P);
  const line = (vals, y, color) => {
    const pts = [];
    vals.forEach((v, i) => { if (v !== null) pts.push(x(i).toFixed(1) + "," + y(v).toFixed(1)); });
    if (!pts.length) return;
    const p = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    p.setAttribute("points", pts.join(" "));
    p.setAttribute("fill", "none"); p.setAttribute("stroke", color); p.setAttribute("stroke-width", "2");
    svg.appendChild(p);
  };
  const axis = (text, xPos, yPos, anchor) => {
    const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
    t.setAttribute("x", xPos); t.setAttribute("y", yPos); t.setAttribute("font-size", "11");
    t.setAttribute("fill", "currentColor"); t.setAttribute("text-anchor", anchor);
    t.textContent = text; svg.appendChild(t);
  };
  line(series.map(p => p.rtt), yR, getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#1d6fb8");
  line(series.map(p => p.dropped), yD, getComputedStyle(document.documentElement).getPropertyValue("--bad").trim() || "#b42318");
  axis(maxR + " ms", 4, P - 6, "start");
  axis("0", 4, H - P + 4, "start");
  axis(maxD + " dropped", W - 4, P + 14, "end");
  axis(series[0].t.slice(11, 19), P, H - 8, "start");
  axis(series[series.length - 1].t.slice(11, 19), W - P, H - 8, "end");
}

$("filters").addEventListener("submit", (e) => { e.preventDefault(); load(); });
$("reset").addEventListener("click", () => { $("filters").reset(); load(); });

$("export").addEventListener("click", () => {
  const ids = selectedIds();
  if (!ids.length) { say("Tick one or more runs first."); return; }
  window.location = "/history/export?ids=" + encodeURIComponent(ids.join("|"));
});

$("move").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!ids.length) { say("Tick one or more runs first."); return; }
  const dest = prompt("Move " + ids.length + " run(s) to this folder (full path, for example /home/you/old-runs):");
  if (!dest) return;
  const data = await postJson("/history/move", { ids: ids, dest: dest });
  say(data.error || ("Moved " + data.moved + " run(s)." + (data.errors.length ? " Problems: " + data.errors.join("; ") : "")));
  load();
});

$("delete").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!ids.length) { say("Tick one or more runs first."); return; }
  if (!confirm("Delete " + ids.length + " run(s)? Their numbers, graphs and logs are removed. This cannot be undone.")) return;
  const data = await postJson("/history/delete", { ids: ids });
  shown = null; $("detail").hidden = true;
  say("Deleted " + data.deleted + " run(s), freeing " + data.freed + "." + (data.errors.length ? " Problems: " + data.errors.join("; ") : ""));
  load();
});

load();

(function () {
  function setPill(s) {
    var on = !!s.running;
    document.getElementById('tpill').classList.toggle('on', on);
    document.getElementById('tpilltext').textContent = on ? 'Running' : 'Stopped';
    return on;
  }
  var lastOn = false;
  function poll() {
    fetch('/status', {cache: 'no-store'}).then(function (r) { return r.json(); })
      .then(function (s) { lastOn = setPill(s); }).catch(function () {});
  }
  poll(); setInterval(poll, 4000);
  document.getElementById('tquit').addEventListener('click', function () {
    if (lastOn && !window.confirm('Quit srtdebug? The running monitor or encode will stop.')) { return; }
    fetch('/quit', {method: 'POST'}).catch(function () {}).then(function () {
      document.body.innerHTML = '<main style="max-width:520px;margin:12vh auto;padding:0 16px;font:16px system-ui,sans-serif;">' +
        '<h1 style="font-size:22px">srtdebug has stopped</h1>' +
        '<p>You can close this tab. To use srtdebug again, use Launch srtdebug.</p></main>';
    });
  });
})();
</script>
</main></body></html>
"""
