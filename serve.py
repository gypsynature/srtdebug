"""srtdebug serve - a browser control panel on localhost.

Start the program once from a terminal, then configure, start and stop monitoring from a
browser and watch the live dashboard on the same port. Each start runs a monitor in the
background; the control panel proxies its dashboard, so only one port is opened.
"""

import html
import http.server
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from urllib.parse import parse_qs, quote, unquote
import urllib.request

import netlink
import spikes
import sysmeter
import thresholds
import tsanalyze

import srtdebug as core

HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]{1,255}$")          # hostnames, IPv4 and IPv6 literals
STREAMID_RE = re.compile(r"^[A-Za-z0-9._:/@\-]{0,128}$")   # conservative: no spaces or shell characters


def parse_config(d):
    """Validate the browser form. Raises ValueError with a message the page can show."""
    mode = d.get("mode")
    if mode not in ("listener", "caller"):
        raise ValueError("mode must be listener or caller")
    try:
        port = int(d.get("port"))
    except (TypeError, ValueError):
        raise ValueError("port must be a number")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    host = str(d.get("host") or "").strip()
    if mode == "listener":
        host = ""   # a listener takes no host: a stray value would stop it from starting
    if host and not HOST_RE.match(host):
        raise ValueError("host has characters that are not allowed")
    if mode == "caller" and not host:
        raise ValueError("caller mode needs the remote host")
    latency = d.get("latency")
    if latency in (None, ""):
        latency = core.DEFAULT_LATENCY_MS
    else:
        try:
            latency = int(latency)
        except (TypeError, ValueError):
            raise ValueError("latency must be a number of milliseconds")
        if not 0 <= latency <= 60000:
            raise ValueError("latency must be between 0 and 60000 ms")
    passphrase = str(d.get("passphrase") or "")
    if passphrase and not core.PASSPHRASE_LEN[0] <= len(passphrase) <= core.PASSPHRASE_LEN[1]:
        raise ValueError(f"the passphrase must be {core.PASSPHRASE_LEN[0]} to {core.PASSPHRASE_LEN[1]} characters")
    pbkeylen = d.get("pbkeylen")
    if pbkeylen in (None, ""):
        pbkeylen = None
    else:
        try:
            pbkeylen = int(pbkeylen)
        except (TypeError, ValueError):
            raise ValueError("key length must be 16, 24 or 32")
        if pbkeylen not in (16, 24, 32):
            raise ValueError("key length must be 16, 24 or 32")
    dur = d.get("duration")
    if dur in (None, ""):
        duration_min = None
    else:
        try:
            duration_min = int(dur)
        except (TypeError, ValueError):
            raise ValueError("scheduled length must be a whole number of minutes")
        if not 1 <= duration_min <= 1440:
            raise ValueError("scheduled length must be between 1 and 1440 minutes")
    streamid = str(d.get("streamid") or "").strip()
    if streamid and not STREAMID_RE.match(streamid):
        raise ValueError("streamid may only contain letters, digits and . _ : / @ -")
    return {"mode": mode, "port": port, "host": host or None, "latency": latency,
            "streamid": streamid or None, "payload": bool(d.get("payload")), "view": bool(d.get("view")),
            "passphrase": passphrase or None, "pbkeylen": pbkeylen, "encrypted": bool(passphrase),
            "duration_min": duration_min}


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Session:
    """One monitor process at a time. Its dashboard is served on a private localhost port."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.cfg = None
        self.internal = None
        self.started = None
        self.dir = None
        self.log_fh = None
        self.pdf = {"state": None, "error": ""}   # background PDF job for the current run
        self.pdf_lock = threading.Lock()
        self.timer = None                 # the scheduled stop, when a length was set
        self.ends_at = None
        self.auto_stopped = False

    def make_pdf(self, html_path, pdf_path):
        """Runs in a thread: Chrome can take a while, so the browser is not left waiting on it."""
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import report
        try:
            report.write_pdf(html_path, pdf_path)
            state = {"state": "ready", "error": ""}
        except Exception as e:
            state = {"state": "error", "error": str(e)}
        with self.pdf_lock:
            self.pdf = state

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cfg):
        with self.lock:
            self._stop_locked()
            self.dir = tempfile.mkdtemp(prefix="srtdebug-serve-")
            with self.pdf_lock:
                self.pdf = {"state": None, "error": ""}
            self.internal = free_port()
            cmd = [sys.executable, os.path.abspath(core.__file__), "monitor",
                   "--mode", cfg["mode"], "--port", str(cfg["port"]),
                   "--web", str(self.internal), "--web-bind", "127.0.0.1",
                   "--export", os.path.join(self.dir, "run.csv"),
                   "--report", os.path.join(self.dir, "run.html")]
            if cfg["host"]:
                cmd += ["--host", cfg["host"]]
            cmd += ["--latency", str(cfg["latency"])]
            if cfg["streamid"]:
                cmd += ["--streamid", cfg["streamid"]]
            if cfg["payload"]:
                cmd.append("--payload")
            if cfg.get("view"):
                cmd.append("--view")
            env = dict(os.environ)
            if cfg["passphrase"]:
                # the passphrase travels in the environment, never on the command line or in status
                env[core.PASSPHRASE_ENV] = cfg["passphrase"]
                cmd += ["--passphrase-env", core.PASSPHRASE_ENV]
            if cfg["pbkeylen"]:
                cmd += ["--pbkeylen", str(cfg["pbkeylen"])]
            self.log_fh = open(os.path.join(self.dir, "monitor.log"), "w")
            self.proc = subprocess.Popen(cmd, stdout=self.log_fh, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env)
            self.cfg = {k: v for k, v in cfg.items() if k != "passphrase"}
            self.started = time.time()
            self.auto_stopped = False
            self.ends_at = None
            if cfg.get("duration_min"):
                secs = cfg["duration_min"] * 60
                self.ends_at = self.started + secs
                self.timer = threading.Timer(secs, self._scheduled_stop, args=(self.proc,))
                self.timer.daemon = True
                self.timer.start()

    def _stop_locked(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)   # the monitor writes its summary and report on Ctrl-C
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.log_fh is not None:
            self.log_fh.close()
            self.log_fh = None

    def _scheduled_stop(self, proc):
        """The scheduled test has run its full length: stop it, then build the PDF for the download."""
        with self.lock:
            if self.proc is not proc or proc.poll() is not None:
                return
            self.auto_stopped = True
            self._stop_locked()
            self.ends_at = None
        html_path = os.path.join(self.dir or "", "run.html")
        pdf_path = os.path.join(self.dir or "", "run.pdf")
        if self.dir and os.path.exists(html_path):
            with self.pdf_lock:
                self.pdf = {"state": "working", "error": ""}
            threading.Thread(target=self.make_pdf, args=(html_path, pdf_path), daemon=True).start()

    def stop(self):
        with self.lock:
            self._stop_locked()

    def log_tail(self, lines=14):
        if not self.dir:
            return []
        try:
            with open(os.path.join(self.dir, "monitor.log"), encoding="utf-8", errors="replace") as f:
                return [l.rstrip("\n") for l in f.readlines()[-lines:]]
        except OSError:
            return []

    def status(self):
        running = self.running()
        code = self.proc.poll() if self.proc is not None else None
        report = bool(self.dir) and os.path.exists(os.path.join(self.dir, "run.html"))
        csv = bool(self.dir) and os.path.exists(os.path.join(self.dir, "run.csv"))
        return {
            "csv": csv,
            "running": running,
            "exit_code": None if running or code is None else code,
            "config": self.cfg,
            "uptime_s": int(time.time() - self.started) if running and self.started else 0,
            "has_session": self.proc is not None,
            "report": report,
            "log": self.log_tail(),
            "ends_in_s": int(max(0, self.ends_at - time.time())) if running and self.ends_at else None,
            "duration_min": (self.cfg or {}).get("duration_min"),
            "auto_stopped": self.auto_stopped,
            "gop": self.gop_summary() if running else {},
            "monitor_presets": load_presets(),
            "focus_request": FOCUS["n"],
        }

    def gop_summary(self):
        """Keyframe interval and GOP size from the monitor, read at most every 5 seconds."""
        now = time.time()
        if now - getattr(self, "_gop_at", 0) < 5:
            return getattr(self, "_gop", {})
        self._gop_at = now
        try:
            raw, _ctype = self.fetch("/gop.json")
            self._gop = json.loads(raw.decode("utf-8", "replace") or "{}")
        except (LookupError, OSError, ValueError):
            self._gop = {}
        return self._gop

    def fetch(self, path):
        """Read a page from the running monitor's private dashboard server."""
        if not self.running():
            raise LookupError("no monitor is running")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.internal}{path}", timeout=5) as r:
            return r.read(), r.headers.get("Content-Type", "text/html; charset=utf-8")


CONTROL_PAGE = """<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>srtdebug control</title>
<style>
:root {
  --bg:#f4f6fa; --card:#ffffff; --soft:#eef1f7; --ink:#1a2233; --muted:#5f6b80; --line:#dde3ee;
  --field:#f7f9fc; --field-line:#b9c4d6; --field-hover:#8f9db5;
  --accent:#2563eb; --accent-soft:#e3ecfd; --accent-ink:#ffffff;
  --accent-2:#ea5f0c; --accent-2-soft:#fdeadb;
  --ok:#16a34a; --bad:#dc2626; --warn:#d97706;
}
:root[data-theme="dark"] {
  --bg:#0b0f15; --card:#131923; --soft:#1a2230; --ink:#e7ecf4; --muted:#8793a8; --line:#263044;
  --field:#0a0e14; --field-line:#3a4863; --field-hover:#56658a;
  --accent:#3b8cff; --accent-soft:#16294a; --accent-ink:#ffffff;
  --accent-2:#ff8a2a; --accent-2-soft:#3a2615;
  --ok:#34d399; --bad:#f87171; --warn:#fbbf24;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
.top { position: sticky; top: 0; z-index: 5; background: var(--card); border-bottom: 1px solid var(--line); }
.top-in { max-width: 1120px; margin: 0 auto; padding: 0 20px; height: 56px; display: flex; align-items: center; gap: 14px; }
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; }
.brand svg { width: 22px; height: 22px; }
.crumb { color: var(--muted); font-size: 13px; }
.spacer { flex: 1; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 26px 20px 56px; }
h1 { font-size: 24px; margin: 0 0 4px; letter-spacing: -.01em; }
.sub { color: var(--muted); margin: 0 0 22px; }
.layout { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 18px; align-items: start; }
@media (max-width: 900px) { .layout { grid-template-columns: 1fr; } }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px 22px; }
.kicker { font-size: 11.5px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; }
.card h2 { font-size: 15px; margin: 0 0 14px; font-weight: 650; }
.group { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); }
.group:first-of-type { margin-top: 0; padding-top: 0; border-top: none; }
.fields { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 16px; }
@media (max-width: 600px) { .fields { grid-template-columns: 1fr; } }
.field.full { grid-column: 1 / -1; }
label { display: block; font-size: 12.5px; color: var(--ink); font-weight: 600; margin-bottom: 6px; letter-spacing: .01em; }
/* fields are set apart from the cards: their own background, a firmer border, and a clear hover and focus */
input, select { width: 100%; padding: 9px 11px; border: 1px solid var(--field-line); border-radius: 9px;
                background-color: var(--field); color: var(--ink); font: inherit; outline: none;
                box-shadow: inset 0 1px 2px rgba(15, 23, 42, .08); transition: border-color .15s, box-shadow .15s; }
input:hover, select:hover { border-color: var(--field-hover); }
input:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
input:disabled, select:disabled { opacity: .55; cursor: not-allowed; }
select { appearance: none; -webkit-appearance: none; padding-right: 34px; cursor: pointer;
         background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%238793a8' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
         background-repeat: no-repeat; background-position: right 12px center; }
input::placeholder { color: var(--muted); opacity: .8; }
.check { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--soft); font-weight: 500; color: var(--ink); margin: 0; }
.check input { width: 16px; height: 16px; accent-color: var(--accent); margin: 0; }
.check span { font-size: 13.5px; }
.hint { color: var(--muted); font-size: 12.5px; margin-top: 4px; }
.cpu-note { margin-top: 10px; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--line); border-left: 3px solid var(--accent); background: var(--soft); color: var(--ink); font-size: 13px; line-height: 1.55; }
.cpu-note[hidden] { display: none; }
.actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 18px; }
.preflight { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 4px 0 18px; background: var(--soft); }
.pf-head { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; align-items: baseline; margin-bottom: 6px; }
.pf-list { list-style: none; margin: 0; padding: 0; }
.pf-list li { display: grid; grid-template-columns: 52px 1fr; gap: 4px 10px; padding: 6px 0; border-top: 1px solid var(--line); font-size: 13.5px; }
.pf-list li:first-child { border-top: 0; }
.pf-tag { font-size: 11px; font-weight: 700; letter-spacing: .04em; padding: 2px 0; }
.pf-PASS .pf-tag { color: var(--ok); } .pf-WARN .pf-tag { color: var(--warn); }
.pf-FAIL .pf-tag { color: var(--bad); } .pf-INFO .pf-tag { color: var(--muted); }
.pf-fix { grid-column: 2; color: var(--muted); font-size: 12.5px; }
.notice { border: 1px solid var(--line); border-left: 3px solid var(--warn); border-radius: 10px;
  padding: 10px 14px; margin: 0 0 14px; background: var(--card); font-size: 13.5px; }
.notice-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.notice-acts { display: flex; gap: 12px; }
.linkbtn { background: none; border: 0; padding: 0; font: inherit; font-size: 13px; font-weight: 600; color: var(--accent); cursor: pointer; }
.notice-list { margin-top: 8px; }
.done-layer { position: fixed; inset: 0; z-index: 60; background: color-mix(in srgb, var(--bg) 62%, transparent);
  backdrop-filter: blur(3px); display: flex; align-items: center; justify-content: center; padding: 16px; }
.done-layer[hidden] { display: none; }
.done-card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px 22px;
  width: min(480px, 100%); box-shadow: 0 18px 50px rgba(0,0,0,.25); }
.done-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.done-head h2 { margin: 0; font-size: 18px; }
.done-card p { margin: 10px 0 14px; }
.done-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.help { margin: 0 0 22px; line-height: 1.55; scroll-margin-top: 130px; }
.help h3 { font-size: 15px; margin: 16px 0 6px; }
.help ul { margin: 6px 0 0; padding-left: 20px; }
.help li { margin: 4px 0; }
.help-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.help-head h2 { margin: 0; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin: 0 0 22px;
  position: sticky; top: 56px; z-index: 4; background: var(--bg); padding-top: 4px; }
.tabs button { background: none; border: 0; padding: 10px 14px; margin-bottom: -1px; font: inherit; font-weight: 600;
               color: var(--muted); border-bottom: 2px solid transparent; cursor: pointer; }
.tabs button[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--accent); }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 9px 16px; border-radius: 9px; border: 1px solid var(--line);
       background: var(--soft); color: var(--ink); font: inherit; font-weight: 600; text-decoration: none; cursor: pointer; transition: transform .05s, opacity .15s; }
.btn:hover { border-color: var(--accent); }
.btn:active { transform: translateY(1px); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.btn.danger { color: var(--bad); }
.btn.primary.danger { background: var(--bad); border-color: var(--bad); color: #fff; }
.btn:disabled { opacity: .45; cursor: default; border-color: var(--line); }
.btn:disabled:hover { border-color: var(--line); }
.state-row { display: flex; gap: 14px; align-items: center; }
.led { width: 14px; height: 14px; border-radius: 50%; background: var(--muted); flex: none; }
.led.on { background: var(--ok); box-shadow: 0 0 0 5px color-mix(in srgb, var(--ok) 18%, transparent); }
.state { font-size: 20px; font-weight: 700; letter-spacing: -.01em; }
.muted { color: var(--muted); }
.facts { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 18px 0 0; }
.fact { background: var(--soft); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.fact .k { font-size: 11.5px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }
.fact .v { font-size: 15px; font-weight: 650; margin-top: 2px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.pill { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 700; padding: 4px 11px; border-radius: 999px;
        border: 1px solid var(--line); color: var(--muted); background: var(--soft); }
.pill.on { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, var(--line)); background: color-mix(in srgb, var(--ok) 10%, transparent); }
.pill .led { width: 8px; height: 8px; }
.err { color: var(--bad); margin-top: 12px; min-height: 1.3em; font-weight: 600; }
details { margin-top: 18px; border-top: 1px solid var(--line); padding-top: 14px; }
summary { cursor: pointer; color: var(--muted); font-weight: 600; font-size: 13px; }
pre { background: var(--soft); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; overflow-x: auto;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 12px 0 0; max-height: 280px; white-space: pre; }
.status { margin-bottom: 18px; }
.status-top { display: flex; justify-content: space-between; align-items: center; gap: 14px 20px; flex-wrap: wrap; }
.status .actions { margin-top: 0; justify-content: flex-end; }
.status .facts { margin-top: 16px; grid-template-columns: repeat(5, minmax(0, 1fr)); }
@media (max-width: 800px) { .status .facts { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.checks { display: grid; gap: 10px; }
.checks .check { align-items: flex-start; }
.check.follow { padding: 4px 8px; border: 0; background: none; font-size: 13px; }
.checks .check span { display: grid; gap: 2px; }
.checks .check strong { font-size: 14px; font-weight: 650; }
.checks .check small { color: var(--muted); font-size: 12.5px; font-weight: 500; }
.checks .check input { margin-top: 3px; }
.foot { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
        margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--line); }
.foot .err { margin: 0; min-height: 0; }
@media (max-width: 560px) {
  .top-in { height: auto; flex-wrap: wrap; padding: 10px 16px; gap: 10px; }
  .crumb { display: none; }
  .wrap { padding: 18px 16px 40px; }
  .status .actions { justify-content: flex-start; }
}
[hidden] { display: none !important; }

/* sub-tabs inside Decode stream */
.subtabs { display: inline-flex; gap: 4px; background: var(--soft); border: 1px solid var(--line); border-radius: 11px;
           padding: 4px; margin: 0 0 14px; }
.subtabs button { border: 0; background: none; padding: 7px 14px; border-radius: 8px; font: inherit; font-weight: 600;
                  color: var(--muted); cursor: pointer; }
.subtabs button[aria-selected="true"] { background: var(--card); color: var(--ink); box-shadow: inset 0 -2px 0 var(--accent-2); }
/* required fields: a shaded highlight, red while empty */
.field.req input, .field.req select { border-color: var(--accent-2); box-shadow: 0 0 0 3px var(--accent-2-soft); }
.field.req.empty input { border-color: var(--bad); box-shadow: 0 0 0 3px rgba(220, 38, 38, .14); }
/* with a destination URL, port and host are not needed: no highlight */
.field.req.optional input { border-color: var(--line); box-shadow: none; }
.field.req.optional.empty input { border-color: var(--line); box-shadow: none; }
.field.invalid input { border-color: var(--bad); box-shadow: 0 0 0 3px rgba(220, 38, 38, .14); }
.hint.bad { color: var(--bad); } .hint.good { color: var(--ok); }
.send-strip { display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap;
              margin: 0 0 10px; padding: 10px 12px; border-radius: 10px; background: var(--card);
              border: 1px solid var(--accent); font-size: 14px; position: sticky; top: 66px; z-index: 5;   /* just below the top bar */
              box-shadow: 0 2px 10px rgba(0, 0, 0, .25); }
.send-strip[hidden] { display: none !important; }
.send-strip { grid-column: 1 / -1; }   /* the page is a two-column grid; the strip spans it */
.dest-summary { margin: 0 0 12px; padding: 10px 12px; border-radius: 10px; background: var(--soft);
                border-left: 4px solid var(--accent); font-size: 14px; line-height: 1.5; }
.dest-summary b { color: var(--ink); }
.dest-summary .muted { color: var(--muted); }
/* top-bar meter: CPU and RAM bars with a graph of the last minute, like the top of a task manager */
.sysmeter { display: flex; gap: 10px; align-items: center; font-size: 11px; color: var(--muted); }
.sysmeter[hidden] { display: none !important; }
.sm-row { display: flex; gap: 5px; align-items: center; }
.sm-label { width: 26px; font-weight: 600; }
.sm-track { position: relative; width: 74px; height: 8px; border-radius: 4px; background: var(--line); overflow: hidden; }
.sm-fill { position: absolute; left: 0; top: 0; bottom: 0; width: 0; background: var(--accent); transition: width .6s; }
.sm-fill.mem { background: var(--accent-2); }
.sm-ours { position: absolute; left: 0; top: 0; bottom: 0; width: 0; background: var(--ink); opacity: .55; transition: width .6s; }
.sm-val { width: 92px; font-weight: 600; color: var(--ink); white-space: nowrap; }
.sm-graph { width: 120px; height: 26px; }
.sm-graph polyline { fill: none; stroke-width: 1.4; vector-effect: non-scaling-stroke; }
.sm-graph polyline.cpu { stroke: var(--accent); }
.sm-graph polyline.mem { stroke: var(--accent-2); }
.sysmeter.hot .sm-val { color: var(--bad); }
.sysmeter.hot .sm-fill { background: var(--bad); }
@media (max-width: 900px) { .sysmeter { display: none !important; } }

/* Encode stream pane: numbered steps and compact action rows */
.card-head h2 { margin-bottom: 4px; }
.card-head p { margin: 0 0 4px; }
.step { display: flex; align-items: center; gap: 10px; margin: 22px 0 12px; font-weight: 650; font-size: 14px; }
.step-n { width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--accent-2); background: var(--accent-2-soft); color: var(--accent-2);
          display: inline-grid; place-items: center; font-size: 12px; font-weight: 700; flex: 0 0 auto; }
.actions.tight { margin-top: 12px; }
.feed-state { border: 1px solid var(--line); border-left: 4px solid var(--muted); border-radius: 12px;
              padding: 14px 16px; margin: 6px 0 14px; background: var(--soft); }
.feed-state[data-phase="connected"] { border-left-color: var(--ok); border-color: var(--ok); background: rgba(46, 160, 67, .14); }
.feed-state[data-phase="connected"] .feed-phase { color: var(--ok); font-weight: 750; font-size: 16px; }
.feed-state[data-phase="connected"] #feed-status { color: var(--ink); }
.feed-state[data-phase="connecting"], .feed-state[data-phase="waiting"] { border-left-color: var(--accent-2); }
.feed-state[data-phase="refused"], .feed-state[data-phase="stopped"] { border-left-color: var(--bad); }
.feed-phase { font-size: 20px; font-weight: 700; letter-spacing: -.01em; }
.feed-detail { margin: 4px 0 0; color: var(--muted); }
.drop { border-radius: 10px; padding: 6px; margin: -6px; transition: background .15s; }
.drop.dropping { background: var(--soft); outline: 2px dashed var(--accent); outline-offset: 2px; }
.drop .hint.bad { color: var(--bad); }
.feed-facts { margin: 12px 0 0; grid-template-columns: repeat(4, minmax(0, 1fr)); }
.feed-facts[hidden] { display: none; }
.feed-alerts { margin: 12px 0 0; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--warn); background: var(--soft); color: var(--ink); }
.feed-alerts[hidden] { display: none; }
.feed-alerts .head { color: var(--warn); font-weight: 650; margin-bottom: 4px; }
.feed-alerts p { margin: 4px 0 0; }
@media (max-width: 800px) { .feed-facts { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.feed-log-title { font-weight: 700; font-size: 13px; }
.steps { margin: 4px 0 16px; }
.steps button { white-space: nowrap; }
.step-nav { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 18px;
            padding-top: 14px; border-top: 1px solid var(--line); }
.feed-logbox { margin: 18px 0 0; }
.feed-logbox .feed-log-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
.feed-logbox .feed-log-head .linkbtn { margin-left: auto; }
.level-pick { display: inline-flex; align-items: center; gap: 6px; }
.level-pick select { width: auto; padding: 5px 8px; }
.view-pick { display: inline-flex; gap: 4px; background: var(--soft); border: 1px solid var(--line); border-radius: 9px; padding: 3px; margin: 10px 0 8px; }
.view-pick button { border: 0; background: none; padding: 5px 12px; border-radius: 7px; font: inherit; font-size: 13px; font-weight: 600; color: var(--muted); cursor: pointer; }
.view-pick button[aria-selected="true"] { background: var(--card); color: var(--ink); box-shadow: inset 0 -2px 0 var(--accent-2); }
.readable { list-style: none; margin: 0; padding: 4px 2px; height: 300px; overflow: auto; background: var(--soft);
  border: 1px solid var(--line); border-radius: 10px; }
.readable li { display: grid; grid-template-columns: 96px 1fr; gap: 10px; padding: 6px 10px; border-bottom: 1px solid var(--line); font-size: 13px; }
.readable li:last-child { border-bottom: 0; }
.readable .when { font: 12px ui-monospace, Menlo, Consolas, monospace; color: var(--muted); }
.readable .n { color: var(--muted); font-size: 12px; margin-left: 6px; }
.readable .ok { color: var(--ok); } .readable .bad { color: var(--bad); } .readable .muted { color: var(--muted); }
.feed-logbox pre { height: 300px; margin: 8px 0 0; overflow: auto; font: 12px/1.5 ui-monospace, Menlo, Consolas, monospace;
  background: var(--soft); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; white-space: pre-wrap; word-break: break-word; }
.feed-events { margin: 0 0 14px; }
.feed-events-list { margin: 6px 0 0; padding-left: 22px; line-height: 1.7; }
.feed-events-list li span.n { color: var(--muted); font-size: 12px; margin-left: 6px; }
.feed-state[data-phase="dropped"] { border-left-color: var(--bad); }
.feed-state[data-phase="unanswered"] { border-left-color: var(--warn); border-color: var(--warn); background: rgba(217, 119, 6, .14); }
.feed-state[data-phase="unanswered"] .feed-phase { color: var(--warn); font-weight: 750; font-size: 16px; }
.feed-log-head { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; flex-wrap: wrap; }
.feed-log-head .linkbtn { margin-left: auto; }
.feed-log pre { margin: 0; height: 300px; overflow: auto; overflow: auto; font: 12px/1.5 ui-monospace, Menlo, Consolas, monospace;
                background: var(--soft); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px;
                white-space: pre-wrap; word-break: break-word; }
.format-box { white-space: pre-wrap; font: 12px/1.6 ui-monospace, Menlo, Consolas, monospace; background: var(--soft);
              border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 12px 0 0; }

/* Layout like a device console: section list on the left, the page on the right */
.wrap.shell { max-width: 1180px; display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 28px; align-items: start; }
.wrap.shell .content { min-width: 0; }
.tabs { flex-direction: column; gap: 2px; border: 0; margin: 0; padding: 0; background: none;
        position: sticky; top: 76px; }
.tabs button { text-align: left; padding: 10px 12px; border-radius: 9px; border-bottom: 0; margin: 0;
               border-left: 3px solid transparent; }
.tabs button:hover { background: var(--soft); }
.tabs button[aria-selected="true"] { background: var(--card); border-left-color: var(--accent-2);
               box-shadow: 0 1px 2px rgba(20,24,40,.06); }
@media (max-width: 860px) {
  .wrap.shell { grid-template-columns: 1fr; gap: 0; }
  .tabs { flex-direction: row; position: sticky; top: 56px; z-index: 4; background: var(--bg);
          border-bottom: 1px solid var(--line); margin: 0 0 18px; padding-top: 4px; }
  .tabs button { border-left: 0; border-bottom: 2px solid transparent; border-radius: 0; margin-bottom: -1px; }
  .tabs button[aria-selected="true"] { background: none; box-shadow: none; border-bottom-color: var(--accent-2); }
}
.dlg-back { position: fixed; inset: 0; background: rgba(0, 0, 0, .55); display: flex; align-items: center; justify-content: center; padding: 16px; z-index: 50; }
.dlg-back[hidden] { display: none; }
.dlg { width: min(480px, 100%); background: var(--card); color: var(--ink); border: 1px solid var(--line); border-radius: 12px; padding: 20px; box-shadow: 0 20px 60px rgba(0, 0, 0, .5); }
.dlg h3 { margin: 0 0 6px; font-size: 17px; }
.dlg label { display: block; margin-top: 12px; }
.dlg .row { display: flex; gap: 8px; margin-top: 6px; }
.dlg .row input { flex: 1; min-width: 0; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--soft); color: var(--ink); font: inherit; }
.dlg .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
.dest-box { border: 1px solid var(--line); border-radius: 10px; padding: 12px; background: var(--soft); }
.dest-head { font-weight: 600; margin-bottom: 8px; }
.dest-list { list-style: none; padding: 0; margin: 0 0 10px; }
.dest-list li { display: flex; gap: 10px; align-items: center; justify-content: space-between; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; margin-bottom: 6px; background: var(--card); }
.dest-list li.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.dest-list .info { min-width: 0; overflow: hidden; }
.dest-list .name { font-weight: 600; display: block; }
.dest-list .sum { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: block; }
.dest-list .acts { display: flex; gap: 6px; flex-shrink: 0; }
.dest-save { display: flex; gap: 8px; }
#presets { margin-bottom: 14px; }
/* the start screen: two large buttons, one for each function */
.wrap.shell.home-mode { grid-template-columns: minmax(0, 1fr); }
.home-mode .tabs { display: none; }
.home { grid-column: 1 / -1; padding: 8px 0 24px; }
.home[hidden] { display: none; }
.home-title { font-size: 22px; margin: 8px 0 18px; }
.home-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 18px; }
.home-btn { display: flex; flex-direction: column; justify-content: flex-end; gap: 10px; min-height: 200px; padding: 28px;
  text-align: left; font: inherit; color: var(--ink); background: var(--card); border: 1px solid var(--line); border-radius: 16px; cursor: pointer; }
.home-btn:hover, .home-btn:focus-visible { border-color: var(--accent); outline: none; box-shadow: 0 0 0 2px var(--accent) inset; }
.home-name { font-size: 26px; font-weight: 700; }
.home-desc { color: var(--muted); line-height: 1.5; }
.dest-save input, .dest-save select { flex: 1; min-width: 0; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--card); color: var(--ink); font: inherit; }
.cmd { margin-top: 14px; }
.cmd summary { cursor: pointer; font-weight: 600; }
.advanced { border: 1px dashed var(--line); border-radius: 10px; padding: 8px 12px; }
.custom-prompt { display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap;
                 padding: 10px 12px; border-radius: 10px; background: var(--soft); font-size: 14px; }
.advanced summary { color: var(--ink); }
.advanced[open] { padding-bottom: 12px; }
.dest-main input { font-size: 16px; padding: 10px 12px; }
.cmdbox { white-space: pre-wrap; word-break: break-all; background: var(--soft); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; font-size: 12px; margin: 8px 0; }
</style></head>
<body>
<header class="top"><div class="top-in">
  <div class="brand"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-6 4 12 2-6h6" fill="none" stroke="var(--accent-2)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg><span>srtdebug</span></div>
  <div class="crumb">Control panel</div>
  <div class="spacer"></div>
  <div class="sysmeter" id="sysmeter" hidden title="CPU and memory of this computer. The darker part of the CPU bar is what this panel's feed and monitor use.">
    <div class="sm-row"><span class="sm-label">CPU</span><div class="sm-track"><div class="sm-fill" id="sm-cpu"></div><div class="sm-ours" id="sm-ours"></div></div><b class="sm-val" id="sm-cpu-t">-</b></div>
    <div class="sm-row"><span class="sm-label">RAM</span><div class="sm-track"><div class="sm-fill mem" id="sm-mem"></div></div><b class="sm-val" id="sm-mem-t">-</b></div>
    <svg class="sm-graph" id="sm-graph" viewBox="0 0 120 26" preserveAspectRatio="none" aria-hidden="true"><polyline id="sm-line-cpu" class="cpu" points=""/><polyline id="sm-line-mem" class="mem" points=""/></svg>
  </div>
  <span class="pill" id="pill"><span class="led"></span><span id="pilltext">Checking</span></span>
  <button class="btn primary" type="submit" form="cfg" id="start">Start monitoring</button>
  <button class="btn" id="help-btn" type="button" aria-label="How this tool works" aria-expanded="false" aria-controls="pane-help">?</button>
  <button class="btn" id="theme" type="button" aria-label="Toggle light and dark theme">Theme</button>
  <button class="btn" id="quit" type="button" title="Stop srtdebug. It also stops by itself a few minutes after this window is closed.">Quit</button>
</div></header>

<main class="wrap shell home-mode" id="main">
  <div class="send-strip" id="send-strip" hidden role="status">
    <span id="send-text"></span>
    <button class="btn" id="send-go" type="button">Show Encode stream</button>
  </div>
  <nav class="tabs" role="tablist">
    <button type="button" role="tab" id="tab-home" aria-selected="false" aria-controls="pane-home">&lsaquo; Start</button>
    <button type="button" role="tab" id="tab-control" aria-selected="true" aria-controls="pane-control">Decode stream</button>
    <button type="button" role="tab" id="tab-feed" aria-selected="false" aria-controls="pane-feed">Encode stream</button>
  </nav>
  <div class="content">
  <section id="pane-help" class="card help" hidden aria-label="How this tool works">
    <div class="help-head"><h2>How this tool works</h2>
      <button type="button" class="btn" id="help-close">Close</button></div>
    <p>srtdebug listens to an SRT stream (or connects to one as a caller), measures it, and tells you whether
      the stream is healthy. It runs on this computer and never sends your stream anywhere.</p>
    <p><strong>Decode stream</strong> receives and checks a stream coming in to this computer.
      <strong>Encode stream</strong> sends a test clip out to a receiver, so you can check the far end.</p>
    <h3>1. Start a monitor</h3>
    <ul>
      <li>On the <strong>Decode stream</strong> tab choose <strong>Listener</strong> to wait for a sender, or
        <strong>Caller</strong> to connect to a remote listener. Enter the port and the latency.</li>
      <li>Press <strong>Start monitoring</strong>. The button becomes <strong>Stop monitoring</strong>, and
        <strong>Open live dashboard</strong> opens the live view in a new browser tab.</li>
      <li>A stream ID or passphrase is optional. The passphrase is used for this run only and is not stored.</li>
    </ul>
    <h3>2. Read the status</h3>
    <ul>
      <li><strong>Healthy</strong>: no warnings or errors in the last 5 seconds. Earlier problems stay listed.</li>
      <li><strong>Degraded</strong>: at least one warning in the last 5 seconds. Click the badge to see the issues.</li>
      <li><strong>Failing</strong>: at least one error in the last 5 seconds.</li>
      <li>The strip shows one cell per 5 seconds, coloured by the worst state in it. Grey means no data.</li>
    </ul>
    <h3>3. What is measured</h3>
    <ul>
      <li><strong>Transport:</strong> bitrate, round-trip time, packet loss. SRT repairs most lost packets by
        retransmitting them, so only packets that arrive too late count as loss in the verdict.</li>
      <li><strong>Transport stream</strong> (when analysis is on): codecs, bitrates, keyframe interval, rate mode,
        and the ETSI TR 101 290 checks. The likely encoder is the name the sender wrote into the stream.</li>
      <li><strong>Picture and sound</strong> (needs ffmpeg): black and white levels, loudness, silence and freeze.
        These are noted but do not change the verdict.</li>
    </ul>
    <h3>4. Stop and get the reports</h3>
    <ul>
      <li>Press <strong>Stop monitoring</strong>. The HTML report, the PDF (if Chrome, Chromium or Edge is installed),
        and the CSV are listed under the status line.</li>
      <li>For a fixed run, set <strong>Scheduled test length</strong> before starting. The monitor stops by itself,
        and <strong>All reports (zip)</strong> gives you everything in one file.</li>
      <li>A report needs some received data. With no data there is only the CSV and the log.</li>
    </ul>
    <h3>5. Thresholds</h3>
    <p>The <strong>Health thresholds</strong> card on the Decode stream tab sets the levels for warnings and failures.
      Changes apply to the next monitor you start. Each report lists the thresholds it used.</p>
    <h3>Without this panel</h3>
    <p>The same monitor runs from a terminal, for example <code>python3 srtdebug.py monitor --mode listener --port 9000</code>.
      Run <code>python3 srtdebug.py --help</code> for the options.</p>
  </section>


  <section id="pane-home" class="home" aria-label="Start" hidden>
    <h1 class="home-title">What do you want to do?</h1>
    <div class="home-grid">
      <button type="button" class="home-btn" id="home-decode">
        <span class="home-name">Decode a stream</span>
        <span class="home-desc">Receive an SRT stream from a sender and check the picture and sound: frozen or black pictures, silence, loss and delay.</span>
      </button>
      <button type="button" class="home-btn" id="home-encode">
        <span class="home-name">Encode a stream</span>
        <span class="home-desc">Send a sample clip out over SRT to a receiver, to test the path or the receiver.</span>
      </button>
    </div>
  </section>
  <section id="pane-control">
    <div class="notice" id="notice" hidden role="status">
      <div class="notice-row">
        <span class="notice-text" id="notice-text"></span>
        <span class="notice-acts">
          <button type="button" class="linkbtn" id="notice-toggle" aria-expanded="false">Details</button>
          <button type="button" class="linkbtn" id="notice-close" aria-label="Dismiss">Dismiss</button>
        </span>
      </div>
      <ul class="pf-list notice-list" id="notice-list" hidden></ul>
    </div>
    <div class="card status" aria-live="polite">
      <div class="status-top">
        <div class="state-row"><span class="led" id="dot"></span>
          <div><div class="state" id="state">Checking...</div><div class="muted" id="detail"></div></div></div>
        <div class="actions">
          <button class="btn" id="view" type="button">Open live dashboard</button>
          <a class="btn" id="report" href="/report?download=1" download="srtdebug-report.html" hidden>HTML report</a>
          <a class="btn" id="report-pdf" href="/report.pdf" hidden>PDF report</a>
          <a class="btn" id="report-csv" href="/report.csv" download hidden>CSV samples</a>
          <a class="btn" id="report-zip" href="/reports.zip" hidden>All reports (zip)</a>
          <button class="btn danger" id="stop" type="button" disabled>Stop</button>
        </div>
      </div>
      <div class="facts" id="facts"></div>
      <details id="logwrap" hidden><summary>Monitor log</summary><pre id="log"></pre></details>
    </div>

    <div class="card dest-box" id="presets">
      <div class="dest-head">Saved presets <span class="muted small">(connection settings; not the passphrase)</span></div>
      <div class="dest-save">
        <select id="preset-pick" aria-label="Choose a saved preset"><option value="">None saved yet</option></select>
        <button class="btn danger" id="preset-del" type="button" disabled>Delete</button>
      </div>
      <div class="hint" id="preset-state"></div>
      <div class="dest-save">
        <input id="preset-name" placeholder="Name for these settings" maxlength="60" aria-label="Preset name">
        <button class="btn primary" id="preset-save" type="button">Save</button>
        <button class="btn" id="preset-new" type="button">New</button>
      </div>
      <div class="hint" id="preset-msg">Set the connection below, give it a name and press Save to keep it for next time.</div>
    </div>

    <div class="subtabs" role="tablist" aria-label="Decode stream sections">
      <button type="button" role="tab" id="sub-conn" aria-selected="true" aria-controls="cfg">Connection</button>
      <button type="button" role="tab" id="sub-thr" aria-selected="false" aria-controls="thr">Health thresholds</button>
    </div>
    <form class="card" id="cfg" novalidate>
      <div class="group">
        <h2>Connection</h2>
        <div class="fields">
          <div class="field full"><label for="mode">Mode</label>
            <select id="mode" name="mode">
              <option value="listener">Listener: wait for a remote sender</option>
              <option value="caller">Caller: connect to a remote listener</option>
            </select>
            <div class="hint" id="hint"></div></div>
          <div class="field req"><label for="port">SRT port (UDP)</label>
            <input id="port" name="port" type="number" min="1" max="65535" value="9000" required></div>
          <div class="field"><label for="latency">Latency (ms)</label>
            <input id="latency" name="latency" type="number" min="0" max="60000" value="20">
            <div class="hint">20 ms suits short paths. Use 3-4 x RTT on a WAN.</div></div>
          <div class="field full req" id="hostfield"><label for="host" id="hostlabel">Remote host (required)</label>
            <input id="host" name="host" placeholder="blank for listener"></div>
          <div class="field full"><label for="streamid">Stream ID (optional)</label>
            <input id="streamid" name="streamid" placeholder="e.g. live/cam1"></div>
          <div class="field"><label for="duration">Scheduled test length (minutes)</label>
            <input id="duration" name="duration" type="number" min="1" max="1440" placeholder="until stopped">
            <div class="hint">Stops by itself after this long, then offers all reports as one download.</div></div>
        </div>
      </div>
      <div class="preflight" id="preflight" hidden aria-live="polite">
        <div class="pf-head"><strong>Before you listen</strong><span class="muted small" id="pf-sum">Checking this PC…</span></div>
        <ul class="pf-list" id="pf-list"></ul>
      </div>
      <div class="group">
        <h2>Security</h2>
        <div class="fields">
          <div class="field full"><label for="passphrase">Passphrase (optional, 10-80 characters)</label>
            <input id="passphrase" name="passphrase" type="password" autocomplete="off" maxlength="80"></div>
          <div class="field"><label for="pbkeylen">Key length</label>
            <select id="pbkeylen" name="pbkeylen">
              <option value="">SRT default</option><option value="16">AES-128</option>
              <option value="24">AES-192</option><option value="32">AES-256</option>
            </select></div>
        </div>
      </div>
      <div class="group">
        <h2>Analysis</h2>
        <div class="checks">
          <label class="check"><input id="payload" name="payload" type="checkbox" checked>
            <span><strong>Analyse the transport stream</strong><small>Codecs, bitrates, integrity and the suspected side of any fault.</small></span></label>
          <label class="check"><input id="vid" name="view" type="checkbox">
            <span><strong>Live video in the dashboard</strong><small>Needs ffmpeg; about 8 s behind the stream.</small></span></label>
        </div>
      </div>
      <div class="foot">
        <span class="muted">Press <strong>Start monitoring</strong> at the top to begin. It changes to Stop while running.</span>
        <div class="err" id="err" role="alert"></div>
      </div>
    </form>
    <form class="card" id="thr" novalidate hidden>
      <div class="group">
        <h2>Health thresholds</h2>
        <p class="muted small">Warning and failure levels. Saved on this computer, and used from the next monitor you start.</p>
        <div class="fields" id="thr-fields"></div>
        <div class="foot">
          <span class="muted" id="thr-note">Showing the saved values.</span>
          <span><button type="button" class="ghost" id="thr-reset">Restore defaults</button>
                <button type="submit" id="thr-save">Save thresholds</button></span>
        </div>
        <div class="err" id="thr-err" role="alert"></div>
      </div>
    </form>
  </section>

  <section id="pane-feed" hidden>
    <section class="card" id="feedcard" aria-label="Encode stream">
      <div class="group">
        <div class="card-head">
          <h2>Encode stream</h2>
          <p class="muted small">Send a sample clip, or your own video, over SRT as if it were a live feed.</p>
        </div>
        <div class="feed-state" id="feed-state" data-phase="idle">
          <div class="feed-phase" id="feed-phase">Not sending</div>
          <p class="feed-detail" id="feed-status" aria-live="polite">Set where the feed goes below, then press Start feed.</p>
          <p class="feed-detail" id="feed-stats"></p>
          <p class="feed-detail" id="feed-peer" hidden></p>
          <p class="feed-detail" id="feed-wifi" hidden></p>
          <div class="feed-alerts" id="feed-alerts" role="alert" hidden></div>
          <div class="facts feed-facts" id="feed-facts" hidden></div>
        </div>
        <div class="feed-events" id="feed-events-wrap" hidden>
          <div class="feed-log-title">What happened</div>
          <p class="feed-detail" id="feed-counts"></p>
          <ol class="feed-events-list" id="feed-events"></ol>
        </div>
        <div class="subtabs steps" role="tablist" aria-label="Encode stream steps">
          <button type="button" role="tab" id="step-btn-1" data-go="1" aria-selected="true" aria-controls="feedcard">1 Where it goes</button>
          <button type="button" role="tab" id="step-btn-2" data-go="2" aria-selected="false" aria-controls="feedcard">2 Send</button>
        </div>
        <div class="feed-step" data-step="1">
        <div class="fields">
          <div class="field full dest-box">
            <div class="dest-head">Saved destinations <span class="muted small">(name, address and options; not the passphrase)</span></div>
            <div class="dest-save">
              <select id="dest-pick" aria-label="Choose a saved destination"><option value="">None saved yet</option></select>
              <button class="btn danger" id="dest-del" type="button" disabled>Delete</button>
            </div>
            <div class="hint" id="dest-state"></div>
            <div class="dest-save">
              <input id="dest-name" placeholder="Name for the current fields" maxlength="60" aria-label="Destination name">
              <button class="btn primary" id="dest-save" type="button">Save</button>
              <button class="btn" id="dest-new" type="button">New</button>
            </div>
            <div class="hint" id="dest-msg">Fill in the destination, then press Save to keep it for next time.</div>
          </div>
          <div class="field full req dest-main"><label for="feed-host">Where it goes: address</label>
            <input id="feed-host" placeholder="203.0.113.5" autocomplete="off">
            <div class="hint">The IP address or name of the receiving monitor or media server.</div></div>
          <div class="field req dest-main"><label for="feed-port">Port</label>
            <input id="feed-port" type="number" min="1" max="65535" value="9100"></div>
          <div class="field full"><label for="feed-url">Full address instead of host and port (optional)</label>
            <input id="feed-url" placeholder="srt://203.0.113.5:9000?streamid=live/cam1">
            <div class="hint" id="feed-url-msg">Leave empty to use the host and port above.</div></div>
          <div class="field"><label for="feed-streamid">Stream ID (if the receiver needs one)</label>
            <input id="feed-streamid" placeholder="live/cam1"></div>
          <div class="field"><label for="feed-passphrase">Passphrase (if the receiver needs one)</label>
            <input id="feed-passphrase" type="password" autocomplete="off" maxlength="80">
            <div class="hint">10-80 characters. Not stored.</div></div>
        </div>
        <div class="step-nav"><span></span><button class="btn primary" type="button" data-go="2">Next</button></div>
        </div>
        <div class="feed-step" data-step="2" hidden>
        <div class="dest-summary" id="dest-summary" role="status"></div>
        <div class="fields">
          <div class="field full"><label for="feed-clip">Sample clip</label>
            <select id="feed-clip"></select>
            <div class="hint" id="feed-cliphint"></div></div>
          <div class="field full drop" id="feed-drop"><label for="feed-source">Your own video (optional)</label>
            <input id="feed-source" placeholder="Drop a video file here, or type its path">
            <div class="hint" id="feed-drop-msg">H.264, H.265 or MPEG-2 in MP4, MOV, MKV or TS.</div></div>
          <div class="field full"><label for="feed-encode">Encoding</label>
            <select id="feed-encode">
              <option value="copy">Send as it is (no re-encode)</option>
              <option value="h264">Re-encode to H.264 (set up for this computer)</option>
            </select>
            <div class="cpu-note" id="feed-cpu-note" hidden></div>
          </div>
          <div class="field full"><label class="check"><input id="feed-clock" type="checkbox">
            <span><strong>Show a clock on the picture</strong></span></label></div>
          <div class="field full custom-prompt" id="custom-prompt" hidden>
            <span>Re-encoding uses this computer's settings. Change the bit rate or other options in Custom settings.</span>
            <button class="btn" id="custom-open" type="button">Open Custom settings</button></div>
          <details class="advanced full" id="custom-details">
            <summary>Custom settings</summary>
            <div class="fields">
              <div class="field"><label for="feed-mode">Connection</label>
                <select id="feed-mode"><option value="caller">Caller: connect to a listener</option>
                  <option value="listener">Listener: wait for a monitor</option></select></div>
              <div class="field"><label for="feed-latency">Latency (ms)</label>
                <input id="feed-latency" type="number" min="0" max="60000" placeholder="500, or 5000 on Wi-Fi">
                <label class="check follow"><input id="feed-allow-low" type="checkbox"><span>Allow a shorter latency on Wi-Fi</span></label></div>
              <div class="field full"><label class="check follow"><input id="feed-on-wifi" type="checkbox"><span>This PC is on Wi-Fi</span></label>
                <div class="hint" id="feed-network-msg">Checking the connection...</div></div>
              <div class="field"><label for="feed-pbkeylen">Encryption strength</label>
                <select id="feed-pbkeylen"><option value="">AES-128 (default)</option>
                  <option value="24">AES-192</option><option value="32">AES-256</option></select>
                <div class="hint">Only used with a passphrase.</div></div>
              <div class="field"><label for="feed-rate">Bit rate (Mb/s), re-encode only</label>
                <input id="feed-rate" type="number" min="0.5" max="50" step="0.5" placeholder="6" autocomplete="off">
                <div class="hint" id="feed-rate-hint">Above the limit you will be asked to confirm.</div></div>
            </div>
          </details>

        </div>
        <div class="actions tight">
          <button class="btn" id="feed-check" type="button">Check format</button>
          <button class="btn" id="feed-make" type="button" title="Writes a set of test clips, one per video format, into a folder you choose. Then pick one in Sample clip to send.">Make sample clips</button>
        </div>
        <pre id="feed-format" class="format-box" hidden></pre>
        <div class="checks"><label class="check"><input id="feed-loop" type="checkbox" checked>
          <span><strong>Repeat until stopped</strong></span></label></div>
        <div class="actions tight">
          <button class="btn primary" id="feed-start" type="button">Start feed</button>
          <button class="btn danger" id="feed-stop" type="button" disabled>Stop feed</button>
        </div>
        <details class="cmd" open>
          <summary>How this is sent</summary>
          <p class="muted small">ffmpeg reads the clip and srt-live-transmit sends it. The passphrase is hidden.</p>
          <pre id="feed-cmd" class="cmdbox">Fill in the fields to see the command.</pre>
          <p class="small" id="feed-cmd-id"></p>
          <div class="actions tight"><button class="btn" id="feed-cmd-copy" type="button">Copy command</button></div>
        </details>
        <div class="feed-logbox" id="feed-logbox">
          <div class="feed-log-head">
            <span class="feed-log-title">Debug log</span>
            <span class="muted small" id="feed-loginfo"></span>
            <label class="level-pick"><span class="muted small">Level</span>
              <select id="feed-level">
                <option value="warn">Warnings</option><option value="note">Notes</option>
                <option value="info">Info</option><option value="debug">Debug</option>
              </select></label>
            <a class="linkbtn" href="/feed/log" download="srtdebug-feed.log">Download</a>
            <label class="check follow"><input id="feed-follow" type="checkbox" checked><span>Follow</span></label>
            <button class="btn" id="feed-clearview" type="button" disabled>Clear from view</button>
          </div>
          <p class="muted small" id="feed-level-note">Changing the level restarts a running feed briefly. The log carries on.</p>
          <p class="muted small">Download keeps the whole log. Clear from view only hides it here.</p>
          <div class="view-pick" role="tablist" aria-label="Log view">
            <button type="button" role="tab" id="view-readable" aria-selected="true">Readable</button>
            <button type="button" role="tab" id="view-raw" aria-selected="false">Raw</button>
          </div>
          <ol class="readable" id="feed-readable"><li class="muted">Plain-language messages appear here once a feed starts.</li></ol>
          <pre id="feed-log" hidden>Debug messages from the transmitter appear here once a feed starts.</pre>
        </div>
        <div class="step-nav"><button class="btn" type="button" data-go="1">Back</button></div>
        </div>
        <p class="muted small" id="feed-dir-line">Clips are kept in <span id="feed-dir"></span>.</p>
        <div class="dlg-back" id="samples-back" hidden>
          <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="samples-title">
            <h3 id="samples-title">Make sample clips</h3>
            <p class="muted small">Choose the folder for the clips. About <span id="samples-size">200</span> MB is written there. Clips already in that folder with the same names are replaced.</p>
            <label class="small muted" for="samples-dir">Folder</label>
            <div class="row"><input id="samples-dir" type="text" spellcheck="false"><button class="btn" type="button" id="samples-browse">Browse…</button></div>
            <div class="err" id="samples-err" role="alert"></div>
            <div class="actions"><button class="btn" type="button" id="samples-cancel">Cancel</button><button class="btn primary" type="button" id="samples-go">Make clips</button></div>
          </div>
        </div>
        <div class="dlg-back" id="stopped-back" hidden>
          <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="stopped-title">
            <h3 id="stopped-title">Encode stream stopped</h3>
            <p class="muted small">The log stays in the panel and in Download until the next feed starts. Download it now if you want to keep a copy.</p>
            <div class="actions">
              <a class="btn" href="/feed/log" download="srtdebug-feed.log" id="stopped-download">Download log</a>
              <button class="btn primary" type="button" id="stopped-done">Done</button>
            </div>
          </div>
        </div>
        <div class="err" id="feed-err" role="alert"></div>
      </div>
    </section>
  </section>

  </div>
</main>
<div class="done-layer" id="feed-done-layer" hidden>
  <div class="done-card" role="dialog" aria-modal="true" aria-labelledby="feed-done-title">
    <div class="done-head">
      <h2 id="feed-done-title">Encode stopped</h2>
      <button type="button" class="btn sm" id="feed-done-close">Close (Esc)</button>
    </div>
    <p class="muted">Download the log of this feed if you need it, or close to go back to the start.</p>
    <div class="done-actions">
      <a class="btn primary" id="feed-done-log" href="/feed/log" download="srtdebug-feed.log">Feed log (text)</a>
    </div>
  </div>
</div>
<div class="done-layer" id="done-layer" hidden>
  <div class="done-card" role="dialog" aria-modal="true" aria-labelledby="done-title">
    <div class="done-head">
      <h2 id="done-title">Monitoring stopped</h2>
      <button type="button" class="btn sm" id="done-close">Close (Esc)</button>
    </div>
    <p class="muted" id="done-status">Preparing your reports…</p>
    <div class="done-actions">
      <a class="btn primary" id="done-zip" href="/reports.zip" hidden>All reports (zip)</a>
      <a class="btn" id="done-html" href="/report?download=1" download="srtdebug-report.html" hidden>HTML report</a>
      <a class="btn" id="done-pdf" href="/report.pdf" hidden>PDF report</a>
      <a class="btn" id="done-csv" href="/report.csv" download hidden>CSV samples</a>
    </div>
  </div>
</div>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var root = document.documentElement;

  // the header button does the job of the section on screen: start or stop a monitor, or a test feed
  function syncTop() {
    var b = $('start'), feed = window.paneMode === 'feed';
    // the start screen has its own two buttons, so the header button is not shown there
    b.hidden = window.paneMode === 'home';
    if (window.paneMode === 'home') { return; }
    if (feed) {
      var fr = !!window.feedRunning;
      b.type = 'button'; b.removeAttribute('form');
      b.textContent = fr ? 'Stop feed' : 'Start feed';
      b.classList.toggle('danger', fr);
    } else {
      b.type = 'submit'; b.setAttribute('form', 'cfg');
      b.textContent = window.srtRunning ? 'Stop monitoring' : 'Start monitoring';
      b.classList.toggle('danger', !!window.srtRunning);
    }
  }
  window.syncTop = syncTop;
  // 'home' is the start screen with the two buttons; 'control' is Decode, 'feed' is Encode
  function showPane(name) {
    window.paneMode = name === 'feed' || name === 'control' ? name : 'home';
    $('pane-home').hidden = window.paneMode !== 'home';
    $('pane-control').hidden = window.paneMode !== 'control';
    $('pane-feed').hidden = window.paneMode !== 'feed';
    $('main').classList.toggle('home-mode', window.paneMode === 'home');
    $('tab-control').setAttribute('aria-selected', window.paneMode === 'control' ? 'true' : 'false');
    $('tab-feed').setAttribute('aria-selected', window.paneMode === 'feed' ? 'true' : 'false');
    if (window.updateSendStrip) { window.updateSendStrip(); }
    syncTop();
    window.scrollTo(0, 0);
  }
  window.showPane = showPane;
  syncTop();
  $('tab-control').addEventListener('click', function () { showPane('control'); });
  $('tab-home').addEventListener('click', function () { showPane('home'); });
  $('home-decode').addEventListener('click', function () { showPane('control'); });
  $('home-encode').addEventListener('click', function () { showPane('feed'); });
  showPane('home');
  function showSub(name) {
    var thr = name === 'thr';
    $('cfg').hidden = thr; $('thr').hidden = !thr;
    $('sub-conn').setAttribute('aria-selected', thr ? 'false' : 'true');
    $('sub-thr').setAttribute('aria-selected', thr ? 'true' : 'false');
  }
  $('sub-conn').addEventListener('click', function () { showSub('conn'); });
  $('sub-thr').addEventListener('click', function () { showSub('thr'); });
  // a required field is highlighted, and turns red while it is empty
  function syncRequired() {
    ['port', 'host', 'feed-port', 'feed-host'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.closest('.field').classList.toggle('empty', !el.value.trim()); }
    });
  }
  document.addEventListener('input', syncRequired);
  document.addEventListener('change', syncRequired);
  syncRequired();
  $('tab-feed').addEventListener('click', function () { showPane('feed'); });
  $('view').addEventListener('click', function () { window.open('/view', '_blank'); });

  $('theme').addEventListener('click', function () {
    var t = root.getAttribute('data-theme');
    var dark = t === 'dark';
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
  });

  var mode = $('mode');
  function syncMode() {
    var caller = mode.value === 'caller';
    // the host only applies to a caller; a listener accepts from any sender, so the field is hidden
    $('hostfield').hidden = !caller;
    $('host').disabled = !caller;
    $('host').placeholder = caller ? 'e.g. 203.0.113.5' : '';
    $('hint').textContent = caller ? 'Connects out to the remote listener.' : 'Waits for a sender to connect.';
  }
  var pfTimer = null;
  function fetchPreflight(mode, host) {
    var port = $('port').value;
    var q = '/preflight?mode=' + encodeURIComponent(mode || 'listener') + '&port=' + encodeURIComponent(port) +
      (host ? '&host=' + encodeURIComponent(host) : '');
    return fetch(q, {cache: 'no-store'})
      .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (x) { return x.ok ? x.j.rows : null; }).catch(function () { return null; });
  }
  // a short notice on the live dashboard when this PC needs attention; dismissed until the issues change
  function rowsHtml(rows) {
    return rows.map(function (r) {
      return '<li class="pf-' + r.level + '"><span class="pf-tag">' + r.level + '</span><span><strong>' +
        esc2(r.name) + '</strong> ' + esc2(r.detail) + '</span>' +
        '</li>';
    }).join('');
  }
  function showNotice(rows, mode) {
    var attn = (rows || []).filter(function (r) { return r.level === 'WARN' || r.level === 'FAIL'; });
    var sig = attn.map(function (r) { return r.name + '|' + r.level; }).join(';');
    var dismissed = null;
    try { dismissed = sessionStorage.getItem('srtdebug-notice'); } catch (e) {}
    if (!attn.length || dismissed === sig) { $('notice').hidden = true; return; }
    $('notice').hidden = false;
    $('notice').setAttribute('data-sig', sig);
    var lead = mode === 'caller' ? 'before connecting out' : 'for incoming streams';
    $('notice-text').textContent = attn.length === 1
      ? 'One thing to check ' + lead + ': ' + attn[0].name + '.'
      : attn.length + ' things to check ' + lead + '.';
    $('notice-list').innerHTML = rowsHtml(rows);
    $('notice-list').hidden = true;
    $('notice-toggle').textContent = 'Details';
    $('notice-toggle').setAttribute('aria-expanded', 'false');
  }
  $('notice-toggle').addEventListener('click', function () {
    var open = $('notice-list').hidden;
    $('notice-list').hidden = !open;
    $('notice-toggle').textContent = open ? 'Hide' : 'Details';
    $('notice-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  $('notice-close').addEventListener('click', function () {
    try { sessionStorage.setItem('srtdebug-notice', $('notice').getAttribute('data-sig') || ''); } catch (e) {}
    $('notice').hidden = true;
  });
  function esc2(t) { return String(t).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function preflight() {
    var box = $('preflight');
    if (mode.value !== 'listener') { box.hidden = true; return; }
    box.hidden = false;
    var port = $('port').value;
    fetchPreflight('listener').then(function (rows) {
      if (!rows) { $('pf-sum').textContent = 'Could not check this PC.'; $('pf-list').innerHTML = ''; return; }
      var fails = rows.filter(function (r) { return r.level === 'FAIL'; }).length;
      var warns = rows.filter(function (r) { return r.level === 'WARN'; }).length;
      $('pf-sum').textContent = fails ? 'Not ready: ' + fails + ' problem' + (fails > 1 ? 's' : '') + ' on this PC'
        : warns ? 'Ready here, with ' + warns + ' thing' + (warns > 1 ? 's' : '') + ' to check on the network'
        : 'Ready on this PC; port forwarding is still to be tested from outside';
      $('pf-list').innerHTML = rowsHtml(rows);
    });
  }
  mode.addEventListener('change', function () { syncMode(); preflight(); });
  $('port').addEventListener('input', function () { clearTimeout(pfTimer); pfTimer = setTimeout(preflight, 400); });
  syncMode();
  preflight();
  // latency, for the stream being received: what we asked for, what the sender asked for, and the one that applies
  function latencyRows(g) {
    if (!g || (!g.ours && !g.asked)) { return []; }
    var rows = [];
    if (g.ours) { rows.push(['We asked for', g.ours]); }
    if (g.asked) { rows.push(['Sender asked for', g.asked]); }
    if (g.ours && g.asked) {
      var a = parseInt(g.ours, 10), b = parseInt(g.asked, 10);
      rows.push(['Applies (the higher)', Math.max(a, b) + ' ms' + (a === b ? '' : (a > b ? ' (ours)' : ' (theirs)'))]);
    }
    return rows;
  }

  function setFacts(items) {
    var box = $('facts');
    box.innerHTML = '';
    items.forEach(function (it) {
      var d = document.createElement('div');
      d.className = 'fact';
      d.innerHTML = '<div class="k"></div><div class="v"></div>';
      d.firstChild.textContent = it[0];
      d.lastChild.textContent = it[1];
      box.appendChild(d);
    });
  }

  function render(s) {
    var on = s.running;
    if (!window.monSeen) { window.monSeen = true; if (on) { showPane('control'); } }
    // a launch while this window is open asks it to come forward
    if (window.focusSeen === undefined) { window.focusSeen = s.focus_request; }
    else if (s.focus_request !== window.focusSeen) {
      window.focusSeen = s.focus_request;
      try { window.focus(); } catch (e) { }
      document.title = 'srtdebug is already open here';
      setTimeout(function () { document.title = 'srtdebug'; }, 4000);
    }
    if (s.monitor_presets) { renderPresets(s.monitor_presets); }
    $('dot').className = 'led' + (on ? ' on' : '');
    $('pill').className = 'pill' + (on ? ' on' : '');
    var left = on ? s.ends_in_s : null;
    var countdown = (left != null) ? ' · ' + Math.floor(left / 60) + ':' + (left % 60 < 10 ? '0' : '') + (left % 60) + ' left' : '';
    $('pilltext').textContent = on ? 'Running' + countdown : (s.has_session ? (s.auto_stopped ? 'Test complete' : 'Stopped') : 'Idle');
    if (on) {
      var c = s.config;
      var where = c.mode === 'caller' ? 'Calling ' + c.host + ':' + c.port : 'Listening on UDP ' + c.port;
      $('state').textContent = where;
      var left = s.ends_in_s;
      $('detail').textContent = 'Running for ' + Math.floor(s.uptime_s / 60) + ' min ' + (s.uptime_s % 60) + ' s'
        + (left != null ? ' · scheduled to end in ' + Math.floor(left / 60) + ' min ' + (left % 60) + ' s' : '');
      setFacts([
        ['Mode', c.mode],
        ['Latency', c.latency + ' ms'],
        ['Encryption', c.encrypted ? 'On' + (c.pbkeylen ? ' (AES-' + c.pbkeylen * 8 + ')' : '') : 'Off'],
        ['Analysis', c.payload ? 'On' : 'Off']
      ].concat(s.gop && s.gop.keyframe ? [['Keyframe interval', s.gop.keyframe.split('  (')[0]]] : [])
        .concat(s.gop && s.gop.gop ? [['GOP size', s.gop.gop]] : [])
        .concat(s.gop && s.gop.headers ? [['SPS/PPS before keyframes', s.gop.headers]] : [])
        .concat(s.gop && s.gop.spikes ? [['Round-trip spikes', s.gop.spikes]] : [])
        .concat(latencyRows(s.gop)));
    } else if (s.has_session) {
      $('state').textContent = s.auto_stopped ? 'Test complete' : 'Stopped';
      $('detail').textContent = s.auto_stopped
        ? 'The scheduled test of ' + s.duration_min + ' min has finished. Download all the reports below.'
        : (s.exit_code === 0 || s.exit_code === null)
          ? 'The last run finished.' : 'The last run exited with code ' + s.exit_code + '. See the log.';
      setFacts([]);
    } else {
      $('state').textContent = 'Not running';
      $('detail').textContent = 'Configure a monitor and press Start.';
      setFacts([]);
    }
    $('stop').disabled = !on;
    $('view').hidden = !on;
    window.srtRunning = on;
    syncTop();
    // a run that has just ended: offer the reports once (not on page load)
    if (!on && window.wasOn) { openDone(); }
    window.wasOn = on;
    if (!$('done-layer').hidden) { doneRender(s); }
    $('report').hidden = !s.report;
    $('report-pdf').hidden = !s.report;
    $('report-csv').hidden = !s.csv;
    $('report-zip').hidden = !s.report;
    $('logwrap').hidden = !(s.log && s.log.length);
    $('log').textContent = (s.log || []).join('\\n');
  }

  function doneRender(s) {
    var ready = !!s.report;
    $('done-title').textContent = s.auto_stopped ? 'Scheduled test complete' : 'Monitoring stopped';
    var waited = Date.now() - (window.doneSince || Date.now());
    if (ready) {
      $('done-status').textContent = 'Your reports are ready. Download them now, or press Esc to skip.';
    } else if (!s.csv && waited > 8000) {
      $('done-status').textContent = 'No data was received during this run, so there are no reports. '
        + 'The log is in the status panel.';
    } else {
      $('done-status').textContent = 'Preparing your reports… they will appear here. Press Esc to skip.';
    }
    $('done-zip').hidden = !ready;
    $('done-html').hidden = !ready;
    $('done-pdf').hidden = !ready;
    $('done-csv').hidden = !s.csv;
  }
  function openDone() {
    window.doneSince = Date.now();
    window.reportsSaved = false;
    $('done-layer').hidden = false;
    $('done-close').focus({preventScroll: true});
  }
  // closing without downloading a report takes the user back to the start screen; after a download they stay
  function closeDone() {
    $('done-layer').hidden = true;
    if (!window.reportsSaved) { showPane('home'); }
  }
  $('done-close').addEventListener('click', closeDone);
  $('done-layer').addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('a')) { window.reportsSaved = true; }
  });
  $('done-layer').addEventListener('click', function (e) { if (e.target === $('done-layer')) { closeDone(); } });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('done-layer').hidden) { closeDone(); }
  });

  function refresh() {
    fetch('/status', {cache: 'no-store'}).then(function (r) { return r.json(); }).then(render).catch(function () {});
  }

  $('cfg').addEventListener('submit', function (e) {
    e.preventDefault();
    $('err').textContent = '';
    var body = {mode: mode.value, port: $('port').value, host: $('host').value,
                latency: $('latency').value, streamid: $('streamid').value, payload: $('payload').checked, view: $('vid').checked,
                passphrase: $('passphrase').value, pbkeylen: $('pbkeylen').value, duration: $('duration').value};
    var pre = mode.value === 'listener' ? fetchPreflight('listener')
      : ($('host').value ? fetchPreflight('caller', $('host').value) : Promise.resolve(null));
    pre.then(function (rows) {
      return fetch('/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
        .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
        .then(function (x) {
          if (!x.ok) { $('err').textContent = x.j.error || 'Could not start.'; }
          else { $('passphrase').value = ''; showNotice(rows, mode.value); }
          refresh();
        });
    });
  });
  $('stop').addEventListener('click', function () {
    fetch('/stop', {method: 'POST'}).then(function () { refresh(); });
  });
  // Quit ends the program: a running monitor or feed is stopped first, so ask before quitting while one runs
  $('quit').addEventListener('click', function () {
    var busy = window.srtRunning || window.feedRunning;
    if (busy && !window.confirm('Quit srtdebug? The running monitor or encode will stop.')) { return; }
    fetch('/quit', {method: 'POST'}).catch(function () {}).then(function () {
      document.body.innerHTML = '<main style="max-width:520px;margin:12vh auto;padding:0 16px;font:16px system-ui,sans-serif;">' +
        '<h1 style="font-size:22px">srtdebug has stopped</h1>' +
        '<p>You can close this tab. To use srtdebug again, use Launch srtdebug.</p></main>';
    });
  });
  // while a monitor runs, the header button stops it instead of submitting the form
  $('start').addEventListener('click', function (e) {
    if (window.paneMode === 'feed') {
      e.preventDefault();
      $(window.feedRunning ? 'feed-stop' : 'feed-start').click();
      return;
    }
    if (window.srtRunning) { e.preventDefault(); $('stop').click(); }
  });

  var thrSpec = [];
  function thrFill(values) {
    var box = $('thr-fields'); box.innerHTML = '';
    thrSpec.forEach(function (t) {
      var f = document.createElement('div'); f.className = 'field';
      var lab = document.createElement('label'); lab.htmlFor = 'thr-' + t.key; lab.textContent = t.label + ' (' + t.unit + ')';
      var inp = document.createElement('input'); inp.type = 'number'; inp.id = 'thr-' + t.key; inp.name = t.key;
      inp.min = t.lo; inp.max = t.hi; inp.step = 'any'; inp.value = values[t.key];
      var hint = document.createElement('div'); hint.className = 'hint';
      hint.textContent = t.why + ' Default: ' + t.default + ' ' + t.unit + '.';
      f.appendChild(lab); f.appendChild(inp); f.appendChild(hint); box.appendChild(f);
    });
  }
  function thrLoad() {
    fetch('/thresholds', {cache: 'no-store'}).then(function (r) { return r.json(); }).then(function (j) {
      thrSpec = j.spec; thrFill(j.values);
    }).catch(function () {});
  }
  $('thr').addEventListener('submit', function (e) {
    e.preventDefault(); $('thr-err').textContent = '';
    var body = {};
    thrSpec.forEach(function (t) { body[t.key] = $('thr-' + t.key).value; });
    fetch('/thresholds', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({values: body})})
      .then(function (r) { return r.json().then(function (j) { return {ok: r.ok, j: j}; }); })
      .then(function (x) {
        if (!x.ok) { $('thr-err').textContent = (x.j.problems || [x.j.error || 'Could not save.']).join('. '); return; }
        thrFill(x.j.values); $('thr-note').textContent = 'Saved. Applies to the next monitor you start.';
      });
  });
  $('thr-reset').addEventListener('click', function () {
    $('thr-err').textContent = '';
    fetch('/thresholds', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({reset: true})})
      .then(function (r) { return r.json(); })
      .then(function (j) { thrFill(j.values); $('thr-note').textContent = 'Defaults restored.'; });
  });
  thrLoad();

  function setHelp(open) {
    $('pane-help').hidden = !open;
    $('help-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      // the panel sits at the top of the page; bring it into view when the page is scrolled down
      $('pane-help').scrollIntoView({behavior: 'smooth', block: 'start'});
      $('help-close').focus({preventScroll: true});
    }
  }
  $('help-btn').addEventListener('click', function () { setHelp($('pane-help').hidden); });
  $('help-close').addEventListener('click', function () { setHelp(false); });

  // saved presets for the Decode stream: connection settings only, chosen from a drop-down
  var mPresets = [], mPreset = '';
  var PRESET_KEYS = ['mode', 'port', 'host', 'latency', 'streamid'];
  function presetFields() {
    var caller = mode.value === 'caller';
    return {mode: mode.value, port: $('port').value.trim(), host: caller ? $('host').value.trim() : '',
            latency: $('latency').value.trim(), streamid: $('streamid').value.trim()};
  }
  function presetSummary(p) {
    var where = p.mode === 'caller' ? 'caller to ' + (p.host || '?') + ':' + (p.port || '') : 'listener on port ' + (p.port || '');
    return where + (p.latency ? '  ·  latency ' + p.latency + ' ms' : '') + (p.streamid ? '  ·  ' + p.streamid : '');
  }
  function presetSame(p) {
    var now = presetFields();
    return PRESET_KEYS.every(function (k) { return String(p[k] || '') === now[k]; });
  }
  function presetMessage(text) { $('preset-msg').textContent = text; }
  function presetState() {
    var p = mPresets.filter(function (x) { return x.name === mPreset; })[0];
    $('preset-state').textContent = p ? 'In use: ' + presetSummary(p) : '';
    $('preset-del').disabled = !p;
    $('preset-save').textContent = mPreset && $('preset-name').value.trim() === mPreset ? 'Update' : 'Save';
  }
  function renderPresets(list) {
    mPresets = list || [];
    var sel = $('preset-pick');
    sel.innerHTML = '';
    var none = document.createElement('option'); none.value = '';
    none.textContent = mPresets.length ? 'Choose a saved preset…' : 'None saved yet';
    sel.appendChild(none);
    mPresets.forEach(function (p) {
      var o = document.createElement('option'); o.value = p.name; o.textContent = p.name; sel.appendChild(o);
    });
    sel.value = mPresets.some(function (p) { return p.name === mPreset; }) ? mPreset : '';
    presetState();
  }
  function loadPreset(p) {
    mode.value = p.mode || 'listener';
    $('port').value = p.port || '';
    $('host').value = p.host || '';
    $('latency').value = p.latency || '';
    $('streamid').value = p.streamid || '';
    mPreset = p.name;
    $('preset-name').value = p.name;
    syncMode();
    $('port').dispatchEvent(new Event('input'));
    renderPresets(mPresets);
    presetMessage('Loaded "' + p.name + '". Change anything you need, then press Update to save the change, or type a new name and press Save to keep both.');
  }
  function presetDirty() {
    var p = mPresets.filter(function (x) { return x.name === mPreset; })[0];
    if (!p) { return; }
    presetMessage(presetSame(p) ? 'Loaded "' + mPreset + '", unchanged.' : 'Changed from the saved "' + mPreset + '". Press Update to save the change.');
    presetState();
  }
  function presetRequest(body, done) {
    fetch('/monitor/preset', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.ok && body.action === 'delete') { mPreset = ''; $('preset-name').value = ''; }
        else if (j.ok) { mPreset = body.name; $('preset-name').value = body.name; }
        renderPresets(j.presets);
        presetMessage(j.ok ? done : (j.error || 'Could not save the preset.'));
      });
  }
  $('preset-save').addEventListener('click', function () {
    var name = $('preset-name').value.trim() || mPreset;
    if (!name) { presetMessage('Type a name for these settings, then press Save.'); $('preset-name').focus(); return; }
    presetRequest({action: 'save', name: name, fields: presetFields()}, 'Saved "' + name + '".');
  });
  $('preset-new').addEventListener('click', function () {
    mPreset = ''; $('preset-name').value = '';
    renderPresets(mPresets);
    presetMessage('New preset. Set the connection, type a name and press Save.');
  });
  $('preset-pick').addEventListener('change', function () {
    var name = this.value;
    var p = mPresets.filter(function (x) { return x.name === name; })[0];
    if (p) { loadPreset(p); return; }
    mPreset = ''; $('preset-name').value = '';
    renderPresets(mPresets);
  });
  $('preset-del').addEventListener('click', function () {
    if (!mPreset || !window.confirm('Delete the saved preset "' + mPreset + '"?')) { return; }
    presetRequest({action: 'delete', name: mPreset}, 'Deleted "' + mPreset + '".');
  });
  $('preset-name').addEventListener('input', presetState);
  $('cfg').addEventListener('input', presetDirty);
  $('cfg').addEventListener('change', presetDirty);

  refresh();
  setInterval(refresh, 2000);
})();
</script>
<script>
(function () {
  function $(id) { return document.getElementById(id); }
  // while a feed is sending, the other panes show that it is still running, and how to get back to it
  function updateSendStrip() {
    var s = window.feedState || {}, strip = $('send-strip');
    var show = !!s.running && window.paneMode !== 'feed';
    strip.hidden = !show;
    if (show) {
      var where = s.host_port || s.target;
      $('send-text').textContent = 'Sending' + (where ? ' to ' + where : '') + ' (' + (s.elapsed_s || 0) + ' s). ' +
        'The feed keeps running while you look at other panes.';
    }
  }
  window.updateSendStrip = updateSendStrip;
  $('send-go').addEventListener('click', function () { if (window.showPane) { window.showPane('feed'); } });
  function render(s) {
    var feedNow = !!s.running;
    // the feed has just stopped (by the user or on its own): offer its log, then go back to the start screen
    if (window.feedWasOn && !feedNow) { openFeedDone(); }
    window.feedWasOn = feedNow;
    if (!window.feedSeen) { window.feedSeen = true; if (feedNow && window.showPane) { window.showPane('feed'); } }
    window.feedRunning = feedNow;
    window.feedState = s;
    if (window.updateDestSummary) { window.updateDestSummary(); }
    updateSendStrip();
    if (window.syncTop) { window.syncTop(); }
    if (s.destinations) { renderDestinations(s.destinations); }
    $('feed-dir').textContent = s.samples_dir;
    var sel = $('feed-clip'), cur = sel.value;
    sel.innerHTML = '';
    var labels = s.clip_labels || {};
    (s.clips || []).forEach(function (c) {
      var o = document.createElement('option'); o.value = c; o.textContent = labels[c] || c; sel.appendChild(o);
    });
    if (cur) sel.value = cur;
    $('feed-cliphint').textContent = (s.clips || []).length ? '' : 'No clips yet. Press Make sample clips.';
    var mk = (s.make || {}).state;
    $('feed-make').disabled = mk === 'making';
    var haveClips = (s.clips || []).length > 0;
    $('feed-make').dataset.have = haveClips ? '1' : '';
    $('feed-make').textContent = mk === 'making' ? 'Making clips...' : haveClips ? 'Regenerate sample clips' : 'Make sample clips';
    $('feed-make').title = haveClips
      ? 'Makes the test clips again in the folder you choose. Use this if a clip is missing, corrupt or was deleted.'
      : 'Writes a set of test clips, one per video format, into a folder you choose. Then pick one in Sample clip to send.';
    $('samples-size').textContent = s.size_mb || 200;
    if (mk === 'error') { $('feed-err').textContent = 'Could not make the clips: ' + s.make.error; }
    $('feed-start').disabled = !!s.running;
    $('feed-stop').disabled = !s.running;
    $('feed-clearview').disabled = !!s.running || !(s.log_tail || []).length;
    if (window.feedWasRunning && !s.running) { stoppedDialog(); }
    window.feedWasRunning = !!s.running;
    if (s.rate_max) {
      $('feed-rate').placeholder = s.rate_max;
      $('feed-rate-hint').textContent = 'Leave it empty to use ' + s.rate_max + ' Mb/s. Above ' + s.rate_max + ' you will be asked to confirm.';
    }
    if (s.rate_note) { $('feed-cpu-note').textContent = s.rate_note; $('feed-cpu-note').hidden = false; }
    if ('network_wifi' in s) {
      $('feed-network-msg').textContent = s.network_wifi === true ? 'Detected: Wi-Fi. The Wi-Fi latency rule applies.'
        : s.network_wifi === false ? 'Detected: not Wi-Fi (wired or another link). Tick the box if this PC is on Wi-Fi anyway.'
        : 'Could not tell what this PC is connected through. Tick the box if it is on Wi-Fi.';
    }
    var wifiEl = $('feed-wifi');
    wifiEl.hidden = !(s.running && s.wifi && (s.latency_auto || s.wifi_short));
    wifiEl.textContent = s.wifi_forced
      ? 'This PC is on Wi-Fi, so the latency is 5000 ms. You asked for less, but Wi-Fi drops packets in short bursts and a ' +
        'shorter latency freezes the picture. Tick "Allow a shorter latency on Wi-Fi" under Custom settings to use it anyway.'
      : s.wifi_short
      ? 'This PC is on Wi-Fi and the latency is ' + s.latency_set + ' ms, a shorter value you allowed. Wi-Fi drops packets in ' +
        'short bursts, and a short latency shows those bursts as frozen pictures. Expect freezes; 5000 ms is more reliable.'
      : 'This PC is on Wi-Fi, so the latency is set to ' + (s.latency_set || 5000) + ' ms automatically. ' +
        'Wi-Fi drops packets in short bursts, and SRT needs time to resend them. A shorter latency shows those bursts as ' +
        'frozen pictures. A wired connection is more reliable.';
    var st = $('feed-status'), where = s.target || s.host_port || '', phase = 'idle', title = 'Not sending';
    if (s.notice && !s.connected) { phase = 'connecting'; title = 'Reconnecting'; st.textContent = s.notice; }
    else if (s.running) {
      if (s.dropping) { phase = 'dropped'; title = 'Connected, then closed each time'; st.textContent = 'The other side accepts the connection and closes it within moments. It has done this ' + s.closes + ' times, so it is rejecting the stream after the handshake. Check the stream ID, passphrase and ingest details with the provider.'; }
      else if (s.refused && !s.connected) { phase = 'refused'; title = 'Refused by the other side'; st.textContent = s.refused + ' Still trying (' + s.elapsed_s + ' s).'; }
      else if (s.connected) { phase = 'connected'; title = '\u2714 Connected successfully'; st.textContent = 'Connected to ' + where + ' and sending ' + s.clip + ' for ' + s.elapsed_s + ' s. The handshake completed.'; }
      else if (s.mode === 'listener') { phase = 'waiting'; title = 'Waiting for a receiver'; st.textContent = 'Listening for a receiver to connect to ' + where + ' (' + s.elapsed_s + ' s).'; }
      else { phase = 'connecting'; title = 'Connecting'; st.textContent = 'Trying to reach ' + where + ' (' + s.elapsed_s + ' s). Nothing has answered yet.'; }
    } else if (s.error) { phase = 'stopped'; title = 'Stopped'; st.textContent = s.error; }
    else { st.textContent = 'Set where the feed goes below, then press Start feed.'; }
    var stt = s.stats, fb = $('feed-facts');
    var avms = stt && s.running ? stt.av_ms : null;
    $('feed-stats').textContent = avms == null ? '' : 'A/V offset on the way out: ' + (avms > 0 ? '+' : '') +
      Math.round(avms) + ' ms (' + (avms > 1 ? 'audio leads' : avms < -1 ? 'audio lags' : 'in step') + ')';
    var al = $('feed-alerts'), alerts = (stt && s.running && stt.alerts) || [];
    al.innerHTML = '';
    al.hidden = !alerts.length;
    if (alerts.length) {
      var hd = document.createElement('div'); hd.className = 'head';
      hd.textContent = alerts.length === 1 ? 'Something needs a look' : 'Things that need a look';
      al.appendChild(hd);
      alerts.forEach(function (a) { var q = document.createElement('p'); q.textContent = a; al.appendChild(q); });
    }
    var pe = $('feed-peer');
    if (s.running && s.peer) {
      var pr = s.peer, where2 = [pr.city, pr.region, pr.country].filter(Boolean).join(', ');
      var parts = ['Receiver: ' + (pr.org || pr.error || 'owner not listed')];
      if (where2) { parts.push(where2); }
      parts.push(pr.ip + (pr.rdns ? ' (' + pr.rdns + ')' : ''));
      pe.textContent = parts.join('  \u00b7  ');
      pe.hidden = false;
    } else { pe.textContent = ''; pe.hidden = true; }
    fb.innerHTML = '';
    fb.hidden = !(s.running && s.connected && stt);
    if (!fb.hidden) {
      var tiles = [
        ['Send rate', stt.rate_mbps.toFixed(2) + ' Mb/s'],
        ['Latency set', s.latency_set !== null && s.latency_set !== undefined ? s.latency_set + ' ms' : 'Not set'],
        ['Receive delay', stt.latency_ms ? stt.latency_ms + ' ms' : 'Waiting'],
        ['Round trip (wire)', stt.rtt_ms ? stt.rtt_ms + ' ms' : 'Waiting'],
        ['Reported lost', String(stt.lost)],
        ['Resent on request', String(stt.retransmitted)],
        ['Encryption', s.encrypted ? 'On (passphrase)' : 'Off']
      ];
      tiles.unshift(['Encoding', s.encode_label || 'Send as it is (no re-encode)']);
      if (s.gop) { tiles.push(['Keyframe interval', s.gop]); }
      var vi = s.video;
      if (vi) {
        tiles.push(['Video codec', vi.codec]);
        tiles.push(['Resolution', vi.resolution]);
        tiles.push(['Frame rate', vi.fps + ' frames/s']);
        tiles.push(['Aspect ratio', vi.aspect]);
        tiles.push(['Scan', vi.scan]);
      }
      var au = s.audio;
      if (au && !au.codec) {
        tiles.push(['Audio', 'None in this clip']);
      } else if (au) {
        tiles.push(['Audio codec', au.codec]);
        tiles.push(['Sample rate', (au.sample_rate / 1000).toFixed(1).replace(/\\.0$/, '') + ' kHz']);
        tiles.push(['Channels', au.channels + (au.layout ? ' (' + au.layout + ')' : '')]);
        tiles.push(['Audio bitrate', au.bitrate_kbps ? au.bitrate_kbps + ' kb/s' : 'Not stated']);
      }
      tiles.forEach(function (t) {
        var d = document.createElement('div'), k = document.createElement('div'), v = document.createElement('div');
        d.className = 'fact'; k.className = 'k'; v.className = 'v';
        k.textContent = t[0]; v.textContent = t[1];
        d.appendChild(k); d.appendChild(v); fb.appendChild(d);
      });
    }
    var evs = s.events || [], evwrap = $('feed-events-wrap');
    evwrap.hidden = !evs.length;
    var ol = $('feed-events'); ol.innerHTML = '';
    evs.forEach(function (e) {
      var li = document.createElement('li'), t = document.createTextNode(e[0]);
      li.appendChild(t);
      if (e[1] > 1) { var n = document.createElement('span'); n.className = 'n'; n.textContent = '\u00d7' + e[1]; li.appendChild(n); }
      ol.appendChild(li);
    });
    var tl = s.totals ? 'Totals: sent ' + s.totals.sent_mb + ' MB, received ' + s.totals.received_mb + ' MB, lost ' + s.totals.lost_kb + ' kB.' : '';
    var tm = function (n) { return n + (n === 1 ? ' time' : ' times'); };
    $('feed-counts').textContent = (s.connects !== undefined ? 'Connected ' + tm(s.connects) + ', closed ' + tm(s.closes) + '. ' : '') + tl;
    if (s.loglevel && document.activeElement !== $('feed-level')) { $('feed-level').value = s.loglevel; }
    $('feed-phase').textContent = title;
    $('feed-state').setAttribute('data-phase', phase);
    var lines = s.raw_lines || s.log_tail || [];
    var follow = $('feed-follow').checked;
    var rawEl = $('feed-log'), rawAt = follow || window.logStick || nearBottom(rawEl);
    rawEl.textContent = lines.length ? lines.join('\\n') : 'No messages yet.';
    if (rawAt) { rawEl.scrollTop = rawEl.scrollHeight; }
    var rd = $('feed-readable'), rdAt = follow || window.logStick || nearBottom(rd); rd.innerHTML = '';
    (s.readable || []).forEach(function (r) {
      var li = document.createElement('li'), w = document.createElement('span'), t = document.createElement('span');
      w.className = 'when'; w.textContent = r.start ? (r.end && r.end !== r.start ? r.start + ' to ' + r.end : r.start) : '';
      t.className = r.level || 'muted'; t.textContent = r.text;
      if (r.count > 1) { var n = document.createElement('span'); n.className = 'n'; n.textContent = '\u00d7' + r.count; t.appendChild(n); }
      li.appendChild(w); li.appendChild(t); rd.appendChild(li);
    });
    if (!(s.readable || []).length) { rd.innerHTML = '<li class="muted">Plain-language messages appear here once a feed starts.</li>'; }
    if (rdAt) { rd.scrollTop = rd.scrollHeight; }
    window.logStick = false;
    $('feed-loginfo').textContent = s.log_lines !== undefined ? (s.log_lines || 0) + ' log lines' : '';
  }
  function stoppedDialog() { $('stopped-back').hidden = false; }
  function stoppedDone() { $('stopped-back').hidden = true; showFeedStep(1); }
  $('stopped-done').addEventListener('click', stoppedDone);
  $('stopped-back').addEventListener('click', function (e) { if (e.target === this) { stoppedDone(); } });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('stopped-back').hidden) { stoppedDone(); }
  });
  function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }
  function refresh() {
    fetch('/feed/status').then(function (r) { return r.json(); }).then(render).catch(function () {});
  }
  function showFeedStep(n) {
    n = String(n);
    if (window.updateDestSummary) { window.updateDestSummary(); }
    document.querySelectorAll('#feedcard .feed-step').forEach(function (el) { el.hidden = el.getAttribute('data-step') !== n; });
    [1, 2].forEach(function (i) { $('step-btn-' + i).setAttribute('aria-selected', String(i) === n ? 'true' : 'false'); });
  }
  document.querySelectorAll('#feedcard [data-go]').forEach(function (el) {
    el.addEventListener('click', function () { showFeedStep(el.getAttribute('data-go')); });
  });
  function showLogView(name) {
    var raw = name === 'raw';
    $('feed-log').hidden = !raw; $('feed-readable').hidden = raw;
    $('view-readable').setAttribute('aria-selected', raw ? 'false' : 'true');
    $('view-raw').setAttribute('aria-selected', raw ? 'true' : 'false');
  }
  $('view-readable').addEventListener('click', function () { showLogView('readable'); });
  $('view-raw').addEventListener('click', function () { showLogView('raw'); });
  $('feed-level').addEventListener('change', function () {
    var lvl = this.value;
    fetch('/feed/loglevel', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({level: lvl})})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) { $('feed-err').textContent = j.error || 'Could not change the level.'; }
        render(j);
      });
  });
  // the destination URL: checked as it is typed; with a URL, port and host are optional
  function srtUrlProblem(u) {
    var scheme = 'srt://';
    if (u.slice(0, scheme.length).toLowerCase() !== scheme) { return 'The address must start with srt:// (for example srt://203.0.113.5:9000).'; }
    var q = u.indexOf('?'), auth = q < 0 ? u.slice(scheme.length) : u.slice(scheme.length, q);
    var query = q < 0 ? '' : u.slice(q + 1);
    var c = auth.lastIndexOf(':');
    if (c < 0) { return 'The address must look like srt://host:port, then ?options if needed. Check the host and port.'; }
    var host = auth.slice(0, c), port = auth.slice(c + 1);
    if (host.indexOf(' ') >= 0 || host.indexOf('/') >= 0) { return 'The host must not contain spaces or slashes.'; }
    if (!port || port.length > 5 || !(port.split('').every(function (ch) { return ch >= '0' && ch <= '9'; }))) {
      return 'The address must look like srt://host:port, then ?options if needed. Check the host and port.';
    }
    if (+port < 1 || +port > 65535) { return 'The port must be between 1 and 65535.'; }
    var mode = 'caller', keys = ['mode', 'streamid', 'latency', 'passphrase', 'pbkeylen'];
    var parts = query.split('&').filter(function (x) { return x; });
    for (var i = 0; i < parts.length; i++) {
      var eq = parts[i].indexOf('=');
      var key = (eq < 0 ? parts[i] : parts[i].slice(0, eq)).toLowerCase();
      var val = eq < 0 ? '' : parts[i].slice(eq + 1);
      if (eq < 0) { return "'" + key + "' needs a value after an equals sign, for example " + key + '=...'; }
      if (keys.indexOf(key) < 0) { return "'" + key + "' is not a setting this tool knows. Check the spelling, or remove it."; }
      if (key === 'mode') {
        if (['caller', 'listener', 'rendezvous'].indexOf(val.toLowerCase()) < 0) { return 'mode must be caller, listener or rendezvous.'; }
        mode = val.toLowerCase();
      }
      if (key === 'latency' && (val === '' || !val.split('').every(function (ch) { return ch >= '0' && ch <= '9'; }))) {
        return 'latency must be a whole number of milliseconds.';
      }
      if (key === 'passphrase' && (val.length < 10 || val.length > 80)) { return 'The passphrase in the address must be 10 to 80 characters.'; }
      if (key === 'pbkeylen' && ['16', '24', '32'].indexOf(val) < 0) { return 'pbkeylen must be 16, 24 or 32.'; }
    }
    if (mode === 'caller' && !host) { return 'A caller needs a host, for example srt://203.0.113.5:9000.'; }
    return '';
  }
  function syncUrl() {
    var u = $('feed-url').value.trim(), msg = $('feed-url-msg'), field = $('feed-url').closest('.field');
    ['feed-port', 'feed-host'].forEach(function (id) { $(id).closest('.field').classList.toggle('optional', !!u); });
    field.classList.toggle('invalid', false);
    if (!u) { msg.className = 'hint'; msg.textContent = 'Leave empty to use the mode, port and host below.'; return; }
    var problem = srtUrlProblem(u);
    field.classList.toggle('invalid', !!problem);
    msg.className = 'hint ' + (problem ? 'bad' : 'good');
    msg.textContent = problem || 'Looks right. The port and host below are not needed.';
  }
  $('feed-url').addEventListener('input', syncUrl);
  syncUrl();
  var destList = [], activeDest = '';
  var DEST_FIELDS = ['url', 'mode', 'port', 'host', 'streamid', 'latency'];
  function destFields() {
    var b = feedBody(), out = {};
    DEST_FIELDS.forEach(function (k) { out[k] = String(b[k === 'url' ? 'url' : k] || '').trim(); });
    return out;
  }
  function destSummary(d) {
    if (d.url) { return d.url; }
    return (d.mode || 'caller') + '  ' + (d.host || '') + ':' + (d.port || '') + (d.latency ? '  ·  latency ' + d.latency + ' ms' : '');
  }
  function destSame(d) {
    var now = destFields();
    return DEST_FIELDS.every(function (k) { return String(d[k] || '') === now[k]; });
  }
  function destMessage(text) { $('dest-msg').textContent = text; }
  // saved destinations are a drop-down, so a long list does not fill the pane
  function renderDestinations(list) {
    destList = list || [];
    var sel = $('dest-pick');
    sel.innerHTML = '';
    var none = document.createElement('option'); none.value = '';
    none.textContent = destList.length ? 'Choose a saved destination…' : 'None saved yet';
    sel.appendChild(none);
    destList.forEach(function (d) {
      var o = document.createElement('option'); o.value = d.name; o.textContent = d.name; sel.appendChild(o);
    });
    sel.value = destList.some(function (d) { return d.name === activeDest; }) ? activeDest : '';
    var cur = destList.filter(function (x) { return x.name === activeDest; })[0];
    $('dest-state').textContent = cur ? 'In use: ' + destSummary(cur) : '';
    $('dest-del').disabled = !cur;
    $('dest-save').textContent = activeDest && $('dest-name').value.trim() === activeDest ? 'Update' : 'Save';
  }
  function loadDest(d) {
    $('feed-url').value = d.url || '';
    $('feed-mode').value = d.mode || 'caller';
    $('feed-port').value = d.port || '';
    $('feed-host').value = d.host || '';
    $('feed-streamid').value = d.streamid || '';
    $('feed-latency').value = d.latency || '';
    $('feed-passphrase').value = '';
    activeDest = d.name;
    $('dest-name').value = d.name;
    renderDestinations(destList);
    destMessage('Loaded "' + d.name + '". Change anything you need, then press Update to save the change, or type a new name and press Save to keep both.');
    refreshCmd();
  }
  function destDirty() {
    if (!activeDest) { return; }
    var d = destList.filter(function (x) { return x.name === activeDest; })[0];
    if (!d) { return; }
    if (destSame(d)) { destMessage('Loaded "' + activeDest + '", unchanged.'); }
    else { destMessage('Changed from the saved "' + activeDest + '". Press Update to save the change.'); }
    $('dest-save').textContent = $('dest-name').value.trim() === activeDest || !$('dest-name').value.trim() ? 'Update' : 'Save';
  }
  function destRequest(body, done) {
    fetch('/feed/destination', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        renderDestinations(j.destinations);
        destMessage(j.ok ? done : (j.error || 'Could not save the destination.'));
      });
  }
  $('dest-save').addEventListener('click', function () {
    var name = $('dest-name').value.trim() || activeDest;
    if (!name) { destMessage('Type a name for these fields, then press Save.'); $('dest-name').focus(); return; }
    var f = destFields();
    destRequest({action: 'save', name: name, fields: f}, 'Saved "' + name + '".');
    activeDest = name;
    $('dest-name').value = name;
    renderDestinations(destList);
  });
  $('dest-new').addEventListener('click', function () {
    activeDest = '';
    $('dest-name').value = '';
    $('feed-url').value = ''; $('feed-mode').value = 'caller'; $('feed-port').value = '9100';
    $('feed-host').value = ''; $('feed-streamid').value = ''; $('feed-latency').value = '';
    $('feed-passphrase').value = '';
    syncUrl();
    renderDestinations(destList);
    destMessage('New destination. Fill in the fields, type a name and press Save.');
  });
  $('dest-name').addEventListener('input', function () {
    $('dest-save').textContent = activeDest && $('dest-name').value.trim() === activeDest ? 'Update' : 'Save';
  });
  $('dest-pick').addEventListener('change', function () {
    var name = this.value;
    var d = destList.filter(function (x) { return x.name === name; })[0];
    if (d) { loadDest(d); return; }
    activeDest = ''; $('dest-name').value = '';
    renderDestinations(destList);
    destMessage('Choose a saved destination, or fill in the fields and press Save.');
  });
  $('dest-del').addEventListener('click', function () {
    var name = activeDest;
    if (!name || !window.confirm('Delete the saved destination "' + name + '"?')) { return; }
    activeDest = ''; $('dest-name').value = '';
    destRequest({action: 'delete', name: name}, 'Deleted "' + name + '".');
  });
  $('pane-feed').addEventListener('input', destDirty);
  // the destination, in words, at the top of the Send step: where the feed goes and how it connects
  function destSummaryHtml() {
    var st = window.feedState || {};
    if (st.running && st.host_port) {
      var live = st.connected ? 'Connected to the receiver' :
        st.mode === 'listener' ? 'Waiting for a receiver to connect' : 'Trying to reach the receiver';
      return '<b>Sending to ' + st.host_port + '</b> (' + (st.mode || 'caller') + ')<br><span class="muted">' + live +
        (st.encrypted ? '. Encrypted.' : '.') + '</span>';
    }
    var u = $('feed-url').value.trim();
    var host = $('feed-host').value.trim(), port = $('feed-port').value.trim();
    var mode = $('feed-mode').value, sid = $('feed-streamid').value.trim();
    var enc = $('feed-passphrase').value ? ('Encrypted, AES-' + ({'24': '192', '32': '256'}[$('feed-pbkeylen').value] || '128')) : 'Not encrypted';
    var where = u || (host && port ? 'srt://' + host + ':' + port : '');
    if (!where) {
      return '<span class="muted">No destination yet. Go back to step 1 and enter where the feed goes.</span>';
    }
    return '<b>Destination: ' + where.replace(/</g, '&lt;') + '</b> (' + (mode === 'listener' ? 'listener, waits for the receiver' : 'caller, connects to the receiver') +
      ')<br><span class="muted">' + (sid ? 'Stream ID ' + sid.replace(/</g, '&lt;') + '. ' : '') + enc + '.</span>';
  }
  function updateDestSummary() { $('dest-summary').innerHTML = destSummaryHtml(); }
  window.updateDestSummary = updateDestSummary;
  ['feed-host', 'feed-port', 'feed-url', 'feed-mode', 'feed-streamid', 'feed-passphrase', 'feed-pbkeylen'].forEach(function (id) {
    $(id).addEventListener('input', updateDestSummary);
    $(id).addEventListener('change', updateDestSummary);
  });
  function feedBody() {
    // with a destination URL, the host and port fields are not used, so they are not sent
    var u = $('feed-url').value.trim();
    return {
      clip: $('feed-clip').value, source: $('feed-source').value.trim(),
      url: u, mode: $('feed-mode').value,
      port: u ? '' : $('feed-port').value, host: u ? '' : $('feed-host').value.trim(), loop: $('feed-loop').checked,
      streamid: $('feed-streamid').value.trim(), latency: $('feed-latency').value,
      passphrase: $('feed-passphrase').value,
      encode: $('feed-encode').value, rate: $('feed-rate').value.trim(), clock: $('feed-clock').checked,
      pbkeylen: $('feed-pbkeylen').value, allow_low_wifi: $('feed-allow-low').checked, on_wifi: $('feed-on-wifi').checked
    };
  }
  var cmdTimer = null;
  function refreshCmd() {
    clearTimeout(cmdTimer);
    cmdTimer = setTimeout(function () {
      fetch('/feed/command', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(feedBody())})
        .then(function (r) { return r.json(); })
        .then(function (j) {
          $('feed-cmd').textContent = j.ok ? j.command : (j.error || 'Fill in the fields to see the command.');
          $('feed-cmd-id').textContent = j.ok && j.stream_id ? 'Stream ID the other side receives: ' + j.stream_id : '';
        });
    }, 250);
  }
  $('pane-feed').addEventListener('input', refreshCmd);
  $('pane-feed').addEventListener('change', refreshCmd);
  $('pane-feed').addEventListener('click', refreshCmd);
  $('feed-cmd-copy').addEventListener('click', function () {
    var text = $('feed-cmd').textContent;
    if (navigator.clipboard) { navigator.clipboard.writeText(text); }
    $('feed-cmd-copy').textContent = 'Copied';
    setTimeout(function () { $('feed-cmd-copy').textContent = 'Copy command'; }, 1500);
  });
  refreshCmd();
  $('feed-start').addEventListener('click', function () {
    $('feed-err').textContent = '';
    var u0 = $('feed-url').value.trim();
    if (u0 && srtUrlProblem(u0)) {
      $('feed-err').textContent = srtUrlProblem(u0);
      $('feed-err').scrollIntoView({block: 'nearest', behavior: 'smooth'});
      return;
    }
    startFeedRequest(feedBody());
  });
  // starts the feed. Above this computer's limit the user is asked first, and can still go ahead
  function startFeedRequest(body) {
    fetch('/feed/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.needs_confirm && !body.allow_over_cpu) {
          if (!window.confirm(j.error)) { return; }
          body.allow_over_cpu = true;
          startFeedRequest(body);
          return;
        }
        if (!j.ok) {
          $('feed-err').textContent = j.error || 'Could not start the feed.';
          $('feed-err').scrollIntoView({block: 'nearest', behavior: 'smooth'});
        } else {
          $('feed-passphrase').value = '';
          window.logStick = true;
          showFeedStep(2);
          $('feedcard').scrollIntoView({behavior: 'smooth', block: 'start'});
        }
        render(j);
      });
  }
  $('feed-clearview').addEventListener('click', function () {
    fetch('/feed/clearview', {method: 'POST'}).then(function (r) { return r.json(); }).then(function (j) {
      if (!j.ok) { $('feed-err').textContent = j.error; }
      render(j);
    });
  });
  function openFeedDone() {
    window.feedLogSaved = false;
    $('feed-done-layer').hidden = false;
    $('feed-done-close').focus({preventScroll: true});
  }
  function closeFeedDone() {
    $('feed-done-layer').hidden = true;
    if (!window.feedLogSaved && window.showPane) { window.showPane('home'); }
  }
  $('feed-done-close').addEventListener('click', closeFeedDone);
  $('feed-done-layer').addEventListener('click', function (e) {
    if (e.target === $('feed-done-layer')) { closeFeedDone(); }
    else if (e.target.closest && e.target.closest('a')) { window.feedLogSaved = true; }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('feed-done-layer').hidden) { closeFeedDone(); }
  });
  $('feed-stop').addEventListener('click', function () {
    fetch('/feed/stop', {method: 'POST'}).then(function (r) { return r.json(); }).then(render);
  });
  function samplesClose() { $('samples-back').hidden = true; }
  function samplesOpen() {
    var have = $('feed-make').dataset.have === '1';
    $('samples-title').textContent = have ? 'Regenerate sample clips' : 'Make sample clips';
    $('samples-go').textContent = have ? 'Regenerate clips' : 'Make clips';
    $('samples-dir').value = $('feed-dir').textContent;
    $('samples-err').textContent = '';
    $('samples-back').hidden = false;
    $('samples-dir').focus();
  }
  $('feed-make').addEventListener('click', samplesOpen);
  $('samples-cancel').addEventListener('click', samplesClose);
  $('samples-back').addEventListener('click', function (e) { if (e.target === this) samplesClose(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !$('samples-back').hidden) samplesClose(); });
  (function () {
    var box = $('feed-drop'), msg = $('feed-drop-msg');
    function setMsg(text, bad) { msg.textContent = text; msg.className = 'hint' + (bad ? ' bad' : ''); }
    ['dragenter', 'dragover'].forEach(function (ev) {
      box.addEventListener(ev, function (e) { e.preventDefault(); box.classList.add('dropping'); });
    });
    ['dragleave', 'dragend'].forEach(function (ev) {
      box.addEventListener(ev, function () { box.classList.remove('dropping'); });
    });
    box.addEventListener('drop', function (e) {
      e.preventDefault(); box.classList.remove('dropping');
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) { return; }
      var okType = /\\.(mp4|m4v|mov|mkv|ts|m2ts|mts|mxf|avi)$/i.test(f.name);
      if (!okType) {
        $('feed-source').value = '';
        var no = '"' + f.name + '" is not a video file this tool can send. Use MP4, M4V, MOV, MKV, TS, M2TS, MTS, MXF or AVI.';
        setMsg(no, true); showFormat([], no); return;
      }
      if (f.size > 4 * 1024 * 1024 * 1024) { setMsg('That file is over 4 GB. Use a shorter or smaller file.', true); return; }
      setMsg('Checking ' + f.name + '... this can take a moment for a large file.', false);
      fetch('/feed/upload?name=' + encodeURIComponent(f.name), {method: 'POST', body: f})
        .then(function (r) {
          return r.json().catch(function () { return {ok: false, error: 'The file was not accepted (the panel replied with HTTP ' + r.status + ').'}; });
        })
        .then(function (j) {
          if (j.ok) {
            $('feed-source').value = j.path;
            var notes = applySourceGop(j.warn || '');
            setMsg('Compatible: ' + f.name + '. It will be sent instead of the clip.' + (notes.length ? ' ' + notes[0] : ''), false);
            showFormat(j.lines, '');
          } else {
            $('feed-source').value = '';
            setMsg(j.error || 'This file cannot be sent.', true);
            showFormat([], j.error || 'This file cannot be sent.');
          }
        })
        .catch(function () {
          var t = 'The file could not be uploaded. The panel may have stopped, or the file is too large. Restart the panel and try again.';
          $('feed-source').value = ''; setMsg(t, true); showFormat([], t);
        });
    });
    $('feed-source').addEventListener('input', function () { setMsg('Drag a video file onto this box, or type its path.', false); });
  })();
  $('samples-browse').addEventListener('click', function () {
    $('samples-err').textContent = '';
    fetch('/feed/folder', {method: 'POST'}).then(function (r) { return r.json(); }).then(function (j) {
      if (j.dir) { $('samples-dir').value = j.dir; }
      else if (j.error) { $('samples-err').textContent = j.error; }
    });
  });
  $('samples-go').addEventListener('click', function () {
    $('samples-err').textContent = '';
    fetch('/feed/samples', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dir: $('samples-dir').value})})
      .then(function (r) { return r.json(); }).then(function (j) {
        if (!j.ok) { $('samples-err').textContent = j.error || 'Could not make the clips.'; return; }
        samplesClose(); render(j);
      });
  });
  function showFormat(lines, error) {
    var box = $('feed-format');
    box.hidden = false;
    box.textContent = error ? error : (lines || []).join('\\n');
  }
  function applySourceGop(warn) {
    // a source whose keyframes are not one second apart is re-encoded: say so now, and keep the box honest
    var copy = $('feed-encode').querySelector('option[value="copy"]');
    copy.disabled = !!warn;
    if (warn) { $('feed-encode').value = 'h264'; }
    syncPrompt();
    return warn ? ['Will be re-encoded: ' + warn] : [];
  }
  function clipInfo() {
    var clip = $('feed-clip').value;
    if (!clip) { showFormat([], 'No clip chosen yet. Make the sample clips first.'); return; }
    showFormat([], 'Reading the clip...');
    fetch('/feed/clipinfo', {method: 'POST', headers: {'Content-Type': 'application/json'},
                             body: JSON.stringify({clip: clip})})
      .then(function (r) { return r.json(); })
      .then(function (j) { showFormat(applySourceGop(j.warn || '').concat(j.lines || []).concat(j.av_warn ? ['Warning: ' + j.av_warn] : []), j.ok ? '' : j.error); })
      .catch(function () { showFormat([], 'Could not read the clip.'); });
  }
  $('feed-clip').addEventListener('change', clipInfo);
  $('feed-clock').addEventListener('change', function () {
    if (this.checked) { $('feed-encode').value = 'h264'; }
    syncPrompt();
  });
  function syncPrompt() { $('custom-prompt').hidden = $('feed-encode').value !== 'h264' || $('custom-details').open; }
  $('feed-encode').addEventListener('change', syncPrompt);
  $('custom-details').addEventListener('toggle', syncPrompt);
  $('custom-open').addEventListener('click', function () {
    $('custom-details').open = true; syncPrompt();
    $('custom-details').scrollIntoView({behavior: 'smooth', block: 'center'});
  });
  syncPrompt();
  $('feed-check').addEventListener('click', function () {
    var src = $('feed-source').value.trim();
    if (!src) { showFormat([], 'Enter the path to a video file first.'); return; }
    showFormat([], 'Reading the file...');
    fetch('/feed/check', {method: 'POST', headers: {'Content-Type': 'application/json'},
                          body: JSON.stringify({source: src})})
      .then(function (r) { return r.json(); })
      .then(function (j) { showFormat(j.lines, j.ok ? '' : j.error); })
      .catch(function () { showFormat([], 'Could not read the file.'); });
  });
  refresh();
  setInterval(refresh, 2000);
  // top-bar meter: CPU and RAM of this computer, polled once a second; the graph keeps the last minute
  function updateSys(j) {
    var box = $('sysmeter');
    if (!j) { return; }
    box.hidden = !j.active;      // nothing sending or monitoring: the meter is not shown, as in Task Manager's idle state
    if (!j.active) { return; }
    if (!j.available) {
      $('sm-cpu-t').textContent = 'not shown on this system';
      $('sm-mem-t').textContent = '';
      return;
    }
    $('sm-cpu').style.width = j.cpu + '%';
    $('sm-ours').style.width = Math.min(j.cpu, j.ours) + '%';
    $('sm-mem').style.width = j.mem + '%';
    $('sm-cpu-t').textContent = Math.round(j.cpu) + '% (ours ' + Math.round(j.ours) + '%)';
    $('sm-mem-t').textContent = Math.round(j.mem) + '% (' + j.mem_used_gb + ' of ' + j.mem_total_gb + ' GB)';
    box.classList.toggle('hot', j.cpu >= 85 || j.mem >= 90);
    var h = j.history || [], n = h.length, c = [], m = [];
    for (var i = 0; i < n; i++) {
      var x = n > 1 ? (i * 120 / (n - 1)).toFixed(1) : '0';
      c.push(x + ',' + (25 - h[i][0] / 100 * 24).toFixed(1));
      m.push(x + ',' + (25 - h[i][1] / 100 * 24).toFixed(1));
    }
    $('sm-line-cpu').setAttribute('points', c.join(' '));
    $('sm-line-mem').setAttribute('points', m.join(' '));
  }
  function pollSys() { fetch('/sys', {cache: 'no-store'}).then(function (r) { return r.json(); }).then(updateSys).catch(function () {}); }
  pollSys();
  setInterval(pollSys, 1000);
})();
</script>
</body></html>
"""


def preflight_payload(query):
    """Local checks for a listener on this PC: port, firewall, address and VPN. Returns (status, body)."""
    q = parse_qs(query)
    mode = (q.get("mode") or ["listener"])[0]
    try:
        port = int((q.get("port") or ["0"])[0])
    except ValueError:
        return 400, {"error": "The port must be a number."}
    host = (q.get("host") or [None])[0] or None
    rows = core.check_environment(mode, host, port)
    label = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
    return 200, {"rows": [{"level": label.get(lvl, "INFO"), "name": name, "detail": detail, "fix": fix}
                          for lvl, name, detail, fix in rows]}


def thresholds_payload():
    saved = thresholds.load()
    return {"values": saved, "defaults": thresholds.DEFAULTS, "path": thresholds.path(),
            "spec": [{"key": k, "label": l, "default": d, "lo": lo, "hi": hi, "unit": u, "why": w}
                     for k, l, d, lo, hi, u, w in thresholds.SPEC]}


def _handler(session):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8", filename=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj), "application/json")

        def _download(self, name, ctype):
            path = os.path.join(session.dir or "", name)
            if not session.dir or not os.path.exists(path):
                self._send(404, "<p>No file yet. It is written when the monitor stops.</p>")
                return
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)

        def _pdf(self):
            html_path = os.path.join(session.dir or "", "run.html")
            if not session.dir or not os.path.exists(html_path):
                self._send(404, "<p>No report yet. It is written when the monitor stops.</p>")
                return
            pdf_path = os.path.join(session.dir, "run.pdf")
            with session.pdf_lock:
                state = dict(session.pdf)
                if state["state"] == "error":
                    session.pdf = {"state": None, "error": ""}   # the next click tries again
                elif state["state"] is None:
                    session.pdf = {"state": "working", "error": ""}
                    threading.Thread(target=session.make_pdf, args=(html_path, pdf_path),
                                     daemon=True).start()
                    state["state"] = "working"
            if state["state"] == "error":
                msg = (f"<p>The PDF could not be made: {html.escape(state['error'])}</p>"
                       "<p>Use the HTML report, or install Chrome, Chromium or Edge (see <code>srtdebug deps</code>).</p>")
                self._send(500, msg)
                return
            if state["state"] == "ready" and os.path.exists(pdf_path):
                with open(pdf_path, "rb") as f:
                    self._send(200, f.read(), "application/pdf", filename="srtdebug-report.pdf")
                return
            # still building: a small page that asks the browser to check again
            self._send(202, '<!doctype html><meta charset="utf-8">'
                       '<meta http-equiv="refresh" content="3"><title>Preparing the PDF</title>'
                       '<body style="font:15px system-ui;padding:32px">Preparing the PDF report. '
                       'This page reloads and starts the download when it is ready.</body>')

        def _zip(self):
            import io
            import zipfile
            if not session.dir or not os.path.exists(os.path.join(session.dir, "run.html")):
                self._send(404, "<p>No reports yet. They are written when the monitor stops.</p>")
                return
            pdf_path = os.path.join(session.dir, "run.pdf")
            with session.pdf_lock:
                if session.pdf.get("state") is None:
                    session.pdf = {"state": "working", "error": ""}
                    threading.Thread(target=session.make_pdf,
                                     args=(os.path.join(session.dir, "run.html"), pdf_path),
                                     daemon=True).start()
            deadline = time.time() + 90          # wait for the PDF so it is in the zip
            while time.time() < deadline:
                with session.pdf_lock:
                    if session.pdf.get("state") != "working":
                        break
                time.sleep(0.5)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name in ("run.html", "run.csv", "run.pdf", "monitor.log"):
                    fp = os.path.join(session.dir, name)
                    if os.path.exists(fp):
                        z.write(fp, name)
            self._send(200, buf.getvalue(), "application/zip", filename="srtdebug-reports.zip")

        def _proxy(self, path):
            try:
                body, ctype = session.fetch(path)
                self._send(200, body, ctype)
            except LookupError:
                self._send(404, "<p>No monitor is running. Start one from the control panel.</p>")
            except (urllib.error.URLError, OSError):
                self._send(503, "<p>The monitor is starting or has stopped. Refresh in a moment.</p>")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                self._send(200, CONTROL_PAGE)
            elif path == "/status":
                _window_seen()   # the open page asks for this every two seconds
                self._json(200, session.status())
            elif path == "/thresholds":
                self._json(200, thresholds_payload())
            elif path == "/preflight":
                self._json(*preflight_payload(self.path.partition("?")[2]))
            elif path == "/feed/status":
                self._json(200, FEEDER.status())
            elif path == "/sys":
                snap = METER.snapshot()
                snap["active"] = bool(_our_pids())   # the meter shows only while the feed or monitor runs
                self._json(200, snap)
            elif path == "/feed/log":
                # the whole debug log of the last feed, as a text file
                if not FEEDER.logpath or not os.path.exists(FEEDER.logpath):
                    self._send(404, "No feed log yet. Start a test feed first.", "text/plain; charset=utf-8")
                else:
                    with open(FEEDER.logpath, "rb") as f:
                        self._send(200, _redact(f.read().decode("utf-8", "replace")).encode("utf-8"),
                                   "text/plain; charset=utf-8", filename="srtdebug-feed.log")
            elif path == "/view":
                self._proxy("/")
            elif path == "/live":
                self._proxy("/live")
            elif path.startswith("/video/"):
                self._proxy(path)
            elif path in ("/export.csv", "/export.pdf"):
                self._proxy(path)
            elif path == "/report.csv":
                self._download("run.csv", "text/csv; charset=utf-8")
            elif path == "/report.pdf":
                self._pdf()
            elif path == "/reports.zip":
                self._zip()
            elif path == "/report":
                report = os.path.join(session.dir or "", "run.html")
                if session.dir and os.path.exists(report):
                    with open(report, "rb") as f:
                        download = "download=1" in (self.path.partition("?")[2])
                        self._send(200, f.read(), filename="srtdebug-report.html" if download else None)
                else:
                    self._send(404, "<p>No report yet. It is written when the monitor stops.</p>")
            else:
                self.send_error(404)

        def do_POST(self):
            path = self.path.split("?")[0]
            length = int(self.headers.get("Content-Length") or 0)
            if path == "/feed/upload":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                name = (query.get("name") or [""])[0]
                saved, lines, err, warn = save_upload(name, self.rfile, length)
                self._json(200, {"ok": not err, "path": saved, "lines": lines, "error": err, "warn": warn})
                return
            raw = self.rfile.read(min(length, 10000)) if length else b"{}"
            if path == "/thresholds":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"ok": False, "error": "Could not read the request."})
                    return
                if body.get("reset"):
                    thresholds.reset()
                    self._json(200, thresholds_payload())
                    return
                values, problems = thresholds.save(body.get("values") or {})
                if problems:
                    self._json(400, {"ok": False, "problems": problems, "values": values})
                else:
                    self._json(200, thresholds_payload())
            elif path == "/stop":
                session.stop()
                self._json(200, {"ok": True})
            elif path == "/window/focus":
                # the launcher asks before opening a tab: if a window is open, it is brought forward instead
                open_now = _window_open()
                if open_now:
                    FOCUS["n"] += 1
                self._json(200, {"open": open_now})
            elif path == "/quit":
                # answer first, then end the program the same way Ctrl-C does, which stops any feed or monitor
                self._json(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.5), os.kill(os.getpid(), signal.SIGTERM)),
                                 daemon=True).start()
            elif path == "/feed/start":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"ok": False, "error": "Could not read the request."})
                    return
                over = over_cpu_limit(body)
                if over is not None and not body.get("allow_over_cpu"):
                    # asked first, so a power user can go ahead knowingly
                    self._json(200, {"ok": False, "needs_confirm": True, "error":
                                     "%g Mb/s is above the limit for this computer (%g Mb/s). The encoder may fall behind "
                                     "real time, so the picture can stutter or freeze at the receiver. Send at %g Mb/s anyway?"
                                     % (over, cpu_profile()[2], over)})
                    return
                ok, msg = FEEDER.start(body)
                self._json(200 if ok else 400, {**FEEDER.status(), "ok": ok, "error": msg})
            elif path == "/feed/loglevel":
                try:
                    level = str(json.loads(raw.decode("utf-8") or "{}").get("level") or "")
                except (json.JSONDecodeError, AttributeError):
                    level = ""
                ok, msg = FEEDER.set_loglevel(level)
                self._json(200 if ok else 400, {**FEEDER.status(), "ok": ok, "error": msg})
            elif path == "/feed/stop":
                FEEDER.stop()
                self._json(200, {"ok": True, **FEEDER.status()})
            elif path == "/feed/check":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"ok": False, "error": "Could not read the request."})
                    return
                source = os.path.expanduser(str(body.get("source") or "").strip())
                if not source:
                    self._json(400, {"ok": False, "error": "Enter the path to a video file first."})
                    return
                sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
                import tsanalyze
                sections, err = tsanalyze.describe_source(source)
                if err:
                    self._json(400, {"ok": False, "error": err, "lines": []})
                else:
                    self._json(200, {"ok": True, "error": "", "lines": tsanalyze.source_report_lines(sections)})
            elif path == "/feed/samples":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                asked = body.get("dir") if isinstance(body, dict) and "dir" in body else FEEDER.samples_dir
                folder, err = checked_samples_dir(asked)
                if err:
                    self._json(400, {**FEEDER.status(), "ok": False, "error": err})
                    return
                FEEDER.start_making(folder)
                self._json(200, {"ok": True, "error": "", **FEEDER.status()})
            elif path == "/feed/clearview":
                ok, msg = FEEDER.clear_view()
                self._json(200 if ok else 400, {**FEEDER.status(), "ok": ok, "error": msg})
            elif path == "/feed/command":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                uri, mode, problem = feed_uri(body)
                if problem:
                    self._json(200, {"ok": False, "command": "", "error": problem})
                else:
                    clip = os.path.basename(str(body.get("clip") or "")) or "<clip>"
                    path_text = os.path.expanduser(str(body.get("source") or "").strip()) or os.path.join(FEEDER.samples_dir, clip)
                    level = str(body.get("loglevel") or FEEDER.loglevel)
                    if level not in LOG_LEVELS:
                        level = "debug"
                    enc_args, _label, enc_problem = encode_plan(body)
                    if enc_problem:
                        self._json(200, {"ok": False, "command": "", "error": enc_problem})
                    else:
                        self._json(200, {"ok": True, "error": "", "stream_id": decoded_stream_id(uri),
                                         "command": feed_command_text(uri, path_text, bool(body.get("loop")), level,
                                                                      encode=enc_args)})
            elif path == "/feed/destination":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                body = body if isinstance(body, dict) else {}
                name = str(body.get("name") or "").strip()
                saved = load_destinations()
                if not name or len(name) > 60:
                    self._json(200, {"ok": False, "error": "Give the destination a name (up to 60 characters).",
                                     "destinations": saved})
                elif body.get("action") == "delete":
                    saved = [d for d in saved if d["name"] != name]
                    save_settings(destinations=saved)
                    self._json(200, {"ok": True, "error": "", "destinations": saved})
                else:
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    dest = {k: str(fields.get(k) or "").strip() for k in DEST_KEYS}
                    problem = ""
                    if dest["url"]:
                        problem = check_srt_url(dest["url"]) or ""
                        dest = {"url": dest["url"]}
                    else:
                        dest = {k: dest[k] for k in ("mode", "port", "host", "streamid", "latency")}
                        if not dest["port"].isdigit() or not 1 <= int(dest["port"]) <= 65535:
                            problem = "The port must be 1-65535."
                    if problem:
                        self._json(200, {"ok": False, "error": problem, "destinations": saved})
                    else:
                        saved = [d for d in saved if d["name"] != name] + [{"name": name, **dest}]
                        save_settings(destinations=saved)
                        self._json(200, {"ok": True, "error": "", "destinations": saved})
            elif path == "/monitor/preset":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                body = body if isinstance(body, dict) else {}
                name = str(body.get("name") or "").strip()
                saved = load_presets()
                if not name or len(name) > 60:
                    self._json(200, {"ok": False, "error": "Give the preset a name (up to 60 characters).", "presets": saved})
                elif body.get("action") == "delete":
                    saved = [p for p in saved if p["name"] != name]
                    save_settings(monitor_presets=saved)
                    self._json(200, {"ok": True, "error": "", "presets": saved})
                else:
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    preset = {k: str(fields.get(k) or "").strip() for k in MON_PRESET_KEYS}
                    problem = ""
                    if preset["mode"] not in ("listener", "caller"):
                        problem = "Choose listener or caller."
                    elif not preset["port"].isdigit() or not 1 <= int(preset["port"]) <= 65535:
                        problem = "The port must be 1-65535."
                    elif preset["latency"] and not preset["latency"].isdigit():
                        problem = "The latency must be a whole number of milliseconds."
                    if problem:
                        self._json(200, {"ok": False, "error": problem, "presets": saved})
                    else:
                        saved = [p for p in saved if p["name"] != name] + [{"name": name, **preset}]
                        save_settings(monitor_presets=saved)
                        self._json(200, {"ok": True, "error": "", "presets": saved})
            elif path == "/feed/clipinfo":
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except (ValueError, json.JSONDecodeError):
                    body = {}
                name = os.path.basename(str((body or {}).get("clip") or ""))
                if name not in clips(FEEDER.samples_dir):
                    self._json(200, {"ok": False, "lines": [], "error": "Choose a clip first."})
                else:
                    clip_path = os.path.join(FEEDER.samples_dir, name)
                    lines, err = clip_metadata(clip_path)
                    warn = "" if err else source_gop_problem(clip_path)
                    av = None if err else source_av_offset(clip_path)
                    if av is not None:
                        lines = lines + ["A/V start offset: %+.0f ms (positive: audio leads)" % av]
                    self._json(200, {"ok": not err, "lines": lines, "error": err, "warn": warn,
                                     "av_warn": source_av_warning(av)})
            elif path == "/feed/folder":
                self._json(200, browse_for_folder(FEEDER.samples_dir))
            elif path == "/start":
                try:
                    cfg = parse_config(json.loads(raw.decode("utf-8") or "{}"))
                except (ValueError, json.JSONDecodeError) as e:
                    self._json(400, {"ok": False, "error": str(e)})
                    return
                feed = _active_feed_side()
                if feed and port_clash(_side_of_monitor(cfg), feed):
                    self._json(400, {"ok": False, "error": "Port %s is already in use by the feed on this PC. "
                                     "Choose a different port for the monitor." % feed["port"]})
                    return
                session.start(cfg)
                self._json(200, {"ok": True})
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            pass

    return Handler


SAMPLES_DIR = os.path.join(os.path.expanduser("~"), "srtdebug-samples")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".srtdebug", "settings.json")


def load_settings():
    """Settings kept between runs (the chosen clips folder). Missing or unreadable means defaults."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    data = load_settings()
    data.update(changes)
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        pass   # the choice still applies for this run


DEST_KEYS = ("url", "mode", "port", "host", "streamid", "latency")


def load_destinations():
    """Saved destinations from the settings file. A passphrase is never saved: it is entered each time."""
    out = []
    for item in load_settings().get("destinations") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out.append({"name": str(item["name"]), **{k: item[k] for k in DEST_KEYS if k in item}})
    return out


MON_PRESET_KEYS = ("mode", "port", "host", "latency", "streamid")


def load_presets():
    """Decode presets (connection settings) from the settings file. A passphrase is never saved."""
    out = []
    for item in load_settings().get("monitor_presets") or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out.append({"name": str(item["name"]), **{k: str(item[k]) for k in MON_PRESET_KEYS if k in item}})
    return out


def checked_samples_dir(text):
    """Returns (absolute folder, '') or ('', reason). Creates the folder if it is missing."""
    text = str(text or "").strip()
    if not text:
        return "", "Enter a folder path first."
    p = os.path.abspath(os.path.expanduser(text))
    if os.path.exists(p) and not os.path.isdir(p):
        return "", "That path is a file, not a folder."
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as e:
        return "", "Could not create that folder: " + str(e)
    if not os.access(p, os.W_OK):
        return "", "That folder is not writable."
    return p, ""


def _explain_ffmpeg(piece):
    """One ffmpeg message, in plain words. Returns (sentence or None, colour class)."""
    text = piece.split("ffmpeg: ", 1)[-1].strip()
    m = re.search(r"Video: (\w+).*?\b(\d{3,5})x(\d{3,5})\b.*?([\d.]+) fps", text)
    if m:
        codec = {"h264": "H.264", "mpeg2video": "MPEG-2"}.get(m.group(1), m.group(1))
        return f"Clip video: {codec} {m.group(2)}x{m.group(3)} at {m.group(4)} frames/s.", "info"
    m = re.search(r"Audio: (\w+).*?(\d+) Hz, (\w+)", text)
    if m:
        codec = {"aac": "AAC", "mp2": "MP2"}.get(m.group(1), m.group(1))
        return f"Clip audio: {codec}, {int(m.group(2)) // 1000} kHz, {m.group(3)}.", "info"
    if text.startswith("Input #0"):
        return "ffmpeg opened the clip.", "info"
    if text.startswith("Output #0"):
        return "ffmpeg is packing the clip into MPEG-TS for the sender.", "info"
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    if m:
        seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return f"Clip length: {seconds:.1f} seconds.", "info"
    # mapping and tag details are in the raw log; they are not sentences a reader needs
    if (text.startswith(("Stream mapping", "Program", "Metadata", "No explicit maps", "Press"))
            or "->" in text or "mapping streams" in text or "Created " in text
            or re.match(r"(service_|encoder|handler|creation_time|major_brand|compatible_brands|timecode)", text.strip())):
        return None, "muted"
    if "error" in text.lower():
        return "ffmpeg reported an error: " + text[:160], "bad"
    if "warning" in text.lower():
        return "ffmpeg warning: " + text[:160], "muted"
    return "ffmpeg: " + text[:160], "muted"


def _explain(piece):
    """One transmitter line, in plain words. Returns (sentence, colour class)."""
    if "ffmpeg: " in piece:
        return _explain_ffmpeg(piece)
    m = re.search(r"Connection established to: (\S+)", piece)
    if m:
        return f"Handshake completed with {m.group(1)}.", "info"
    if "SRT target connected" in piece:
        return "Connected. The stream is open to the other side.", "ok"
    if "SRT target disconnected" in piece:
        return "The other side closed the connection. Retrying.", "bad"
    if "REJECT" in piece or "rejecting per reception" in piece:
        if "Password required" in piece or "UNSECURE" in piece:
            return "Refused: the other side wants a passphrase, or does not want one from this feed.", "bad"
        if "BADSECRET" in piece or "Wrong passphrase" in piece:
            return "Refused: the passphrase does not match.", "bad"
        return "Refused by the other side.", "bad"
    if "SECURED" in piece:
        return "Encryption agreed with the other side.", "ok"
    if "Broken pipe" in piece:
        return "The feed input closed. The feed has stopped.", "muted"
    if piece.startswith("[") and "Error" in piece:
        return "The feed pipeline reported an error: " + piece.split("] ", 1)[-1][:120] + ".", "muted"
    if "Media path" in piece:
        return "Starting. Sending the feed to the other side" + (" with a stream ID." if "streamid" in piece else "."), "info"
    if "log level now" in piece:
        return "Log level changed. Reconnecting.", "info"
    m = re.search(r"([\d.]+) bytes lost, ([\d.]+) bytes sent, ([\d.]+) bytes received", piece)
    if m:
        return (f"Totals so far: {round(float(m.group(2)) / 1e6, 2)} MB sent, "
                f"{round(float(m.group(3)) / 1e6, 2)} MB received, {round(float(m.group(1)) / 1000, 1)} kB lost.", "info")
    if "generateSocketID" in piece:
        return None, "muted"   # a new attempt is always followed by a handshake line; it is left out of the readable view
    return piece[:160], "muted"


def _readable(lines):
    """The log as rows. Connect, close and handshake lines that repeat are merged into one row with counts,
    so a long run of reconnects reads as one sentence."""
    rows, cycle = [], None

    def flush():
        nonlocal cycle
        if cycle:
            parts = []
            if cycle["connects"]:
                parts.append(f"connected {cycle['connects']} time" + ("s" if cycle["connects"] != 1 else ""))
            if cycle["closes"]:
                parts.append(f"was closed by the other side {cycle['closes']} time" + ("s" if cycle["closes"] != 1 else ""))
            if cycle["last"] == "connect":
                # the latest event is a connect: the link is up now, and any closes were earlier
                if cycle["closes"]:
                    text = f"Connected now. Earlier, the other side closed it {cycle['closes']} time" + ("s" if cycle["closes"] != 1 else "") + "."
                else:
                    text = "Connected."
                level = "ok"
            else:
                text = ("Connected, then the other side closed it: " + ", ".join(parts) + ".") if cycle["closes"] else \
                       ("Connected: " + ", ".join(parts) + ".")
                level = "bad" if cycle["closes"] else "ok"
            rows.append({"text": text, "level": level, "count": 1,
                         "start": cycle["start"], "end": cycle["end"]})
            cycle = None

    for raw in lines:
        raw = raw.replace("SRT target", "\nSRT target")
        for piece in re.split(r"\n|(?=\d{2}:\d{2}:\d{2}\.\d+/)", raw):
            piece = piece.strip()
            if not piece:
                continue
            t = re.match(r"(\d{2}:\d{2}:\d{2})", piece)
            when = t.group(1) if t else ""
            if "SRT target connected" in piece or "SRT target disconnected" in piece or \
               re.search(r"Connection established to: ", piece):
                if cycle is None:
                    cycle = {"connects": 0, "closes": 0, "last": "", "start": when, "end": when}
                if "SRT target connected" in piece:
                    cycle["connects"] += 1
                    cycle["last"] = "connect"
                if "SRT target disconnected" in piece:
                    cycle["closes"] += 1
                    cycle["last"] = "close"
                if when:
                    cycle["end"] = when
                continue
            if "generateSocketID" in piece:
                continue
            flush()
            text, level = _explain(piece)
            if text is None:
                continue
            if rows and rows[-1]["text"] == text:
                rows[-1]["count"] += 1
                rows[-1]["end"] = when or rows[-1]["end"]
            else:
                rows.append({"text": text, "level": level, "count": 1, "start": when, "end": when})
    flush()
    return rows[-60:]


def _plain_events(lines):
    """The transmitter's log in plain sentences: what connected, what closed, and the running totals."""
    events, totals = [], None
    connects = closes = 0
    for line in lines:
        for _ in range(line.count("SRT target connected")):
            connects += 1
            events.append("Connected to the other side.")
        for _ in range(line.count("SRT target disconnected")):
            closes += 1
            events.append("The other side closed the connection. Retrying.")
        if "SECURED" in line:
            events.append("Encryption agreed with the other side.")
        m = re.search(r"([\d.]+) bytes lost, ([\d.]+) bytes sent, ([\d.]+) bytes received", line)
        if m:
            totals = {"lost_kb": round(float(m.group(1)) / 1000, 1), "sent_mb": round(float(m.group(2)) / 1e6, 2),
                      "received_mb": round(float(m.group(3)) / 1e6, 2)}
        if "Broken pipe" in line:
            events.append("The feed input closed (the feed was stopped, or it ended).")
        if "Media path" in line:
            events.append("Started sending.")
    # collapse repeats in a row so a long run of retries reads as one line
    collapsed = []
    for e in events:
        if collapsed and collapsed[-1][0] == e:
            collapsed[-1][1] += 1
        else:
            collapsed.append([e, 1])
    return collapsed[-8:], connects, closes, totals


def _forward(src, dst, analyser):
    """Copies the transport stream from ffmpeg to srt-live-transmit and measures it on the way.
    The copy is written first, so measuring never delays the stream. If measuring fails, the copy carries on."""
    measuring = analyser is not None
    try:
        while True:
            chunk = src.read1(65536)
            if not chunk:
                break
            dst.write(chunk)
            dst.flush()
            if measuring:
                try:
                    analyser.feed(chunk)
                except Exception:
                    measuring = False
    except (OSError, ValueError):
        pass   # srt-live-transmit closed its input, or the feed was stopped
    finally:
        for f in (dst, src):
            try:
                f.close()
            except OSError:
                pass


def _pump_lines(stream, sink, prefix):
    """Copies a process's messages into the feed log, one time-stamped line each, until the process ends."""
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            sink.write(time.strftime("%H:%M:%S") + " " + prefix + text + "\n")
            sink.flush()
    except (OSError, ValueError):
        pass   # the log was closed for a restart; the process is ending too
    finally:
        try:
            stream.close()
        except OSError:
            pass


def decoded_stream_id(uri):
    """The stream ID as the other side receives it: the encoded value in the address, decoded. '' if none."""
    m = re.search(r"[?&]streamid=([^&]*)", uri or "")
    return unquote(m.group(1)) if m else ""


def cpu_profile():
    """(x264 preset, threads, bit rate in Mb/s) for the machine ffmpeg runs on. Nothing here is set by the user:
    a heavier setting than the machine can keep up with would make the feed itself stutter."""
    cores = os.cpu_count() or 2
    if cores >= 8:
        return "veryfast", cores - 2, 6
    if cores >= 4:
        return "superfast", cores - 2, 5
    return "ultrafast", 1, 4


RATE_CEILING_MBPS = 50   # a limit against typing mistakes; above this computer's own limit the user is asked to confirm


def cpu_note():
    """Why the bit rate limit is what it is, in words for the panel."""
    cores = os.cpu_count() or 2
    preset, threads, top = cpu_profile()
    return ("This computer has " + str(cores) + " processor core" + ("" if cores == 1 else "s") + ". Re-encoding at up to "
            + str(top) + " Mb/s with the " + preset + " setting keeps up in real time, so the feed does not stutter. "
            "A higher rate may fall behind the clock and make the picture stutter or freeze.")


def over_cpu_limit(body):
    """The typed bit rate when it is above this computer's limit and the video is re-encoded, else None."""
    if str(body.get("encode") or "") != "h264":
        return None
    typed = str(body.get("rate") or "").strip()
    if not typed:
        return None
    try:
        rate = float(typed)
    except ValueError:
        return None
    return rate if rate > cpu_profile()[2] else None


WIFI_MIN_LATENCY = 5000   # the latency used on Wi-Fi unless the user allows less
_NETWORK = {"at": 0.0, "value": None}


def network_detected():
    """What this PC's connection looks like: True (Wi-Fi), False (not Wi-Fi) or None (could not tell). Cached for a minute."""
    now = time.time()
    if now - _NETWORK["at"] > 60:
        _NETWORK["value"] = netlink.network_is_wifi(None)
        _NETWORK["at"] = now
    return _NETWORK["value"]
CLOCK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf"]


def clock_filter(clock_file="clock.txt"):
    """A clock drawn on the picture: UTC time to the millisecond, in a dark box.
    The text comes from a file the feeder rewrites about every 20 ms, because ffmpeg's own clock stops at seconds."""
    font = next((f for f in CLOCK_FONTS if os.path.isfile(f)), "")
    opts = ["textfile=" + clock_file, "reload=1", "x=20", "y=20", "fontsize=h/22",
            "fontcolor=white", "box=1", "boxcolor=black@0.55", "boxborderw=10"]
    if font:
        opts.insert(0, "fontfile=" + font)
    return "drawtext=" + ":".join(opts)


_FIELD_ORDER_CACHE = {}


def source_field_order(path):
    """'tt' or 'bb' for an interlaced clip (top or bottom field first), '' for progressive or unknown."""
    if not path or not os.path.isfile(path):
        return ""
    key = (path, os.path.getmtime(path))
    if key not in _FIELD_ORDER_CACHE:
        order = ""
        try:
            out = subprocess.run([shutil_which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=field_order", "-of", "json", path],
                                 capture_output=True, text=True, timeout=20)
            field = str(((json.loads(out.stdout or "{}").get("streams") or [{}])[0]).get("field_order", ""))
            order = {"tt": "tt", "tb": "tt", "bb": "bb", "bt": "bb"}.get(field, "")
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            order = ""
        _FIELD_ORDER_CACHE[key] = order
    return _FIELD_ORDER_CACHE[key]


def encode_plan(body):
    """What happens to the video before it is sent: (ffmpeg args, a label for the panel, problem).
    The re-encode settings come from this computer (cpu_profile), not from the panel."""
    enc = str(body.get("encode") or "copy")
    clock = bool(body.get("clock"))
    if enc == "copy" and clock:
        return None, "", "The clock is drawn on the picture, so the video has to be re-encoded. Choose Re-encode to H.264."
    if enc == "copy":
        return ["-c", "copy"], "Send as it is (no re-encode)", ""
    if enc != "h264":
        return None, "", "Choose Send as it is, or Re-encode to H.264."
    preset, threads, top = cpu_profile()
    rate = top
    typed = str(body.get("rate") or "").strip()
    if typed:
        try:
            rate = float(typed)
        except ValueError:
            return None, "", "The bit rate must be a number of Mb/s, for example 3."
        if not 0.5 <= rate <= RATE_CEILING_MBPS:
            return None, "", f"The bit rate must be between 0.5 and {RATE_CEILING_MBPS} Mb/s."
    kb = int(round(rate * 1000))
    # an interlaced source stays interlaced: without these flags x264 codes each frame as progressive
    order = str(body.get("_field_order") or "")
    x264 = "nal-hrd=cbr:force-cfr=1" + (":bff=1" if order == "bb" else "")
    interlace = (["-flags", "+ilme+ildct"] + (["-top", "1"] if order == "tt" else [])) if order else []
    args = (["-vf", clock_filter(str(body.get("_clock_file") or "clock.txt"))] if clock else []) + [
            "-c:v", "libx264", "-preset", preset, "-threads", str(threads),
            "-force_key_frames", "expr:gte(t,n_forced*1)", "-b:v", f"{kb}k",
            "-minrate", f"{kb}k", "-maxrate", f"{kb}k", "-bufsize", f"{kb}k",
            "-x264-params", x264]
    args += interlace + ["-c:a", "aac", "-b:a", "128k"]
    label = f"H.264 at {rate} Mb/s constant, keyframe every second (x264 {preset}, {threads} threads)"
    if rate > top:
        label += ", above this computer's suggested limit"
    if order:
        label += ", interlaced, %s field first" % ("top" if order == "tt" else "bottom")
    if clock:
        label += ", with a clock on the picture"
    return args, label, ""


def feed_command_text(uri, path, loop, level, stats="<stats file>", encode=None):
    """The two programs of a feed, as a shell pipeline a person can copy. The passphrase is hidden."""
    import shlex
    src = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", FFMPEG_LOGLEVEL.get(level, "verbose"), "-nostats", "-re"]
    src += (["-stream_loop", "-1"] if loop else [])
    src += ["-i", path] + (encode or ["-c", "copy"]) + ["-f", "mpegts"] + FEED_MUX + ["-"]
    # -chunk:1316 sets the media payload per SRT packet: 1316 bytes, seven 188-byte transport-stream packets
    dst = ["srt-live-transmit", "file://con", uri, "-chunk:1316", "-loglevel:" + level, "-stats-report-frequency:500",
           "-statsout:" + stats, "-pf:json"]
    return _redact(shlex.join(src) + " | " + shlex.join(dst))


def _redact(text):
    """The debug log prints the destination with its passphrase in it: hide the value wherever the log is shown."""
    return re.sub(r"(passphrase=)[^&\s'\"]*", r"\1***", text)


SRT_URL_KEYS = ("mode", "streamid", "latency", "passphrase", "pbkeylen", "pkt_size")


def check_srt_url(url):
    """The structure a destination URL must have. Returns a message for the user, or '' when it is usable."""
    if not re.match(r"srt://", url, re.I):
        return "The address must start with srt:// (for example srt://203.0.113.5:9000)."
    m = re.match(r"srt://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)?:(\d{1,5})(?:\?(.*))?$", url, re.I | re.S)
    if not m:
        return "The address must look like srt://host:port, then ?options if needed. Check the host and port."
    port = int(m.group(2))
    if not 1 <= port <= 65535:
        return "The port must be between 1 and 65535."
    mode = "caller"
    for part in [x for x in (m.group(3) or "").split("&") if x]:
        key, sep, value = part.partition("=")
        key = key.lower()
        if not sep:
            return f"'{key}' needs a value after an equals sign, for example {key}=..."
        if key not in SRT_URL_KEYS:
            return f"'{key}' is not a setting this tool knows. Check the spelling, or remove it."
        if key == "mode":
            if value.lower() not in ("caller", "listener", "rendezvous"):
                return "mode must be caller, listener or rendezvous."
            mode = value.lower()
        elif key == "latency" and not value.isdigit():
            return "latency must be a whole number of milliseconds."
        elif key == "passphrase" and not core.PASSPHRASE_LEN[0] <= len(value) <= core.PASSPHRASE_LEN[1]:
            return f"The passphrase in the address must be {core.PASSPHRASE_LEN[0]} to {core.PASSPHRASE_LEN[1]} characters."
        elif key == "pbkeylen" and value not in ("16", "24", "32"):
            return "pbkeylen must be 16, 24 or 32."
        elif key == "pkt_size" and not (value.isdigit() and 188 <= int(value) <= 1456):
            return "pkt_size must be a number of bytes from 188 to 1456 (1316 for media)."
    if mode == "caller" and not m.group(1):
        return "A caller needs a host, for example srt://203.0.113.5:9000."
    return ""


# the feed re-muxes the clip, so the broadcast timing is set here too: PCR every 40 ms, PAT/PMT every 100 ms
AV_MEASURE_ON = False   # the outgoing A/V offset is measured but not shown until it is validated
FEED_MUX = ["-pcr_period", "40", "-pat_period", "0.1", "-sdt_period", "0.5"]
FFMPEG_LOGLEVEL = {"warn": "warning", "note": "info", "info": "info", "debug": "verbose"}
LOG_LEVELS = ("warn", "note", "info", "debug")   # srt-live-transmit levels offered in the panel


def _key_and_id_problem(body):
    """Checks the options SRT itself limits: key size (16, 24 or 32 bytes) and stream ID (at most 512 bytes)."""
    pbk = str(body.get("pbkeylen") or "").strip()
    if pbk not in ("", "16", "24", "32"):
        return "The encryption strength must be AES-128, AES-192 or AES-256."
    streamid = str(body.get("streamid") or "").strip()
    if len(streamid.encode("utf-8")) > 512:
        return "The stream ID is longer than SRT allows (512 bytes). Shorten it."
    return ""


def feed_uri(body):
    """The SRT address for a feed. Returns (uri, mode, problem); problem is empty when the address is usable."""
    url = (body.get("url") or "").strip()
    if url:
        problem = check_srt_url(url)
        if problem:
            return None, None, problem
        mode = "caller" if "mode=caller" in url else "listener" if "mode=listener" in url else "caller"
        # the Stream ID, latency and passphrase fields apply to a URL too, unless the URL already sets them.
        # The query is edited as text: a stream ID may contain '#', which a URL parser would treat as a fragment.
        passphrase = str(body.get("passphrase") or "")
        if passphrase and not core.PASSPHRASE_LEN[0] <= len(passphrase) <= core.PASSPHRASE_LEN[1]:
            return None, None, f"The passphrase must be {core.PASSPHRASE_LEN[0]} to {core.PASSPHRASE_LEN[1]} characters."
        problem = _key_and_id_problem(body)
        if problem:
            return None, None, problem
        base, _, query = url.partition("?")
        parts = [part for part in query.split("&") if part]
        keys = set()
        for i, part in enumerate(parts):
            key, _, value = part.partition("=")
            keys.add(key)
            if key == "streamid":
                # escape the value once, so a raw ID such as #!::r=...,m=publish reaches the listener intact
                parts[i] = "streamid=" + quote(unquote(value), safe="")
        pbk = str(body.get("pbkeylen") or "").strip() if passphrase else ""
        extra = [("streamid", (body.get("streamid") or "").strip()),
                 ("latency", str(body.get("latency") or "").strip()),
                 ("passphrase", passphrase),
                 ("pbkeylen", pbk)]
        for key, value in extra:
            if value and key not in keys:
                parts.append(key + "=" + quote(value, safe=""))
        uri = base + ("?" + "&".join(parts) if parts else "")
    else:
        mode = body.get("mode") or "caller"
        try:
            port = int(body.get("port") or 0)
        except (TypeError, ValueError):
            return None, None, "The port must be a number."
        if not 1 <= port <= 65535:
            return None, None, "The port must be 1-65535."
        host = (body.get("host") or "").strip() or None
        if mode == "caller" and not host:
            return None, None, "Caller mode needs the remote address, or give a destination URL."
        try:
            latency = int(body["latency"]) if body.get("latency") not in (None, "") else None
        except (TypeError, ValueError):
            return None, None, "The latency must be a number of milliseconds."
        passphrase = str(body.get("passphrase") or "")
        if passphrase and not core.PASSPHRASE_LEN[0] <= len(passphrase) <= core.PASSPHRASE_LEN[1]:
            return None, None, f"The passphrase must be {core.PASSPHRASE_LEN[0]} to {core.PASSPHRASE_LEN[1]} characters."
        problem = _key_and_id_problem(body)
        if problem:
            return None, None, problem
        uri = core.build_uri(mode, host, port, latency=latency,
                             streamid=(body.get("streamid") or "").strip() or None,
                             passphrase=passphrase or None,
                             pbkeylen=(str(body.get("pbkeylen") or "").strip() or None) if passphrase else None)
    return uri, mode, ""


class Feeder:
    """Sends a test clip over SRT, as caller or listener, or to a destination URL. One feed at a time.
    The same pipeline as 'srtdebug feed': ffmpeg reads the clip at its own speed and srt-live-transmit sends it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.p1 = None
        self.p2 = None
        self.clip = ""
        self.clip_path = ""
        self.latency_set = None
        self.peer = None
        self.encode_label = ""
        self.encode_copy = True
        self.target = ""
        self.error = ""
        self.make = {"state": "idle", "error": ""}   # making the sample clips, in the background
        self.samples_dir = str(load_settings().get("samples_dir") or SAMPLES_DIR)
        self.log = None        # file both processes write to; read for the connection state
        self.logpath = ""
        self.started = 0.0
        self.mode = ""
        self.host_port = ""
        self.encrypted = False
        self.statspath = ""
        self._stats_off = 0
        self._stats_last = None
        self._stats_tot = {"bytes": 0, "packets": 0, "lost": 0, "retrans": 0, "dropped": 0, "recv": 0}
        self.loglevel = "debug"
        self.command = ""      # the command the current or last feed was started with
        self.view_offset = 0   # the panel shows the log from here on; set by "Clear from view"
        self.last_body = None   # the settings of the last feed, kept in memory only, so a level change can restart it
        self.want = False       # True from Start until Stop: a feed that ends without Stop is resumed
        self.gen = 0            # bumped by every start and stop, so an old watchdog knows to exit
        self.resume_at = 0.0    # seconds into the clip where the current run began
        self.progpath = ""      # ffmpeg writes its position here, so a resume knows where the clip got to
        self.notice = ""        # plain sentence shown while the feed is reconnecting or resuming
        self.restarts = []      # times of automatic resumes, to stop a feed that keeps failing
        self.run_offset = 0     # where this run's lines start in the shared log
        self.wifi = None        # True when this run's traffic goes over Wi-Fi, None when unknown
        self.latency_auto = False   # True when the latency was chosen automatically for the network
        self.wifi_short = False     # True when a shorter latency than the Wi-Fi value was allowed by the user
        self.wifi_forced = False    # True when a typed latency was raised to the Wi-Fi value
        self.spike_watch = spikes.SpikeWatch()   # round-trip spikes in this session
        self.ts = None                           # analyser for the stream as sent, for A/V offset
        self.link_pos = 0       # how far the log has been read for the link state
        self.link_state = ""    # "up" or "down", from the newest connect or disconnect line

    def running(self):
        return self.p2 is not None and self.p2.poll() is None

    def _last_stats(self):
        """Totals for this feed, and the newest rates. Each report covers only its own interval, so they are added up here."""
        if not self.statspath or not os.path.exists(self.statspath):
            return None
        data = None
        try:
            with open(self.statspath, "rb") as f:
                f.seek(self._stats_off)
                chunk = f.read()
            end = chunk.rfind(b"\n")
            if end >= 0:
                self._stats_off += end + 1
                for raw_line in chunk[:end].split(b"\n"):
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        one = json.loads(line)
                    except ValueError:
                        continue
                    snd = one.get("send", {})
                    tot = self._stats_tot
                    tot["bytes"] += snd.get("bytes", 0)
                    tot["packets"] += snd.get("packets", 0)
                    tot["lost"] += snd.get("packetsLost", 0)
                    tot["retrans"] += snd.get("packetsRetransmitted", 0)
                    tot["dropped"] += snd.get("packetsDropped", 0)
                    tot["recv"] += one.get("recv", {}).get("packets", 0)
                    self.spike_watch.add(one.get("link", {}).get("rtt", 0), tot["dropped"])
                    data = one
        except OSError:
            return None
        if data is None:
            data = getattr(self, "_stats_last", None)
            if data is None:
                return None
        self._stats_last = data
        tot = self._stats_tot
        send, recv, link = data.get("send", {}), data.get("recv", {}), data.get("link", {})
        send = dict(send, bytes=tot["bytes"], packets=tot["packets"], packetsLost=tot["lost"],
                    packetsRetransmitted=tot["retrans"], packetsDropped=tot["dropped"])
        recv = dict(recv, packets=tot["recv"])
        sent_mb = round(send.get("bytes", 0) / 1e6, 2)
        recv_mb = round(recv.get("bytes", 0) / 1e6, 2)
        rtt_ms = round(link.get("rtt", 0), 1)
        latency_ms = int(recv.get("msTsbPdDelay", 0) or 0)
        buffer_ms = int(send.get("msBuf", 0) or 0)
        lost, dropped = send.get("packetsLost", 0), send.get("packetsDropped", 0)
        alerts = []
        if dropped:
            alerts.append(f"{dropped} packets were too late to play and were dropped. The link cannot keep up with the latency set.")
        if lost:
            alerts.append(f"{lost} packets were lost on the way and {send.get('packetsRetransmitted', 0)} were resent.")
        if rtt_ms > 150:
            alerts.append(f"Round trip is {rtt_ms} ms, which is high for a contribution link.")
        sp = self.spike_watch.summary()
        if sp:
            alerts.append(f"{sp['last']} Spikes this session: {sp['count']}.")
        av = self._av_now()
        if av is not None and not (core.AV_DETECT_MS[0] <= av <= core.AV_DETECT_MS[1]):
            side = "audio leads" if av > 0 else "audio lags"
            alerts.append(f"Audio and video are {av:+.0f} ms apart on the way out ({side}). Viewers can notice this when "
                          "audio is more than 45 ms ahead of video or 125 ms behind (ITU-R BT.1359).")
        if latency_ms and buffer_ms > 0.5 * latency_ms:
            alerts.append(f"The sender buffer holds {buffer_ms} ms of the {latency_ms} ms latency. The link is struggling to keep up.")
        return {"sent_mb": sent_mb, "sent_packets": send.get("packets", 0),
                "retransmitted": send.get("packetsRetransmitted", 0), "lost": lost,
                "recv_packets": recv.get("packets", 0), "recv_mb": recv_mb, "rtt_ms": rtt_ms,
                "rate_mbps": round(send.get("mbitRate", 0), 2),
                "latency_ms": latency_ms, "buffer_ms": buffer_ms, "av_ms": av, "alerts": alerts}

    def clear_view(self):
        """Hides the log from the panel. The file stays, so Download still has everything, until the next feed."""
        with self.lock:
            if self.running():
                return False, "Stop the feed first, then clear the log."
            if self.logpath and os.path.exists(self.logpath):
                self.view_offset = os.path.getsize(self.logpath)
            return True, ""

    def _progress(self):
        """What the feed is doing now, read from the transmitter's log. Only a few lines are kept."""
        lines = []
        if self.logpath and os.path.exists(self.logpath):
            try:
                with open(self.logpath, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(self.view_offset, f.tell() - 300000, 0))   # the panel keeps the last ~300 KB while running
                    text = _redact(f.read().decode("utf-8", "replace"))
                for line in text.splitlines():
                    line = line.strip()
                    if line and all(32 <= ord(c) < 127 for c in line) and len(line) < 240:
                        lines.append(line)
            except OSError:
                pass
        # the link state is kept from line to line, so a close is not lost when ffmpeg's output pushes it out of view
        if self.logpath and os.path.exists(self.logpath):
            try:
                with open(self.logpath, "rb") as f:
                    f.seek(max(self.link_pos, self.run_offset, 0))
                    fresh = _redact(f.read().decode("utf-8", "replace"))
                    self.link_pos = f.tell()
                for l in fresh.splitlines():
                    if re.search(r"SRT target connected|Accepted SRT|Connection established", l):
                        self.link_state = "up"
                    elif "SRT target disconnected" in l:
                        self.link_state = "down"
            except OSError:
                pass
        link = self.link_state
        connected = link == "up"
        refused = ""
        for l in lines:
            if "REJECT" in l or "rejecting per reception" in l:
                reason = l.split(":")[-1].strip() if "REJECT" in l else l
                if "Password required" in l or "UNSECURE" in l:
                    refused = ("The listener is not set up for a passphrase, but this feed sends one. Remove the passphrase here, or set it on the listener."
                               if self.encrypted else
                               "The listener needs a passphrase, and this feed has none. Enter the same passphrase.")
                elif "BADSECRET" in l or "Wrong passphrase" in l:
                    refused = "The passphrase does not match the listener's. Check it on both sides."
                else:
                    refused = "The listener refused the connection: " + reason
        if refused and connected:
            refused = ""
        running = self.running()
        events, connects, closes, totals = _plain_events(lines)
        readable = _readable(lines)
        dropping = running and closes >= 3 and closes >= 0.8 * connects
        dup_lines = sum(1 for l in lines if "dup!" in l or "drop!" in l)
        return {"readable": readable, "events": events, "connects": connects, "closes": closes, "totals": totals, "dropping": dropping,
                "dup_lines": dup_lines, "link": link,
                "stats": self._last_stats() if running or connected else None,
                "running": running, "connected": connected, "elapsed_s": int(time.time() - self.started)
                if running and self.started else 0, "mode": self.mode, "host_port": self.host_port,
                "log_tail": lines[-14:], "raw_lines": lines[-400:], "log_lines": len(lines), "refused": refused}

    def _video_out(self):
        """The video as it leaves this panel: the source's format, or the re-encoded one when re-encoding."""
        info = clip_video(self.clip_path)
        if not info or self.encode_copy:
            return info
        info = dict(info)
        info["codec"] = "H.264 (re-encoded)"
        return info

    def _with_frame_alert(self, stats, dup_lines):
        """Adds a warning when ffmpeg is repeating or dropping frames to reach the chosen frame rate."""
        if stats is None or dup_lines < 20:
            return stats
        stats = dict(stats)
        stats["alerts"] = list(stats.get("alerts", [])) + [
            "ffmpeg is repeating or dropping frames to reach the frame rate you chose (%d times in the recent log). "
            "The picture will judder or freeze on the other side. Choose Same as the source, or a frame rate that divides "
            "evenly from the source." % dup_lines]
        return stats

    def _gop_text(self):
        """Keyframe spacing: measured from the source when it is sent as it is, or set by the encoder."""
        if not self.clip_path:
            return ""
        if self.encode_copy:
            fps = clip_video(self.clip_path)
            return clip_gop_text(self.clip_path, fps["fps"] if fps else "")
        return "1.0 s, set by the encoder"

    def _find_peer(self, host):
        """Who is at the other end: the owner of the address, from ipinfo.io. Runs in the background."""
        info = lookup_peer(host)
        if self.running():
            self.peer = info

    def status(self):
        prog = self._progress()
        if prog["connected"] and prog.get("link") == "up":
            self.notice = ""
        if not prog["running"] and self.clip and not self.want:
            # the feed ended on its own (not stopped from the panel): say why, in the reader's words
            self.error = "The feed stopped." + (" Last message: " + prog["log_tail"][-1] if prog["log_tail"] else "")
            self.clip = self.target = ""
        return {"running": prog["running"], "clip": self.clip, "target": self.target, "error": self.error,
                "notice": self.notice if self.want else "", "restarts": len(self.restarts),
                "resume_at": _fmt_clock(self.resume_at) if self.resume_at else "", "rate_max": cpu_profile()[2], "rate_note": cpu_note(),
                "make": dict(self.make), "samples_dir": self.samples_dir, "clips": clips(self.samples_dir),
                "clip_labels": {c: clip_label(c) for c in clips(self.samples_dir)},
                "connected": prog["connected"], "elapsed_s": prog["elapsed_s"], "mode": prog["mode"],
                "host_port": prog["host_port"], "log_tail": prog["log_tail"], "raw_lines": prog["raw_lines"], "refused": prog["refused"],
                "loglevel": self.loglevel, "command": self.command, "size_mb": _estimated_mb(),
                "destinations": load_destinations(), "monitor_presets": load_presets(), "focus_request": FOCUS["n"], "stats": self._with_frame_alert(prog["stats"], prog["dup_lines"]), "events": prog["events"],
                "readable": prog["readable"][-400:], "connects": prog["connects"],
                "closes": prog["closes"], "totals": prog["totals"], "dropping": prog["dropping"],
                "encrypted": bool(self.encrypted and self.running()),
                "audio": clip_audio(self.clip_path) if self.clip_path and prog["running"] else None,
                "video": self._video_out() if self.clip_path and prog["running"] else None,
                "latency_set": self.latency_set, "peer": self.peer if prog["running"] else None,
                "wifi": self.wifi if prog["running"] else None,
                "latency_auto": self.latency_auto if prog["running"] else False,
                "wifi_short": self.wifi_short if prog["running"] else False,
                "wifi_forced": self.wifi_forced if prog["running"] else False,
                "network_wifi": network_detected(),
                "encode_label": self.encode_label if prog["running"] else "",
                "gop": self._gop_text() if prog["running"] else ""}

    def start(self, body, keep_log=False, note=None, resuming=False):
        if not resuming:
            monitor = _active_monitor_side()
            if monitor and port_clash(_side_of_feed(body), monitor):
                return False, ("Port %s is already in use by the Decode monitor on this PC. Choose a different port "
                               "for the feed." % monitor["port"])
        with self.lock:
            if self.running():
                return False, "A feed is already running. Stop it first."
            if resuming and not self.want:
                return False, "The feed was stopped."
            source = os.path.expanduser(str(body.get("source") or "").strip())
            if source:
                if not os.path.isfile(source):
                    return False, "That source file was not found. Check the path."
                clip, path = os.path.basename(source), source
            else:
                clip = os.path.basename(str(body.get("clip") or ""))
                if clip not in clips(self.samples_dir):
                    return False, "Choose a clip from the list. Make the clips first if the list is empty."
                path = os.path.join(self.samples_dir, clip)
            host_ip = str(body.get("host") or "").strip()
            on_wifi = True if body.get("on_wifi") else netlink.network_is_wifi(host_ip or None)
            try:
                typed = int(str(body.get("latency") or "").strip()) if str(body.get("latency") or "").strip() else None
            except ValueError:
                return False, "The latency must be a number of milliseconds."
            allow_low = bool(body.get("allow_low_wifi"))
            auto_latency = typed is None
            forced = False
            if on_wifi and not allow_low:
                # on Wi-Fi the latency is at least 5000 ms; only the override box allows a lower value
                if typed is None or typed < WIFI_MIN_LATENCY:
                    forced = typed is not None
                    typed = WIFI_MIN_LATENCY
                    auto_latency = True
                body = dict(body, latency=str(typed))
            elif auto_latency:
                body = dict(body, latency="500")
            self.wifi_short = bool(on_wifi and allow_low and typed is not None and typed < WIFI_MIN_LATENCY)
            uri, mode, problem = feed_uri(body)
            if problem:
                return False, problem
            self.wifi, self.latency_auto = on_wifi, auto_latency
            if not resuming:
                self.spike_watch = spikes.SpikeWatch()
            self.wifi_forced = forced
            clock_file = ""
            if body.get("clock"):
                fd, clock_file = tempfile.mkstemp(prefix="srtdebug-clock-", suffix=".txt")
                os.close(fd)
                _write_clock(clock_file)
                body = dict(body, _clock_file=clock_file)
            body = dict(body, _field_order=source_field_order(path))
            enc_args, enc_label, enc_problem = encode_plan(body)
            if enc_problem:
                return False, enc_problem
            auto_note = ""
            if enc_args == ["-c", "copy"]:
                gop_problem = source_gop_problem(path)
                if gop_problem:
                    enc_args, enc_label, _ = encode_plan({"encode": "h264", "clock": body.get("clock"),
                                                          "rate": body.get("rate"), "_clock_file": clock_file,
                                                          "_field_order": body["_field_order"]})
                    auto_note = gop_problem
            self.encode_label = (auto_note + " " + enc_label) if auto_note else enc_label
            self.encode_copy = enc_args == ["-c", "copy"]
            level = str(body.get("loglevel") or self.loglevel)
            if level not in LOG_LEVELS:
                level = "debug"
            try:
                start_at = max(0.0, float(body.get("start_at") or 0))
            except (TypeError, ValueError):
                start_at = 0.0
            if self.progpath and os.path.exists(self.progpath):
                os.remove(self.progpath)
            fd, self.progpath = tempfile.mkstemp(prefix="srtdebug-feed-pos-", suffix=".txt")
            os.close(fd)
            src = [shutil_which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", FFMPEG_LOGLEVEL[level],
                   "-nostats", "-progress", self.progpath, "-re"]
            if body.get("loop"):
                src += ["-stream_loop", "-1"]
            if start_at > 0:
                src += ["-ss", "%.1f" % start_at]
            src += ["-i", path] + enc_args + ["-f", "mpegts"] + FEED_MUX + ["-"]
            if self.log:
                self.log.close()
            if self.statspath and os.path.exists(self.statspath):
                os.remove(self.statspath)
            if keep_log and self.logpath and os.path.exists(self.logpath):
                # a level change or an automatic resume: the log carries on in the same file, with a line marking it
                self.log = open(self.logpath, "a")
                self.log.write((note or f"--- log level now {level}; reconnecting ---") + "\n")
                self.log.flush()
            else:
                if self.logpath and os.path.exists(self.logpath):
                    os.remove(self.logpath)
                fd, self.logpath = tempfile.mkstemp(prefix="srtdebug-feed-", suffix=".log")
                self.log = os.fdopen(fd, "w")
                self.view_offset = 0
            sfd, self.statspath = tempfile.mkstemp(prefix="srtdebug-feed-stats-", suffix=".json")
            self._stats_off = 0
            self._stats_last = None
            if not resuming:   # an automatic resume keeps the totals for the whole session
                self._stats_tot = {"bytes": 0, "packets": 0, "lost": 0, "retrans": 0, "dropped": 0, "recv": 0}
            os.close(sfd)
            self.run_offset = os.path.getsize(self.logpath) if self.logpath and os.path.exists(self.logpath) else 0
            self.link_pos, self.link_state = self.run_offset, ""
            self.command = feed_command_text(uri, path, bool(body.get("loop")), level, self.statspath, encode=enc_args)
            try:
                self.p1 = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                threading.Thread(target=_pump_lines, args=(self.p1.stderr, self.log, "ffmpeg: "), daemon=True).start()
                self.ts = tsanalyze.TSAnalyzer(start=time.monotonic())
                self.p2 = subprocess.Popen([shutil_which("srt-live-transmit") or core.SRT_BIN, "file://con", uri,
                                            "-chunk:1316", "-loglevel:" + level, "-stats-report-frequency:500",
                                            "-statsout:" + self.statspath, "-pf:json"],
                                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)
                threading.Thread(target=_forward, args=(self.p1.stdout, self.p2.stdin, self.ts), daemon=True).start()
            except OSError as e:
                self.error = f"Could not start the feed: {e}"
                return False, self.error
            self.started = time.time()
            self.mode = mode
            self.encrypted = bool(str(body.get("passphrase") or ""))
            self.loglevel = level
            self.last_body = dict(body)
            self.want = True
            self.gen += 1
            self.resume_at = start_at
            if not resuming:
                self.restarts = []
            threading.Thread(target=self._watch, args=(self.gen,), daemon=True).start()
            if clock_file:
                threading.Thread(target=_clock_writer, args=(clock_file, self.gen, self), daemon=True).start()
            self.host_port = uri.split("?")[0].replace("srt://", "")
            self.peer = None
            if mode == "caller":
                threading.Thread(target=self._find_peer, args=(self.host_port.rsplit(":", 1)[0],), daemon=True).start()
            self.clip = clip
            self.clip_path = path
            self.latency_set = None
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
                self.latency_set = int(q["latency"][0]) if q.get("latency") else None
            except (ValueError, KeyError):
                pass
            self.target = uri.split("?")[0] + ("  (" + mode + ")")
            self.error = ""
            return True, ""

    def set_loglevel(self, level):
        """Change the transmitter's log level. It is fixed when the transmitter starts, so a running feed
        restarts at the new level; the connection drops for a moment and the log carries on."""
        if level not in LOG_LEVELS:
            return False, "Choose warn, note, info or debug."
        if not self.running() or not self.last_body:
            self.loglevel = level
            return True, ""
        body = dict(self.last_body)
        body["loglevel"] = level
        self.stop()
        return self.start(body, keep_log=True)

    def stop(self):
        with self.lock:
            self.want = False     # the user asked for this; nothing resumes it
            self.gen += 1
            self.notice = ""
            self._kill_procs()
            self.clip = self.target = ""

    def _av_now(self):
        """Audio-video offset on the way out, in ms (positive: audio leads). Not shown yet: the measurement from
        the stream's timestamps still gives wrong values on some streams, so it stays off until it is checked."""
        if self.ts is None or not AV_MEASURE_ON:
            return None
        try:
            wins = list(self.ts.windows)[:-1][-5:]
        except RuntimeError:          # the window list changed while it was being read
            return None
        vals = [v for v in (self.ts._av_offset_ms(w) for w in wins) if v is not None]
        return round(statistics.median(vals), 1) if vals else None

    def _kill_procs(self):
        for p in (self.p2, self.p1):
            if p is not None and p.poll() is None:
                p.terminate()
        for p in (self.p2, self.p1):
            if p is not None:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    def _play_position(self):
        """Where the clip got to: the start point plus what ffmpeg has read, wrapped around when the clip loops."""
        try:
            with open(self.progpath) as f:
                found = re.findall(r"out_time_us=(\d+)", f.read())
        except (OSError, ValueError):
            found = []
        if not found:
            return self.resume_at
        secs = int(found[-1]) / 1e6
        dur = _clip_seconds(self.clip_path)
        if not dur:
            return round(self.resume_at + secs, 1)
        first_pass = max(dur - self.resume_at, 0.1)
        if secs < first_pass:
            return round(self.resume_at + secs, 1)
        return round((secs - first_pass) % dur, 1)

    def _watch(self, gen):
        """Runs while a feed is wanted. When the other side closes the session and does not answer again for a
        while, or the transmitter exits on its own, the feed is started again from where the clip had got to."""
        down_since = None
        last_bytes, still_since, seen_bytes = -1, time.time(), False
        while True:
            time.sleep(2)
            with self.lock:
                if gen != self.gen or not self.want:
                    return
                p1, p2 = self.p1, self.p2
            if p1 is not None and p1.poll() is not None and p1.returncode == 0:
                return   # the clip simply finished (not repeating): nothing to resume
            link = self._progress()["link"]
            # the log does not always say when the other side has gone; bytes that stop moving do
            sent = self._stats_tot["bytes"]
            if sent != last_bytes:
                last_bytes, still_since = sent, time.time()
                seen_bytes = seen_bytes or sent > 0
            stalled = seen_bytes and time.time() - still_since >= 10
            dead = p2 is None or p2.poll() is not None
            if link != "down" and not stalled and not dead:
                if link == "up":
                    down_since = None
                continue   # connected, or not connected yet: the status line already says which
            if down_since is None:
                down_since = time.time()
                self.notice = ("The other side closed the session. Reconnecting; if it does not answer within "
                               "10 seconds, the feed starts again from where the clip got to.")
            if dead or time.time() - down_since >= 10:
                self._resume(gen)
                return

    def _resume(self, gen):
        with self.lock:
            if gen != self.gen or not self.want:
                return
            now = time.time()
            self.restarts = [t for t in self.restarts if now - t < 600] + [now]
            if len(self.restarts) > 20:
                self.want = False
                self.notice = ""
                self._kill_procs()
                self.clip = self.target = ""
                self.error = ("The other side closed the session 20 times in 10 minutes, so the feed was stopped. "
                              "Check the other side, then press Start feed.")
                return
            pos = self._play_position()
            body = dict(self.last_body or {})
            body["start_at"] = pos
            self._kill_procs()
            self.notice = "Starting again from %s into the clip." % _fmt_clock(pos)
        note = "--- the other side closed the session; starting again from %s into the clip ---" % _fmt_clock(pos)
        ok, msg = self.start(body, keep_log=True, note=note, resuming=True)
        if not ok:
            with self.lock:
                self.want = False
                self.notice = ""
                self.error = msg or "The feed could not be started again."

    def start_making(self, folder):
        with self.lock:
            if self.make["state"] == "making":
                return
            self.samples_dir = folder
            self.make = {"state": "making", "error": ""}
        save_settings(samples_dir=folder)

        def job():
            import samples
            try:
                samples.make(folder, log=lambda m: None)
                state = {"state": "ready", "error": ""}
            except Exception as e:
                state = {"state": "error", "error": str(e)}
            with self.lock:
                self.make = state
        threading.Thread(target=job, daemon=True).start()


def shutil_which(name):
    import shutil
    return shutil.which(name)


def _clip_seconds(path):
    """Length of a clip in seconds, or 0.0 if it cannot be read."""
    try:
        out = subprocess.run([shutil_which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=10)
        return float(out.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _write_clock(path):
    """Writes the current time, to the millisecond, for the clock overlay. Replaced in one step so ffmpeg never reads half of it."""
    now = time.time()
    text = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)) + ".%03d UTC" % int((now % 1) * 1000)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def _clock_writer(path, gen, feeder):
    """Keeps the clock file current while its run is the current one, then removes it."""
    while feeder.gen == gen:
        _write_clock(path)
        time.sleep(0.02)
    for p in (path, path + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass


def _fmt_clock(secs):
    """Seconds as m:ss for the panel and the log."""
    secs = int(secs or 0)
    return "%d:%02d" % (secs // 60, secs % 60)


def _rate_text(fps):
    """'25' -> '25', '24000/1001' -> '23.976'."""
    named = {"24000/1001": "23.976", "30000/1001": "29.97", "60000/1001": "59.94"}
    if fps in named:
        return named[fps]
    return str(int(fps)) if "/" not in fps else fps


def clip_label(name):
    """A sentence for a sample clip in the list: the format in words, not the file name."""
    stem = name[:-3] if name.endswith(".ts") else name
    if stem == "all-formats":
        return "All formats in one clip (the format changes between clips)"
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import samples
        spec = next((x for x in samples.FORMATS if x[0] == stem), None)
    except Exception:
        spec = None
    if not spec:
        return name
    _, w, h, fps, codec, scan, order = spec
    codec_name = "H.264" if codec == "h264" else "MPEG-2"
    rate = _rate_text(fps)
    if scan == "i":
        if "/" in fps:
            n, d = fps.split("/")
            field_rate = _rate_text(f"{int(n) * 2}/{d}")
        else:
            field_rate = _rate_text(str(int(fps) * 2))
        shape = f"{h}i{field_rate}"
        frames = f"{rate} frames/s ({field_rate} fields/s)"
        scan_text = "Interlaced, " + ("top field first" if order == "tt" else "bottom field first")
    else:
        shape = f"{h}p{rate}"
        frames = f"{rate} frames/s"
        scan_text = "Progressive"
    return f"{shape}  ·  {scan_text}  ·  {w}×{h}  ·  {frames}  ·  {codec_name}"


def _estimated_mb():
    try:
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import samples
        return samples.estimated_mb()
    except Exception:
        return 0


_AUDIO_CACHE = {}
_VIDEO_CACHE = {}
_GOP_CACHE = {}


def source_gop(path):
    """Keyframe gaps in the first 30 seconds: (median, longest), or (None, None) when none are found."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+30",
                              "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    keys = []
    for line in out.stdout.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
            keys.append(float(parts[0]))
    gaps = [b - a for a, b in zip(keys, keys[1:]) if b > a]
    if not gaps:
        return None, None
    return statistics.median(gaps), max(gaps)


def source_gop_problem(path):
    """Why a source is re-encoded instead of sent as it is, or '' when its keyframes are at most one second apart.
    A receiver can wait a long time for a keyframe on a source with longer gaps, so the feed re-encodes it."""
    median, longest = source_gop(path)
    if median is None:
        return ("Not enough keyframes were found in the first 30 seconds to measure the spacing, so this will be "
                "re-encoded to H.264 with a keyframe every second.")
    if longest > 1.05:
        return ("This source has a keyframe every %.1f s (up to %.1f s apart), so it is re-encoded to H.264 with a "
                "keyframe every second." % (median, longest))
    return ""


def source_av_offset(path):
    """Video start minus audio start in ms (positive: audio leads), from the first timestamp of each stream, or None."""
    try:
        out = subprocess.run([shutil_which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_type,start_time", "-of", "json", path],
                             capture_output=True, text=True, timeout=20)
        streams = json.loads(out.stdout or "{}").get("streams", [])
        v = next(float(x["start_time"]) for x in streams if x.get("codec_type") == "video" and "start_time" in x)
        a = next(float(x["start_time"]) for x in streams if x.get("codec_type") == "audio" and "start_time" in x)
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        return None
    return (v - a) * 1000.0


def source_av_warning(ms):
    """A warning when a clip's audio and video start further apart than BT.1359 allows, or '' when they do not."""
    if ms is None or core.AV_DETECT_MS[0] <= ms <= core.AV_DETECT_MS[1]:
        return ""
    side = "audio leads" if ms > 0 else "audio lags"
    return ("The clip's audio and video start %+.0f ms apart (%s). The feed sends the clip's timing as it is, "
            "so viewers will see that offset." % (ms, side))


def clip_gop_text(path, fps_text):
    """The keyframe spacing in the source, measured from its first 30 seconds (cached per file)."""
    if not path or not os.path.isfile(path):
        return ""
    key = (path, os.path.getmtime(path))
    if key in _GOP_CACHE:
        return _GOP_CACHE[key]
    text = "No keyframes found in the first 30 seconds."
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+30",
                              "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60)
        keys = []
        for line in out.stdout.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
                keys.append(float(parts[0]))
        gaps = [b - a for a, b in zip(keys, keys[1:]) if b > a]
        if len(keys) == 1:
            text = "Only one keyframe in the first 30 seconds, so the interval is longer than that."
        if gaps:
            gap = statistics.median(gaps)
            try:
                fps = float(eval(fps_text)) if "/" in str(fps_text) else float(fps_text)
            except (ValueError, SyntaxError, ZeroDivisionError):
                fps = 0
            frames = f", {round(gap * fps)} frames" if fps else ""
            steady = "" if max(gaps) - min(gaps) < 0.1 else " (spacing varies)"
            text = f"{gap:.1f} s{frames}, from the source{steady}"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        text = "Could not read the keyframes of this clip."
    _GOP_CACHE.clear()
    _GOP_CACHE[key] = text
    return text


def _fps_text(rate):
    """'25/1' -> '25', '30000/1001' -> '29.97'."""
    n, _, d = str(rate or "0/1").partition("/")
    try:
        value = int(n) / (int(d) if d else 1)
    except (ValueError, ZeroDivisionError):
        return "?"
    for exact, text in ((24000 / 1001, "23.976"), (30000 / 1001, "29.97"), (60000 / 1001, "59.94")):
        if abs(value - exact) < 0.01:
            return text
    return ("%.3f" % value).rstrip("0").rstrip(".")


def clip_video(path):
    """The video format of the clip being sent, read once per file (the feed copies it unchanged)."""
    if not path or not os.path.isfile(path):
        return None
    key = (path, os.path.getmtime(path))
    if key in _VIDEO_CACHE:
        return _VIDEO_CACHE[key]
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=codec_name,profile,width,height,avg_frame_rate,display_aspect_ratio,field_order",
                              "-of", "json", path], capture_output=True, text=True, timeout=20)
        st = (json.loads(out.stdout or "{}").get("streams") or [None])[0]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        st = None
    if not st:
        info = None
    else:
        scans = {"progressive": "Progressive", "tt": "Interlaced, top field first", "tb": "Interlaced, top field first",
                 "bb": "Interlaced, bottom field first", "bt": "Interlaced, bottom field first"}
        info = {"codec": str(st.get("codec_name", "?")).upper() + (" " + st["profile"] if st.get("profile") else ""),
                "resolution": "%sx%s" % (st.get("width", "?"), st.get("height", "?")),
                "fps": _fps_text(st.get("avg_frame_rate")),
                "aspect": str(st.get("display_aspect_ratio") or "Not stated"),
                "scan": scans.get(st.get("field_order"), "Not stated")}
    _VIDEO_CACHE.clear()
    _VIDEO_CACHE[key] = info
    return info


def clip_audio(path):
    """The audio format of the clip being sent, read once per file (the feed copies it unchanged)."""
    if not path or not os.path.isfile(path):
        return None
    key = (path, os.path.getmtime(path))
    if key in _AUDIO_CACHE:
        return _AUDIO_CACHE[key]
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                              "stream=codec_name,sample_rate,channels,channel_layout,bit_rate",
                              "-of", "json", path], capture_output=True, text=True, timeout=20)
        st = (json.loads(out.stdout or "{}").get("streams") or [None])[0]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        st = None
    if not st:
        info = {"codec": "", "sample_rate": 0, "channels": 0, "layout": "", "bitrate_kbps": 0}
    else:
        info = {"codec": str(st.get("codec_name", "?")).upper(), "sample_rate": int(st.get("sample_rate") or 0),
                "channels": int(st.get("channels") or 0), "layout": str(st.get("channel_layout") or ""),
                "bitrate_kbps": int(st["bit_rate"]) // 1000 if st.get("bit_rate") else 0}
    _AUDIO_CACHE.clear()
    _AUDIO_CACHE[key] = info
    return info


def clip_metadata(path):
    """Everything ffprobe and a short PCR sample can tell about a clip, as 'name: value' lines."""
    name = os.path.basename(path)
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                             capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], "Could not read this clip. Check that ffprobe is installed, or make the clips again."
    if out.returncode != 0 or not data:
        return [], "This clip could not be read. Make the clips again."
    fmt = data.get("format", {})
    lines = [f"File: {name}"]
    try:
        lines.append(f"Size: {int(fmt.get('size', 0)) / 1e6:.1f} MB")
        lines.append(f"Length: {float(fmt.get('duration', 0)):.1f} s")
        lines.append(f"Container: {fmt.get('format_long_name', fmt.get('format_name', '?'))}")
        lines.append(f"Total bitrate: {int(fmt.get('bit_rate', 0)) / 1e6:.2f} Mb/s")
    except (TypeError, ValueError):
        pass
    field_names = {"progressive": "progressive", "tt": "interlaced, top field first",
                   "bb": "interlaced, bottom field first", "tb": "interlaced, top field first",
                   "bt": "interlaced, bottom field first"}
    for st in data.get("streams", []):
        if st.get("codec_type") == "video":
            prof = f" ({st['profile']})" if st.get("profile") else ""
            lines.append(f"Video: {st.get('codec_long_name', st.get('codec_name', '?'))}{prof}")
            lines.append(f"Resolution: {st.get('width')}x{st.get('height')}")
            num, _, den = str(st.get("avg_frame_rate", "0/0")).partition("/")
            lines.append(f"Frame rate: {_rate_text(num if den in ('', '1', '0') else num + '/' + den)} frames/s")
            lines.append(f"Scan: {field_names.get(st.get('field_order'), 'not stated')}")
            lines.append(f"Pixel format: {st.get('pix_fmt', '?')}")
            if st.get("display_aspect_ratio"):
                lines.append(f"Picture aspect: {st['display_aspect_ratio']}")
        elif st.get("codec_type") == "audio":
            lines.append(f"Audio: {st.get('codec_long_name', st.get('codec_name', '?'))}")
            lines.append(f"Audio sample rate: {st.get('sample_rate', '?')} Hz")
            lines.append(f"Audio channels: {st.get('channels', '?')} ({st.get('channel_layout', '?')})")
            if st.get("bit_rate"):
                lines.append(f"Audio bitrate: {int(st['bit_rate']) // 1000} kb/s")
    try:
        with open(path, "rb") as f:
            data_head = f.read(8 * 1024 * 1024)
        last, gaps = {}, []
        for off in range(0, len(data_head) - 187, 188):
            p_ = data_head[off:off + 188]
            if p_[0] != 0x47:
                continue
            pid = ((p_[1] & 0x1F) << 8) | p_[2]
            if ((p_[3] >> 4) & 3) not in (2, 3) or p_[4] < 7 or not (p_[5] & 0x10):
                continue
            pcr = (int.from_bytes(p_[6:11], "big") >> 7) * 300 + (int.from_bytes(p_[10:12], "big") & 0x1FF)
            if pid in last:
                gaps.append((pcr - last[pid]) / 27_000)
            last[pid] = pcr
        if gaps:
            gaps.sort()
            lines.append(f"PCR interval: median {gaps[len(gaps) // 2]:.1f} ms, longest {gaps[-1]:.1f} ms "
                         f"(first 8 MB)")
    except OSError:
        pass
    return lines, ""


def clips(folder=None):
    """Clip names in the samples folder that the feed may send."""
    try:
        return sorted(f for f in os.listdir(folder or SAMPLES_DIR) if f.endswith(".ts"))
    except OSError:
        return []


def browse_for_folder(start):
    """Opens a native folder window on this computer. Falls back to a typed path when none can open."""
    code = ("import sys, tkinter as tk\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
            "print(filedialog.askdirectory(initialdir=sys.argv[1], title='Choose a folder for the test clips') or '')\n")
    try:
        out = subprocess.run([sys.executable, "-c", code, start], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "dir": "", "error": "The folder window did not open. Type the folder path instead."}
    if out.returncode != 0:
        return {"ok": False, "dir": "", "error": "A folder window cannot open on this computer. Type the folder path instead."}
    return {"ok": True, "dir": out.stdout.strip(), "error": ""}


_PEER_CACHE = {}


def lookup_peer(host):
    """Who owns the address the feed goes to. Reverse DNS on this computer, then ipinfo.io (cached per address)."""
    if host in _PEER_CACHE:
        return _PEER_CACHE[host]
    info = {"ip": host, "rdns": "", "org": "", "city": "", "region": "", "country": "", "error": ""}
    try:
        info["ip"] = socket.gethostbyname(host)
    except OSError:
        info["error"] = "The address could not be resolved."
        _PEER_CACHE[host] = info
        return info
    try:
        info["rdns"] = socket.gethostbyaddr(info["ip"])[0]
    except OSError:
        pass
    try:
        req = urllib.request.Request("https://ipinfo.io/" + info["ip"] + "/json",
                                     headers={"User-Agent": "srtdebug", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        for k in ("org", "city", "region", "country"):
            info[k] = str(d.get(k, "") or "")
        if d.get("bogon"):
            info["error"] = "This is a private address, so no owner is listed."
    except (OSError, ValueError):
        info["error"] = "The owner of this address could not be looked up (ipinfo.io did not answer)."
    _PEER_CACHE[host] = info
    return info


UPLOAD_DIR = os.path.join(os.path.expanduser("~"), ".srtdebug", "uploads")
UPLOAD_MAX = 4 * 1024 ** 3
UPLOAD_EXT = (".mp4", ".m4v", ".mov", ".mkv", ".ts", ".m2ts", ".mts", ".mxf", ".avi")
SEND_VIDEO = ("h264", "hevc", "mpeg2video", "mpeg4")


def check_sendable(path):
    """Is this file one the feed can send? Checks the video codec, then packs three seconds the same way the feed does."""
    lines, err = clip_metadata(path)
    if err:
        return lines, "This file could not be read as a video. Choose a complete video file."
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                              "-of", "json", path], capture_output=True, text=True, timeout=30)
        streams = json.loads(out.stdout or "{}").get("streams", [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return lines, "Could not read this file. Check that it is a complete video file."
    video = [st for st in streams if st.get("codec_type") == "video"]
    if not video:
        return lines, "This file has no video. Choose a video file."
    codec = str(video[0].get("codec_name", ""))
    if codec not in SEND_VIDEO:
        return lines, ("The video is " + codec.upper() + ", which the feed cannot send. "
                       "Use H.264, H.265 (HEVC) or MPEG-2 video.")
    try:
        test = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-t", "3", "-i", path, "-map", "0:v:0",
                               "-map", "0:a?", "-c", "copy", "-f", "mpegts", "-"],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return lines, "The file could not be checked in time. Try a smaller file."
    if test.returncode != 0:
        detail = (test.stderr.decode("utf-8", "replace").strip().splitlines() or ["unknown error"])[-1]
        return lines, "The file could not be packed for sending: " + detail
    return lines, ""


def _drain(stream, length):
    """Reads and discards the request body, so the reply can be read by the browser."""
    left = max(0, min(length, UPLOAD_MAX + 1))
    while left > 0:
        chunk = stream.read(min(1024 * 1024, left))
        if not chunk:
            break
        left -= len(chunk)


SEND_TYPES_TEXT = "MP4, M4V, MOV, MKV, TS, M2TS, MTS, MXF or AVI"


def save_upload(name, stream, length):
    """Writes an uploaded video into the uploads folder, then checks it. Returns (path, lines, error)."""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or ""))[:120]
    if not base or not base.lower().endswith(UPLOAD_EXT):
        _drain(stream, length)
        return "", [], "\"" + (base or "This file") + "\" is not a video file this tool can send. Use " + SEND_TYPES_TEXT + ".", ""
    if length <= 0 or length > UPLOAD_MAX:
        _drain(stream, length)
        return "", [], "The file must be between 1 byte and 4 GB.", ""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, base)
    part = path + ".part"
    left = length
    with open(part, "wb") as f:
        while left > 0:
            chunk = stream.read(min(1024 * 1024, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left:
        os.remove(part)
        return "", [], "The upload was cut short. Try again.", ""
    os.replace(part, path)
    lines, err = check_sendable(path)
    if err:
        os.remove(path)
        return "", lines, err, ""
    median, longest = source_gop(path)
    if median is not None:
        lines.append("Keyframes: every %.1f s%s" % (median, " (spacing varies)" if longest - median > 0.1 else ""))
    warn = source_gop_problem(path)
    return path, lines, "", warn


FEEDER = Feeder()
SESSION = None                       # the monitor session, set in run()
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _active_feed_side():
    """The feed's port side, while it is sending."""
    if not FEEDER.want or not FEEDER.last_body:
        return None
    return _side_of_feed(FEEDER.last_body)


def _active_monitor_side():
    """The monitor's port side, while it is running."""
    s = SESSION
    if s is None or s.proc is None or s.proc.poll() is not None or not s.cfg:
        return None
    return _side_of_monitor(s.cfg)


def _our_pids():
    """Processes this panel started (the feed's two programs and the monitor), for the meter's own share."""
    procs = [FEEDER.p1, FEEDER.p2, SESSION.proc if SESSION is not None else None]
    return [p.pid for p in procs if p is not None and p.poll() is None]


METER = sysmeter.Meter(pids=_our_pids)


def _host_port_of_url(url):
    m = re.match(r"^srt://([^:/?]*):(\d+)", str(url or "").strip(), re.I)
    return (m.group(1), int(m.group(2))) if m else ("", None)


def _side_of_feed(body):
    """The feed's port on this PC, as (mode, port, host), from the fields the panel sends."""
    url = str(body.get("url") or "").strip()
    if url:
        host, port = _host_port_of_url(url)
        query = parse_qs(url.split("?", 1)[1]) if "?" in url else {}
        # a URL with mode= says which side it is; with no host it can only be a listener
        mode = (query.get("mode") or [""])[0].lower() or ("listener" if not host else "caller")
    else:
        host = str(body.get("host") or "").strip()
        try:
            port = int(body.get("port")) if str(body.get("port") or "").strip() else None
        except (TypeError, ValueError):
            port = None
        mode = str(body.get("mode") or "caller")
    return {"mode": mode, "port": port, "host": host}


def _side_of_monitor(cfg):
    return {"mode": cfg.get("mode") or "listener", "port": int(cfg.get("port") or 0), "host": cfg.get("host") or ""}


def port_clash(a, b):
    """True when two sides would use the same port on this PC: both listen on it, or one sends to this PC
    (a loopback address) on the port the other listens on. Two senders, or a sender to another machine, are fine."""
    if not a or not b or a["port"] is None or b["port"] is None or a["port"] != b["port"]:
        return False
    if a["mode"] == "listener" and b["mode"] == "listener":
        return True
    for x, y in ((a, b), (b, a)):
        if x["mode"] == "caller" and x["host"].lower() in LOOPBACK and y["mode"] == "listener":
            return True
    return False


# The panel quits by itself when no browser window has been looking at it for a while, so a monitor or
# encode cannot keep running unnoticed. The page asks for its status every two seconds, so an open window
# keeps it alive. (The two environment settings are for testing; the defaults are three minutes and two minutes.)
IDLE_QUIT_S = float(os.environ.get("SRTDEBUG_IDLE_QUIT_S", "180"))         # after a window has been seen
FIRST_WINDOW_S = float(os.environ.get("SRTDEBUG_FIRST_WINDOW_S", "120"))   # time allowed for the first window
WINDOW_SEEN = {"last": None, "started": time.monotonic()}
WINDOW_OPEN_S = 90        # a window that has checked in this recently is still open (background tabs check in about once a minute)
FOCUS = {"n": 0}          # counts launch requests made while a window is open; the open page brings itself forward on each


def _window_open():
    last = WINDOW_SEEN["last"]
    return last is not None and time.monotonic() - last < WINDOW_OPEN_S


def _window_seen():
    WINDOW_SEEN["last"] = time.monotonic()


def _watch_for_window():
    """Quit, stopping any feed or monitor, when no window has checked in for too long."""
    while True:
        time.sleep(5)
        last = WINDOW_SEEN["last"]
        now = time.monotonic()
        if last is None:
            idle, limit = now - WINDOW_SEEN["started"], FIRST_WINDOW_S
        else:
            idle, limit = now - last, IDLE_QUIT_S
        if idle > limit:
            # a laptop waking from sleep: give the open window a moment to check in before quitting
            time.sleep(min(30.0, limit))
            last = WINDOW_SEEN["last"]
            if last is None or time.monotonic() - last > limit:
                print("No browser window is open any more, so srtdebug is quitting.", flush=True)
                os.kill(os.getpid(), signal.SIGTERM)
                return


def run(a):
    global SESSION
    session = Session()
    SESSION = session
    WINDOW_SEEN["started"] = time.monotonic()
    METER.start()
    threading.Thread(target=_watch_for_window, daemon=True).start()
    try:
        server = http.server.ThreadingHTTPServer((a.bind, a.port), _handler(session))
    except OSError as e:
        raise SystemExit(f"error: cannot serve on {a.bind}:{a.port} ({e.strerror or e}). "
                         f"Pick another port with --port.")
    shown = "localhost" if a.bind in ("127.0.0.1", "localhost") else a.bind
    print(core.paint(f"Control panel: http://{shown}:{a.port}/   (Ctrl-C to quit)", core.GREEN))
    if a.bind not in ("127.0.0.1", "localhost"):
        print(core.paint("Warning: anyone who can reach this address can start and stop monitoring.",
                         core.YELLOW))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        server.server_close()
    return 0
