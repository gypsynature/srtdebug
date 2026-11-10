#!/usr/bin/env python3
"""
srtdebug - live inspector for SRT streams, built on srt-live-transmit.

  srtdebug check   --mode listener --port 9000 [--selftest]
  srtdebug monitor --mode caller --host 10.0.0.5 --port 9000 [options]

Stdlib only. Requires the srt-tools package (provides srt-live-transmit).
"""

import argparse
import csv
import http.server
import ipaddress
import math
import io
import json
import os
import re
import shutil
import spikes
import statistics
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from urllib.parse import quote

VERSION = "3.0"
# SRT's own default latency is 120 ms. 20 ms is the lowest value that works on a short path:
# SRT recommends at least 3-4 x RTT, so it only suits links with RTT under about 6 ms.
DEFAULT_LATENCY_MS = 20
PASSPHRASE_ENV = "SRTDEBUG_PASSPHRASE"
PASSPHRASE_LEN = (10, 80)

SRT_BIN = "srt-live-transmit"
# srt-live-transmit only accepts file://con (stdin/stdout) for file-type URIs.
# The payload is discarded by sending stdout to /dev/null; we only measure it.
SINK = "file://con"
TTY = sys.stdout.isatty() and "NO_COLOR" not in os.environ

# Health thresholds: defaults and saved overrides live in thresholds.py (edit them in the control
# panel's Thresholds card, or in thresholds.json).
import thresholds as _thresholds
import netlink
import usage as _usage
import sysmeter


def _pc_ram_mb():
    """Total RAM of this PC in MB, or None where it cannot be read (not Linux)."""
    try:
        return sysmeter._mem()[1] * 1024.0
    except Exception:
        return None
_T = _thresholds.load()
LOSS_WARN_PCT, LOSS_BAD_PCT = _T["loss_warn_pct"], _T["loss_bad_pct"]
# Loss that was detected and then retransmitted in time is repaired, so it is information, not a
# warning. What counts is packets the receive buffer gave up on: any of them warns, and a rate
# above UNRECOVERED_FAIL_PCT fails.
UNRECOVERED_FAIL_PCT = _T["unrecovered_fail_pct"]
RTT_WARN_MS, RTT_BAD_MS = _T["rtt_warn_ms"], _T["rtt_bad_ms"]

GREEN, YELLOW, RED, CYAN, BOLD, DIM = "32", "33", "31", "36", "1", "2"
KEYS = ["rx_bytes", "rx_packets", "lost", "retrans", "dropped"]
ROW_FIELDS = [
    "time", "elapsed_s", "bitrate_mbps", "rx_packets", "lost", "loss_pct",
    "retrans", "dropped", "belated", "rtt_ms", "buf_ms", "latency_ms", "link_bw_mbps", "flow_window",
]
PAYLOAD_FIELDS = ["mux_mbps", "video_mbps", "audio_mbps", "null_pct", "cc_errors",
                  "pcr_disc", "pcr_back", "table_changes", "av_offset_ms"]
PROBE_BYTES = 16 * 1024 * 1024   # stream kept in memory for ffprobe snapshots (about 6 s at 20 Mbps)
CONTENT_WINDOW_S = 4.0           # audio and video content checks look at this much of the newest stream
CONTENT_EVERY_S = 15.0           # and run every this many seconds (ffmpeg detectors, a few seconds of CPU)
PROBE_WINDOW_S = 3.0             # ffprobe reads only this much of the newest stream; older bytes would
                                 # hide a change: it takes its parameters from the first SPS it finds
PROBE_FIRST_S = 4.0             # first codec probe after this many seconds
PROBE_EVERY_S = 15.0            # then every this many seconds


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if TTY else str(text)


def loss_color(pct):
    return RED if pct >= LOSS_BAD_PCT else YELLOW if pct >= LOSS_WARN_PCT else GREEN


# A/V limits from ITU-R BT.1359 (same as the dashboard)
AV_DETECT_MS = (-125.0, 45.0)
AV_ACCEPT_MS = (-185.0, 90.0)


def av_text(ms):
    """'+23 ms (audio leads)', coloured by the A/V limits. Positive means video is stamped later."""
    if ms is None:
        return None
    side = "audio leads" if ms > 1 else "audio lags" if ms < -1 else "in step"
    text = f"{ms:+.0f} ms ({side})"
    if ms > AV_ACCEPT_MS[1] or ms < AV_ACCEPT_MS[0]:
        return paint(text, RED)
    if ms > AV_DETECT_MS[1] or ms < AV_DETECT_MS[0]:
        return paint(text, YELLOW)
    return text


def rtt_color(ms):
    return RED if ms >= RTT_BAD_MS else YELLOW if ms >= RTT_WARN_MS else GREEN


def fmt_hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def run(args, timeout=5):
    """Run a command; return (returncode or None, combined output)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def add_crypto_args(p):
    p.add_argument("--passphrase", nargs="?", const="", default=None, metavar="TEXT",
                   help="encrypt the stream (10-80 characters, must match the sender). "
                        "Leave TEXT out to be prompted, which keeps it out of shell history")
    p.add_argument("--passphrase-env", metavar="VAR",
                   help="read the passphrase from environment variable VAR")
    p.add_argument("--pbkeylen", type=int, choices=[16, 24, 32],
                   help="AES key length in bytes: 16 (AES-128), 24 or 32 (default: SRT's default)")


def resolve_passphrase(a):
    """Return the passphrase for this run, or None. Validates the length and never prints it."""
    env_name = getattr(a, "passphrase_env", None)
    value = getattr(a, "passphrase", None)
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            raise SystemExit(f"error: environment variable {env_name} is empty or not set")
    elif value is None:
        return None
    elif value == "":
        if not sys.stdin.isatty():
            raise SystemExit("error: --passphrase needs a value when not run from a terminal "
                             "(or use --passphrase-env)")
        import getpass
        value = getpass.getpass("SRT passphrase (10-80 characters, hidden): ")
    low, high = PASSPHRASE_LEN
    if not low <= len(value) <= high:
        raise SystemExit(f"error: the passphrase must be {low} to {high} characters")
    return value


def build_uri(mode, host, port, latency=None, streamid=None, passphrase=None, pbkeylen=None):
    if mode == "caller":
        if not host:
            raise SystemExit("error: caller mode needs --host (the remote listener)")
        base = f"srt://{host}:{port}"
    else:
        base = f"srt://{host or '0.0.0.0'}:{port}"
    params = [f"mode={mode}"]
    if latency is not None:
        params.append(f"latency={latency}")
    if streamid:
        params.append("streamid=" + quote(streamid, safe=""))
    if passphrase:
        params.append("passphrase=" + quote(passphrase, safe=""))
        if pbkeylen:
            params.append(f"pbkeylen={pbkeylen}")
    return base + "?" + "&".join(params)


def parse_sample(d):
    recv = d.get("recv", {})
    link = d.get("link", {})
    return {
        "t": d.get("time", 0) / 1000.0,  # ms since srt-live-transmit started
        "rx_bytes": recv.get("bytes", 0),
        "rx_packets": recv.get("packets", 0),
        "lost": recv.get("packetsLost", 0),
        "retrans": recv.get("packetsRetransmitted", 0),
        "dropped": recv.get("packetsDropped", 0),
        "belated": recv.get("packetsBelated", 0),
        "rtt": link.get("rtt", 0),
        "link_bw": link.get("bandwidth", 0),
        "buf_ms": recv.get("msBuf", 0),
        # negotiated receiver delay: SRT uses the larger of the two sides' latency settings
        "tsbpd": recv.get("msTsbPdDelay", 0),
        "window": d.get("window", {}).get("flow", 0),
    }


# ----------------------------------------------------------------- check

def _check_ufw(add, port):
    if not shutil.which("ufw"):
        return
    _, out = run(["ufw", "status", "verbose"])
    if "Status:" not in out:
        add("warn", "ufw", "present but status unreadable (run with sudo)")
    elif "Status: inactive" in out:
        add("ok", "ufw", "inactive, not filtering")
    elif re.search(rf"^{port}(/udp)?\s.*ALLOW", out, re.M):
        add("ok", "ufw", f"active, rule allows UDP {port}")
    elif re.search(r"Default:\s*deny \(incoming\)", out):
        add("fail", "ufw", f"active with default deny; no rule allows UDP {port}",
            f"sudo ufw allow {port}/udp")
    else:
        add("ok", "ufw", "active, default allows incoming")


def _check_iptables(add, port):
    if not shutil.which("iptables"):
        return
    rc, out = run(["iptables", "-S"])
    if rc != 0:
        add("warn", "iptables", "rules unreadable (run with sudo)")
        return
    policy = re.search(r"^-P INPUT (\w+)", out, re.M)
    verdicts = []
    for line in out.splitlines():
        if re.search(rf"--dport {port}(\s|$)", line):
            t = re.search(r"-j (\w+)", line)
            if t:
                verdicts.append(t.group(1))
    if "DROP" in verdicts or "REJECT" in verdicts:
        add("fail", "iptables", f"a rule drops or rejects port {port}",
            "review with: sudo iptables -S")
    elif "ACCEPT" in verdicts:
        add("ok", "iptables", f"rule allows port {port}")
    elif policy and policy.group(1) != "ACCEPT":
        add("fail", "iptables", f"INPUT policy is {policy.group(1)} and no rule opens port {port}",
            f"sudo iptables -I INPUT -p udp --dport {port} -j ACCEPT")
    else:
        add("ok", "iptables", "default policy accepts input")


CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")  # shared address space (carrier NAT, overlays)


def _route_source_ip():
    """The address this PC uses to reach the internet. Only a route lookup; no packet is sent."""
    _, out = run(["ip", "-4", "route", "get", "1.1.1.1"])
    m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", out)
    if m:
        return m.group(1)
    _, out = run(["hostname", "-I"])
    ips = out.split()
    return ips[0] if ips else None


def _vpn_interfaces():
    _, out = run(["ip", "-o", "link", "show"]) if shutil.which("ip") else run(["ifconfig", "-l"])
    names = re.findall(r"^\d+: ([\w.@-]+):", out, re.M) or out.split()
    return sorted({n.split("@")[0] for n in names
                   if re.match(r"(tun|wg|tailscale|ppp|nordlynx|proton)\d*", n)})


def listener_network(add, port):
    """Explain what the network path allows for a listener. Reads local state only; it cannot see the
    router, so the last step is always a test from outside the network."""
    ip = _route_source_ip()
    if not ip:
        add("warn", "network address", "no network address found", "check that this PC is connected")
        return
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        addr = None
    if addr is not None and addr.version == 4 and addr in CGNAT_RANGE:
        add("fail", "network address",
            f"{ip} is a shared address (carrier-grade NAT or an overlay network such as Tailscale); "
            f"a router cannot forward UDP {port} to it from the internet",
            "ask your ISP for a public IPv4 address, or have the sender join the same overlay network")
    elif addr is not None and addr.is_private:
        add("warn", "network address",
            f"{ip} is a private (LAN) address: this PC is behind a router. Senders outside this network "
            f"need UDP {port} forwarded on the router to {ip}",
            f"router: forward UDP {port} to {ip}, then allow it in this PC's firewall and test from outside")
    elif addr is not None:
        add("ok", "network address", f"{ip} is a public address; no router is in the way")
    add("info", "port forwarding",
        f"cannot be verified from this PC. Test it from outside your network: a sender on mobile data "
        f"(or a friend's connection) points at this address and port {port}",
        f"send from outside to {ip}:{port} with mode caller, then watch the monitor for packets")


def vpn_check(add, mode, peer_ip=None):
    """Is a VPN carrying this PC's traffic? Looks at the route the traffic takes (the default route, or the
    route to the peer in caller mode), not just at whether a VPN adapter exists."""
    via = netlink.route_interface(peer_ip) if peer_ip else netlink.default_interface()
    win = netlink.windows_route(peer_ip) if netlink.is_wsl() else None
    if win and netlink.is_vpn_adapter(*win):
        # WSL: the VPN runs on Windows, so the Linux side cannot see it; Windows decides the route
        if mode == "listener":
            add("fail", "VPN",
                f"Windows sends traffic through {win[1] or win[0]}, a VPN. Senders on the internet cannot "
                f"reach a listener inside a VPN tunnel",
                "disconnect the VPN before listening, or send from a sender on the same VPN")
        else:
            add("warn", "VPN",
                f"Windows sends traffic to {peer_ip or 'the remote host'} through {win[1] or win[0]}, a VPN. "
                f"Many VPNs block or slow SRT's UDP, so the connection may fail",
                "disconnect the VPN, or test without it")
    elif netlink.is_vpn_interface(via):
        if mode == "listener":
            add("fail", "VPN",
                f"traffic leaves through {via}, a VPN. Senders on the internet cannot reach a listener "
                f"inside a VPN tunnel",
                "disconnect the VPN before listening, or send from a sender on the same VPN")
        else:
            add("warn", "VPN",
                f"traffic to {peer_ip or 'the remote host'} leaves through {via}, a VPN. Many VPNs block "
                f"or slow SRT's UDP, so the connection may fail",
                "disconnect the VPN, or test without it")
    else:
        if win:
            add("info", "VPN", f"Windows sends this traffic through {win[1] or win[0]}; no VPN detected on that route")
        vpns = _vpn_interfaces()
        if vpns:
            add("info", "VPN", f"{', '.join(vpns)} is present but not carrying this traffic")


def wifi_check(add, mode, peer_ip=None):
    """Is this PC's traffic on Wi-Fi? Says why srtdebug uses a longer latency there."""
    if not netlink.network_is_wifi(peer_ip):
        return
    add("info", "Wi-Fi",
        "this PC's traffic goes over Wi-Fi. Wi-Fi drops packets in short bursts and SRT needs time to resend "
        "them, so srtdebug sets the latency to 5000 ms here. A shorter latency turns those bursts into frozen "
        "pictures on the other side",
        "a wired connection is the most reliable. Set the latency yourself under Custom settings once you have "
        "tested the link")


def check_environment(mode, host, port):
    """Return a list of (level, title, detail, fix). Levels: ok, warn, fail, info."""
    res = []

    def add(level, title, detail, fix=None):
        res.append((level, title, detail, fix))

    if not 1 <= port <= 65535:
        add("fail", "port", f"{port} is not a valid port")
        return res
    if port < 1024 and os.geteuid() != 0:
        add("warn", "port", f"{port} is privileged (<1024); may need sudo")

    if not shutil.which(SRT_BIN):
        add("fail", SRT_BIN, "not installed", "sudo apt install srt-tools")
        return res
    _, out = run([SRT_BIN, "-version"])
    m = re.search(r"SRT Library version: ([\d.]+)", out)
    add("ok", SRT_BIN, f"found (SRT library {m.group(1) if m else 'unknown'})")

    if mode == "listener":
        _, out = run(["ss", "-H", "-ulpn"])
        hits = [l for l in out.splitlines() if re.search(rf":{port}\s", l)]
        if hits:
            who = re.search(r'users:\(\("([^"]+)"', hits[0])
            add("fail", f"UDP {port}",
                f"already in use by {who.group(1) if who else 'another process'}",
                "stop that process or choose another port")
        else:
            add("ok", f"UDP {port}", "free to listen on")
        _check_ufw(add, port)
        _check_iptables(add, port)
        listener_network(add, port)
        vpn_check(add, mode)
        wifi_check(add, mode)
    else:
        peer_ip = None
        if not host:
            add("fail", "remote host", "caller mode needs --host", None)
        else:
            try:
                peer_ip = socket.gethostbyname(host)
                add("ok", "remote host", f"{host} resolves to {peer_ip}")
            except OSError:
                add("fail", "remote host", f"cannot resolve {host}", "check the address or DNS")
        vpn_check(add, mode, peer_ip)
        wifi_check(add, mode, peer_ip)
        add("info", "note", "outbound UDP to the peer is not verifiable until connected")
    return res


def loopback_selftest(port):
    """Short SRT sender -> receiver pair on 127.0.0.1. Returns a result tuple."""
    title = "loopback test"
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", port))
        probe.close()
    except OSError:
        return ("warn", title, f"skipped: UDP {port} is busy (stop the listener first)", None)

    tmp = tempfile.mkdtemp(prefix="srtdebug-selftest-")
    stats = os.path.join(tmp, "rx.jsonl")
    try:
        rx = subprocess.Popen(
            [SRT_BIN, "-t", "6", "-s", "20", "-f", "-pf", "json", "-statsout", stats,
             f"srt://127.0.0.1:{port}?mode=listener", SINK],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        time.sleep(1.0)  # let the listener bind before the caller connects
        # Continuous pipe source: srt-live-transmit cannot poll raw device nodes.
        gen = subprocess.Popen(["yes", "srtdebug-selftest"], stdout=subprocess.PIPE)
        tx = subprocess.Popen(
            [SRT_BIN, "-t", "3", SINK, f"srt://127.0.0.1:{port}?mode=caller"],
            stdin=gen.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        gen.stdout.close()
        tx.wait(timeout=15)
        gen.kill()
        gen.wait(timeout=5)
        rx.wait(timeout=15)
        got = 0
        if os.path.exists(stats):
            with open(stats) as f:
                for line in f:
                    try:
                        got = json.loads(line).get("recv", {}).get("bytes", 0)
                    except ValueError:
                        pass
        if got > 0:
            return ("ok", title, f"{got / 1024:.0f} KB passed through SRT on loopback", None)
        err = (rx.stderr.read() or tx.stderr.read() or "").strip().splitlines()
        detail = "no data received" + (f": {err[-1]}" if err else "")
        return ("fail", title, detail, "run srt-live-transmit -v to see the SRT log")
    except (OSError, subprocess.TimeoutExpired) as e:
        return ("fail", title, f"could not run: {e}", None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def ask_port_forwarding(results, port):
    """Ask the operator whether the router forwards the port (terminal only). Returns the updated results."""
    if not sys.stdin.isatty():
        return results
    ip = next((d.split(",")[0].strip() for _, t, d, _ in results if t == "reachable at"), "this machine")
    print()
    print("Router port forwarding cannot be checked from this machine.")
    ans = input(f"Is UDP {port} forwarded on your router to {ip}? [y/n/skip] (skip): ").strip().lower()
    out = []
    for item in results:
        if item[1] != "port forwarding":
            out.append(item)
    if ans in ("y", "yes"):
        out.append(("ok", "port forwarding", f"confirmed by operator: UDP {port} forwarded to {ip}", None))
    elif ans in ("n", "no"):
        out.append(("warn", "port forwarding",
                    f"operator reports UDP {port} is not forwarded: senders outside this network cannot reach you",
                    f"forward UDP {port} to {ip} on your router"))
    else:
        out.append(next((item for item in results if item[1] == "port forwarding"),
                        ("info", "port forwarding", "not confirmed", None)))
    return out


def print_checks(results, heading):
    print(paint(heading, BOLD))
    colors = {"ok": GREEN, "warn": YELLOW, "fail": RED, "info": CYAN}
    labels = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
    for level, name, detail, fix in results:
        print(f"  [{paint(labels[level], colors[level])}] {name:<18} {detail}")
        if fix and level in ("warn", "fail"):
            print(f"         {'':<18} fix: {fix}")
    failed = any(r[0] == "fail" for r in results)
    warned = any(r[0] == "warn" for r in results)
    return failed, warned


def cmd_check(a):
    results = check_environment(a.mode, a.host, a.port)
    if a.selftest:
        if a.mode == "listener" and not any(r[0] == "fail" for r in results):
            results.append(loopback_selftest(a.port))
        elif a.mode == "caller":
            results.append(("info", "loopback test", "only runs in listener mode", None))
    failed, warned = print_checks(results, f"srtdebug check - {a.mode} UDP {a.port}")
    print()
    if failed:
        print(paint("NOT READY - fix the FAIL items before testing.", RED))
        return 1
    if warned:
        print(paint("READY WITH WARNINGS - review the WARN items.", YELLOW))
        return 0
    print(paint("READY for testing.", GREEN))
    return 0


# --------------------------------------------------------------- monitor

def _enc_text(a):
    """Plain description of the encryption setting. The passphrase itself is never written down."""
    if not getattr(a, "encrypted", False):
        return "off"
    bits = (getattr(a, "pbkeylen", None) or 16) * 8
    return f"on, AES-{bits}" + ("" if getattr(a, "pbkeylen", None) else " (default key length)")


class Monitor:
    def __init__(self, a, proc, stats_path, export_fh):
        self.a = a
        self.proc = proc
        self.stats_path = stats_path
        self.export_fh = export_fh
        self.export_fmt = "csv" if a.export and a.export.lower().endswith(".csv") else "jsonl"
        self.export_writer = None
        self.payload = bool(getattr(a, "payload", False))
        fields = ROW_FIELDS + (PAYLOAD_FIELDS if self.payload else [])
        if export_fh and self.export_fmt == "csv":
            self.export_writer = csv.DictWriter(export_fh, fieldnames=fields)
            self.export_writer.writeheader()

        self.start = time.time()
        self.ts = None           # transport-stream analyzer (payload mode)
        self.last_probe_snap = None   # newest stream snapshot the probe used (for GOP)
        self.ring = bytearray()  # recent stream bytes, for the ffprobe snapshot
        self.arrivals = deque()  # (monotonic time, bytes) for each chunk, to cut the newest window
        self.content_last = None      # latest audio and video content check
        self.content_prev = set()     # problem kinds seen in the previous check
        self.content_dir = None
        self.ring_lock = threading.Lock()
        self.codec_detail = {}   # pid -> plain-language description from ffprobe
        self.codec_fields = {}   # pid -> [(label, value)] for the stream details panel
        self.codec_raw = {}      # pid -> merged ffprobe entry, for the config checks
        self.picture_history = []  # per-frame luma measurements, newest last
        self.viewer = None       # viewer.Viewer when --view is on
        self.usage_last = None   # latest CPU and memory reading of this monitor (see usage.py)
        self.usage_hist = deque(maxlen=1800)   # the readings for the trend charts: an hour at one every 2 s
        self.probe_status = "waiting for enough stream to probe"
        self.srt_sec = {}        # per-second SRT evidence for attribution
        self.reconnect_pending = False
        self.connects = 0
        self.prev_lost = None
        self.prev_dropped = None
        if self.payload:
            sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
            import tsanalyze
            self.ts = tsanalyze.TSAnalyzer(start=time.monotonic())
        self.fh = None
        self.buf = ""
        self.prev = None           # rate baseline (last emitted sample)
        self.last = None           # most recent raw sample (cumulative)
        self.before_reset = {k: 0 for k in KEYS}  # totals carried over reconnects
        self.last_seen = None
        self.row = None
        self.rows = 0
        self.rate_n, self.rate_sum, self.rate_min, self.rate_max = 0, 0.0, None, 0.0
        self.stalled = False
        self.idle_emit = 0.0
        self.av_recent = []                    # last few A/V offsets, for a steady live reading
        self.rtt_n, self.rtt_sum, self.rtt_max = 0, 0, 0
        self.spikes = spikes.SpikeWatch()   # round-trip spikes, reported like the sending panel's
        self.events = deque(maxlen=200)
        self.rows_list = []          # every sample, kept for the HTML report
        self.event_count = 0
        self.stderr_tail = deque(maxlen=3)
        self.reject_count = 0           # rejections seen since the last connection-log entry
        self.peer_asked_ms = None       # latency the other device asked for in the handshake (listener side)
        # caller diagnosis: why no signal arrived (rejected, no answer, or connected with no data)
        self.t0 = time.time()
        self.first_rx = None
        self.conn_drops = None        # UDP drops on the listening port, the last time it was read
        self.conn_reject_logged = 0.0 # when a repeated rejection was last written to the connection log
        self.rx_last_bytes = None
        self.rx_last_change = time.time()
        self.reject_reason = None
        self.diag_shown = set()
        self.stopping = False
        self.drawn = 0

    def _conn(self, kind, text):
        """Connection log: each attempt, what the other side said, and what the network did. Shown in the
        Events tab of the report, with a UTC time, so a failed connection can be traced step by step."""
        self.events.append((time.strftime("%H:%M:%S UTC", time.gmtime()), "connection", f"{kind}: {text}", None))

    def _note(self, kind, text):
        """Record a one-off event (shown in the dashboard and kept in the report)."""
        self.events.append((time.strftime("%H:%M:%S UTC", time.gmtime()), kind, text, None))

    def _reject_text(self, line):
        """Plain reason for an srt-live-transmit rejection line, or None if the line is not one."""
        if "REJECT" not in line and "rejecting per reception" not in line:
            return None
        if "UNSECURE" in line or "Password required" in line:
            return ("the two sides disagree about encryption: one has a passphrase and the other does not. "
                    "Ask the other side to confirm its passphrase, or set the same one on both sides.")
        if "BADSECRET" in line or "Wrong passphrase" in line:
            return "the passphrase does not match the listener's. Check it on both sides."
        return (f"the listener refused the connection ({line.split(': ', 1)[-1][:120]}). It may already be "
                f"serving another caller.")

    def _udp_loop(self):
        """Listener: every 2 s, see whether the kernel discarded packets for this port. Packets that
        reach the PC but are thrown away mean the listener is not reading fast enough."""
        port = self.a.port
        if netlink.udp_drops(port) is None:
            self._conn("UDP drops", f"not readable on this system, so discards on port {port} are not counted")
            return
        self.conn_drops = netlink.udp_drops(port)
        while not self.stopping:
            time.sleep(2.0)
            now = netlink.udp_drops(port)
            if now is None:
                continue
            if self.conn_drops is not None and now > self.conn_drops:
                self._conn("UDP drops", f"{now - self.conn_drops:,} packets reached port {port} and were discarded "
                                        f"before SRT read them. The listener is not keeping up: check the sender's "
                                        f"bitrate and this PC's load, or ask for a larger receive buffer.")
            self.conn_drops = now

    def _usage_loop(self):
        """Every 2 s: CPU and memory of this monitor and the helpers it runs (srt-live-transmit, and ffmpeg
        when the video is on). Shown in the Run details tab."""
        meter = _usage.Usage()
        while not self.stopping:
            pids = [os.getpid()]
            if self.proc is not None and self.proc.poll() is None:
                pids.append(self.proc.pid)
            viewer = self.viewer
            vproc = getattr(viewer, "proc", None) if viewer is not None else None
            if vproc is not None and vproc.poll() is None:
                pids.append(vproc.pid)
            self.usage_last = meter.sample(pids)
            if self.usage_last is not None:
                self.usage_last["video"] = vproc is not None and vproc.poll() is None
                total_mb = _pc_ram_mb()
                self.usage_last["mem_pc_pct"] = (round(self.usage_last["rss_mb"] / total_mb * 100, 2)
                                                 if total_mb else None)
                if self.usage_last["cpu_pct"] is not None:
                    # for the trend charts: seconds since start, CPU % of one core, memory MB, memory % of this PC
                    self.usage_hist.append((time.monotonic() - self.start, self.usage_last["cpu_pct"],
                                            self.usage_last["rss_mb"], time.time(),
                                            self.usage_last["mem_pc_pct"]))
            time.sleep(2.0)

    def _diag_loop(self):
        """Caller: check once a second, because with no data there are no stats lines to trigger it."""
        while not self.stopping:
            time.sleep(1.0)
            self._diagnose(time.time())

    def diagnosis_lines(self):
        return [f"  {paint(text, RED)}" for _, kind, text, _ in self.events
                if kind == "error" and text.startswith(("Connection rejected", "No answer", "Connected to"))]

    def _diagnose(self, now):
        """Caller only: explain a missing signal once, in plain words."""
        # until data arrives, a progress line every 5 s, so the connection log shows each step as it happens
        if self.first_rx is None and not self.reject_reason and now - getattr(self, "wait_logged", 0.0) >= 5:
            self.wait_logged = now
            elapsed = int(now - self.t0)
            if self.a.mode == "caller":
                text = f"still trying {self.a.host}:{self.a.port}, no reply after {elapsed} s"
            else:
                text = f"listening on port {self.a.port}, no caller yet ({elapsed} s)"
            self._conn("waiting", text)
        if self.a.mode != "caller":
            return
        target = f"{self.a.host}:{self.a.port}"
        if self.reject_reason and "rejected" not in self.diag_shown:
            self.diag_shown.add("rejected")
            self._note("error", f"Connection rejected: {self.reject_reason}")
        elif self.first_rx is None and now - self.t0 > 10 and "noanswer" not in self.diag_shown \
                and not self.reject_reason:
            self.diag_shown.add("noanswer")
            self._conn("no answer", f"nothing came back from {target} after 10 s")
            self._note("error", f"No answer from {target} after 10 s. Quickest first step: restart the stream "
                                f"on the other side (listener and sender), then reconnect. If it still fails, "
                                f"check the address and port, whether anything on the path blocks UDP "
                                f"(firewall, VPN or NAT), that their listener is running, and that the other end "
                                f"is not already serving another connection or also set to caller.")
        elif self.first_rx is not None and now - self.rx_last_change > 10 and "nodata" not in self.diag_shown:
            self.diag_shown.add("nodata")
            self._note("error", f"Connected to {target}, but no data for 10 s. Quickest first step: restart the "
                                f"stream on the other side (listener and sender). If it still gets no data, ask "
                                f"them to check that their sender is feeding the listener and that the listener "
                                f"is not already serving another caller.")

    # -- background: srt-live-transmit stderr (connect / disconnect messages)
    def _read_stderr(self):
        for raw in self.proc.stderr:
            line = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else raw.strip()
            if not line:
                continue
            self.stderr_tail.append(line)
            asked = re.search(r"HSREQ/rcv.*delay=(\d+)", line)
            if asked:
                self.peer_asked_ms = int(asked.group(1))
            reject = self._reject_text(line)
            if reject:
                # keep the first reason; repeats are counted and reported with the next logged entry
                self.reject_reason = self.reject_reason or reject
                self.reject_count += 1
                now = time.time()
                if now - self.conn_reject_logged > 30:
                    self.conn_reject_logged = now
                    n, self.reject_count = self.reject_count, 0
                    more = f" (seen {n} times since the last entry)" if n > 1 else ""
                    self._conn("rejected", reject + more)
                continue
            kind = self._classify(line)
            if kind in ("connected", "disconnected"):
                self._conn(kind, line[:160])
            if kind:
                self.events.append((time.strftime("%H:%M:%S UTC", time.gmtime()), kind, line, None))
                if kind in ("connected", "disconnected"):
                    self.event_count += 1
                if kind == "connected":
                    self.connects += 1
                    if self.connects > 1:
                        self.reconnect_pending = True  # the next sample carries this as evidence

    # -- background: the stream bytes, when payload analysis is on
    def all_events(self):
        """Monitor events and transport-stream events (PMT/PCR/CC/SDT changes), in time order.
        Both lists are kept after the stream ends, so the report gets them."""
        merged = list(self.events)
        if self.ts is not None:
            merged += list(self.ts.events)
        return sorted(merged, key=lambda e: e[0])

    def add_event(self, kind, text, detail=None):
        self.events.append((time.strftime("%H:%M:%S UTC", time.gmtime()), kind, text, detail))

    def _read_payload(self):
        fd = self.proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 188 * 200)
            except OSError:
                break
            if not chunk:
                break
            now = time.monotonic()
            self.ts.feed(chunk, now=now)
            if self.viewer is not None:
                self.viewer.feed(chunk)
            with self.ring_lock:
                self.ring.extend(chunk)
                self.arrivals.append((now, len(chunk)))
                while self.arrivals and now - self.arrivals[0][0] > max(PROBE_WINDOW_S, CONTENT_WINDOW_S) + 2:
                    self.arrivals.popleft()
                if len(self.ring) > PROBE_BYTES:
                    del self.ring[:len(self.ring) - PROBE_BYTES]

    # -- background: ffprobe on a rolling snapshot, for profile, chroma and colour
    def _content_loop(self):
        """Audio and video content checks on the newest few seconds, on a timer."""
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import content
        time.sleep(PROBE_FIRST_S + 2)
        self.content_dir = content.window_dir()
        while not self.stopping and self.proc.poll() is None:
            with self.ring_lock:
                cutoff = time.monotonic() - CONTENT_WINDOW_S
                recent = sum(n for t, n in self.arrivals if t >= cutoff)
                snap = bytes(self.ring[-recent:]) if 0 < recent <= len(self.ring) else b""
            if len(snap) >= 188 * 200:
                result = content.analyse(snap, self.content_dir)
                found = content.findings(result)
                self.content_last = dict(result, time=time.strftime("%H:%M:%S UTC", time.gmtime()),
                                         findings=[(lvl, text) for _, lvl, text in found])
                kinds = {kind for kind, _, _ in found}
                for kind, level, text in found:
                    if kind == "loudness":                   # shown in the Checks tab; not an event
                        continue
                    if kind not in self.content_prev:        # a problem that was not there last time
                        self.add_event(level, f"content: {text}")
                self.content_prev = kinds
            time.sleep(CONTENT_EVERY_S)

    def _probe_loop(self):
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import tsanalyze
        time.sleep(PROBE_FIRST_S)
        while not self.stopping and self.proc.poll() is None:
            with self.ring_lock:
                # the newest PROBE_WINDOW_S seconds only (see PROBE_WINDOW_S)
                cutoff = time.monotonic() - PROBE_WINDOW_S
                recent = sum(n for t, n in self.arrivals if t >= cutoff)
                snap = bytes(self.ring[-recent:]) if 0 < recent <= len(self.ring) else bytes(self.ring)
            if len(snap) >= 188 * 200:
                self.last_probe_snap = snap        # the GOP is measured from this, on request
                detail = tsanalyze.probe_stream(snap)
                if detail is None:
                    self.probe_status = "ffprobe not available"
                    return
                if detail:
                    detail = tsanalyze.merge_probe(self.codec_raw, detail)
                    self.codec_raw = detail
                    vraw = next((s for s in detail.values() if s.get("codec_type") == "video"), None)
                    if vraw is not None:
                        import picture
                        frames = picture.analyse(snap, vraw) or []
                        stamp = time.strftime("%H:%M:%S UTC", time.gmtime())
                        self.picture_history.extend(dict(f, time=stamp) for f in frames)
                        del self.picture_history[:-40]
                    if self.viewer is not None and not self.viewer.started:
                        video = next((s for s in detail.values() if s.get("codec_type") == "video"), None)
                        if video is not None:
                            self.viewer.start(video.get("color_transfer", ""))
                    desc = {pid: tsanalyze.describe_detail(s) for pid, s in detail.items()}
                    new_fields = {pid: tsanalyze.stream_fields(s) for pid, s in detail.items()}
                    if self.codec_fields:
                        # a change is logged only when two probes in a row report the same new values,
                        # so one partial probe cannot create an event
                        changes = tsanalyze.field_changes(self.codec_fields, new_fields)
                        if not changes:
                            self.pending_change = None
                        elif changes == getattr(self, "pending_change", None):
                            self.add_event("info", "codec change: " + tsanalyze.change_summary(changes),
                                           changes)
                            self.codec_fields = new_fields
                            self.pending_change = None
                        else:
                            self.pending_change = changes
                    else:
                        self.codec_fields = new_fields
                    self.codec_detail = desc
                    self.probe_status = f"probed {time.strftime('%H:%M:%S UTC', time.gmtime())}"
            time.sleep(PROBE_EVERY_S)

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

    # -- stats handling
    def _on_line(self, text):
        try:
            d = json.loads(text)
        except ValueError:
            return
        s = parse_sample(d)
        self.last_seen = time.time()
        if s["rx_bytes"] != self.rx_last_bytes:
            self.rx_last_bytes = s["rx_bytes"]
            self.rx_last_change = self.last_seen
        if s["rx_bytes"] > 0 and self.first_rx is None:
            self.first_rx = self.last_seen
            self._conn("data started", f"{s['rx_bytes']:,} bytes in the first sample")
        self._diagnose(self.last_seen)

        before = self.last
        self.last = s
        if before is not None and s["rx_bytes"] < before["rx_bytes"]:
            # counters restarted: srt reconnected. Keep totals, reset the baseline.
            for k in KEYS:
                self.before_reset[k] += before[k]
            self.prev = None
        if self.prev is None:
            self.prev = s
            return

        if self.stalled:
            # data resumed after a stall: start a fresh baseline so the burst
            # is not averaged over the gap
            self.stalled = False
            self.prev = s
            return
        dt = s["t"] - self.prev["t"]
        if dt <= 0 or dt < self.a.interval:
            return
        rate = (s["rx_bytes"] - self.prev["rx_bytes"]) * 8 / dt / 1e6
        self.prev = s
        self._emit(s, rate)

    def _idle_tick(self):
        # no data for a whole interval: report zero so a dropout is visible. Silence is measured
        # from the last bytes of stream, or the last stats line, whichever is more recent.
        now = time.time()
        if self.last is None or self.prev is None:
            return
        quiet = [now - self.last_seen] if self.last_seen else []
        if self.ts is not None and self.ts.last_data is not None:
            quiet.append(time.monotonic() - self.ts.last_data)
        if not quiet or min(quiet) < self.a.interval or now - self.idle_emit < self.a.interval:
            return
        self.idle_emit = now
        self.stalled = True
        self._emit(self.last, 0.0)

    def _emit(self, s, rate):
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
            "latency_ms": s["tsbpd"],
            "link_bw_mbps": round(s["link_bw"], 1),
            "flow_window": s["window"],
        }
        if self.ts is not None:
            complete = self.ts.windows[-1] if self.ts.windows else None
            # a stale second (no packets since) must read zero, not repeat the last full rate
            fresh = complete is not None and int(time.monotonic() - self.ts.start) - complete["t"] <= 2
            if fresh:
                row.update({k: v for k, v in self.ts.summary_row(complete).items()
                            if k in PAYLOAD_FIELDS})
            else:
                row.update({"mux_mbps": 0.0, "video_mbps": 0.0, "audio_mbps": 0.0})
            sec = int(row["elapsed_s"])
            lost_d = max(0, s["lost"] - self.prev_lost) if self.prev_lost is not None else 0
            drop_d = max(0, s["dropped"] - self.prev_dropped) if self.prev_dropped is not None else 0
            self.prev_lost, self.prev_dropped = s["lost"], s["dropped"]
            self.srt_sec[sec] = {"lost": lost_d, "dropped": drop_d,
                                 "reconnect": self.reconnect_pending}
            self.reconnect_pending = False
        if row.get("av_offset_ms") is not None:
            self.av_recent = (self.av_recent + [row["av_offset_ms"]])[-5:]
        self.row = row
        self.rows += 1
        self.rows_list.append(row)
        self.rate_n += 1
        self.rate_sum += rate
        self.rate_min = rate if self.rate_min is None else min(self.rate_min, rate)
        self.rate_max = max(self.rate_max, rate)
        self.rtt_n += 1
        self.rtt_sum += s["rtt"]
        self.rtt_max = max(self.rtt_max, s["rtt"])
        self.spikes.add(s["rtt"], s["dropped"])

        if self.export_fh:
            if self.export_fmt == "csv":
                self.export_writer.writerow(row)
            else:
                self.export_fh.write(json.dumps(row) + "\n")
        if not TTY:
            print(f"{row['time']}  {rate:7.2f} Mbps  lost {row['lost']:<6} "
                  f"rtt {row['rtt_ms']} ms  buf {row['buf_ms']} ms", flush=True)

    def totals(self):
        last = self.last or {k: 0 for k in KEYS}
        return {k: self.before_reset[k] + last[k] for k in KEYS}

    def status(self, now):
        if self.proc.poll() is not None:
            return "STOPPED", RED
        if self.last_seen is None:
            return "WAITING FOR STREAM", YELLOW
        if now - self.last_seen > max(5.0, 3 * self.a.interval):
            return f"NO DATA for {int(now - self.last_seen)}s", RED
        if self.row and self.row["bitrate_mbps"] < 0.01:
            return "CONNECTED, NO PAYLOAD", YELLOW
        return "RECEIVING", GREEN

    def lines(self):
        a = self.a
        now = time.time()
        status, code = self.status(now)
        t = self.totals()
        row = self.row or {}
        rate = row.get("bitrate_mbps", 0.0) if status == "RECEIVING" else 0.0

        where = (f"listener {a.host or '0.0.0.0'}:{a.port}" if a.mode == "listener"
                 else f"caller -> {a.host}:{a.port}")
        lat = f"{a.latency} ms" if a.latency is not None else "default"
        out = [f"{paint('srtdebug', BOLD)} | {where} | latency {lat} | up {fmt_hms(now - self.start)}"]

        ev = ""
        if self.events:
            ts, _, text, _detail = self.events[-1]
            ev = f"   last event {ts}: {text[:48]}"
        out.append(f"Status   {paint(status, code)}{ev}")

        stats = ""
        if self.rate_n:
            stats = (f"avg {self.rate_sum / self.rate_n:.2f}   "
                     f"min {self.rate_min:.2f}   peak {self.rate_max:.2f}")
        out.append(f"Bitrate  {paint(f'{rate:.2f} Mbps', BOLD)}   {stats}")

        total = t["rx_packets"] + t["lost"]
        loss = 100.0 * t["lost"] / total if total else 0.0
        out.append(f"Packets  {t['rx_packets']:,} rx   lost {t['lost']} "
                   f"({paint(f'{loss:.2f}%', loss_color(loss))})   "
                   f"retrans {t['retrans']}   dropped {t['dropped']}")

        rtt = row.get("rtt_ms", 0)
        neg = row.get("latency_ms", 0)
        if not neg:
            negotiated = "negotiated: waiting for the stream"
        elif a.latency is not None and neg > a.latency + 1:
            negotiated = f"negotiated {neg} ms (peer set this; the larger value applies)"
        else:
            negotiated = f"negotiated {neg} ms"
        out.append(f"Latency  RTT {paint(f'{rtt:.1f} ms', rtt_color(rtt))}   "
                   f"buffer {row.get('buf_ms', 0)} ms   {negotiated}")
        if self.av_recent:
            vals = sorted(self.av_recent)
            av = vals[len(vals) // 2]
            out.append(f"A/V      offset {av_text(av)}")

        if a.verbose:
            out.append(f"Link     est. bandwidth {row.get('link_bw_mbps', 0)} Mbps   "
                       f"flow window {row.get('flow_window', 0)}   belated {row.get('belated', 0)}")

        export = f"   export: {a.export} ({self.rows} rows)" if a.export else ""
        out.append(paint(f"Ctrl-C to stop{export}", DIM))
        return out

    def draw(self):
        if not TTY:
            return
        lines = self.lines()
        out = [f"\033[{self.drawn}A"] if self.drawn else []
        for ln in lines:
            out.append("\033[2K" + ln + "\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.drawn = len(lines)

    def run(self):
        a = self.a
        self._conn("start", f"{a.mode} {a.host or '0.0.0.0'}:{a.port}, latency "
                            f"{a.latency if a.latency is not None else 'default'} ms, "
                            f"encryption {_enc_text(a)}, "
                            f"stream ID {'set' if getattr(a, 'streamid', None) else 'not set'}")
        threading.Thread(target=self._read_stderr, daemon=True).start()
        if a.mode == "listener":
            threading.Thread(target=self._udp_loop, daemon=True).start()
        threading.Thread(target=self._diag_loop, daemon=True).start()   # progress lines in both modes
        threading.Thread(target=self._usage_loop, daemon=True).start()
        deadline = self.start + a.duration if a.duration else None
        last_draw = 0.0
        while True:
            if self.fh is None and os.path.exists(self.stats_path):
                self.fh = open(self.stats_path, "r")
            chunk = self.fh.readline() if self.fh else ""
            if chunk:
                self.buf += chunk
                if self.buf.endswith("\n"):
                    self._on_line(self.buf)
                    self.buf = ""
            now = time.time()
            if now - last_draw >= 0.5:
                self.draw()
                last_draw = now
            if not chunk:
                self._idle_tick()
                if self.proc.poll() is not None:
                    break
                if deadline and now >= deadline:
                    break
                time.sleep(0.05)

    def write_health(self):
        """Write the stream report (health checks, streams, analysis CPU) for the control panel's history."""
        path = getattr(self.a, "health", None)
        if not path or self.ts is None:
            return
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import tsanalyze
        try:
            tsanalyze.write_report(path, tsanalyze.build_report(self.ts, fields=self.codec_fields))
        except (OSError, ValueError):
            pass

    def _health_loop(self):
        while not self.stopping and self.proc.poll() is None:
            time.sleep(5)
            self.write_health()

    def shutdown(self):
        self.stopping = True
        if getattr(self, "content_dir", None):
            shutil.rmtree(self.content_dir, ignore_errors=True)
        if self.viewer is not None:
            self.viewer.stop()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.fh:
            self.fh.close()
        if self.export_fh:
            self.export_fh.close()
        if self.ts is not None:
            self.ts.finish_window()

    def payload_view(self):
        """The same payload snapshot the TUI and report use."""
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import tsanalyze
        p = tsanalyze.payload_snapshot(self.ts, self.codec_detail, self.srt_sec, self.probe_status,
                                       mode=self.a.mode, fields=self.codec_fields, raw=self.codec_raw)
        import picture
        p["picture"] = list(self.picture_history)
        p["content"] = self.content_last
        p["picture_findings"] = picture.findings(self.picture_history[-40:])
        return p

    def payload_summary(self):
        """Codecs, rates and suspected causes from the transport-stream analysis."""
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import tsanalyze
        wins = list(self.ts.windows)
        # first and last seconds are usually partial: leave them out of the averages
        rows = [self.ts.summary_row(w) for w in (wins[1:-1] if len(wins) > 3 else wins)]
        out = ["", paint("Payload analysis", BOLD)]
        for s in self.ts.streams():
            detail = self.codec_detail.get(s["pid"], "")
            out.append(f"  PID 0x{s['pid']:04X}  {s['kind']:<6} {s['codec']:<14} {detail}")
        out.append(f"  Codec detail: {self.probe_status}")
        sdt = self.ts.sdt
        if sdt and (sdt.get("provider") or sdt.get("service")):
            out.append(f"  Likely encoder  {sdt.get('provider') or '?'} / {sdt.get('service') or '?'}  "
                       "(name the sender wrote into the stream; a clue, the sender can change it)")
        if not self.ts.streams():
            out.append("  no program tables seen yet (is this an MPEG-TS stream?)")
        av_vals = [r["av_offset_ms"] for r in rows if r.get("av_offset_ms") is not None]
        if av_vals:
            out.append(f"  A/V       offset {av_text(sorted(av_vals)[len(av_vals) // 2])} "
                       f"(median of the run; positive = audio leads)")
        if rows:
            mux = [r["mux_mbps"] for r in rows]
            null = [r["null_pct"] for r in rows]
            vid = [r["video_mbps"] for r in rows]
            aud = [r["audio_mbps"] for r in rows]
            out.append(f"  Mux       avg {sum(mux) / len(mux):.2f} Mbps   peak {max(mux):.2f}   "
                       f"null packets avg {sum(null) / len(null):.1f}%")
            ratio = tsanalyze.TS_PAYLOAD_RATIO
            out.append(f"  Video     codec ≈{sum(vid) / len(vid) * ratio:.2f} Mbps   "
                       f"Audio codec ≈{sum(aud) / len(aud) * ratio:.2f} Mbps   "
                       f"(stream rates incl. TS headers: video {sum(vid) / len(vid):.2f}, "
                       f"audio {sum(aud) / len(aud):.2f})")
        tr = [t for t in self.ts.tr101290() if t["count"]]
        if tr:
            out.append("  TR 101 290  " + "; ".join(f"P{t['priority']} {t['name']} x{t['count']}" for t in tr))
        else:
            out.append("  TR 101 290  no errors")
        out.append(f"  Integrity CC errors {self.ts.totals['cc_errors']}   "
                   f"PCR discontinuities {self.ts.totals['pcr_disc']}   "
                   f"table changes {self.ts.totals['table_changes']}")
        findings = tsanalyze.attribute(wins, self.srt_sec)
        verdict, tally = tsanalyze.verdict_for(findings, self.a.mode)
        out.append(f"  Verdict   {paint(verdict, BOLD)}")
        for f in findings[:12]:
            span = f"{f['t']}s" if f["t"] == f["t_end"] else f"{f['t']}-{f['t_end']}s"
            who = tsanalyze.owner_label(f["owner"], self.a.mode)
            out.append(f"    {span:<10} x{f['count']:<3} {who:<26} {f['confidence']:<8} "
                       f"{f['what']}: {f['evidence']}")
        if len(findings) > 12:
            out.append(f"    ... {len(findings) - 12} more in the export")
        return "\n".join(out)

    def summary(self):
        dur = time.time() - self.start
        t = self.totals()
        out = ["", paint("Session summary", BOLD) + f"  ({fmt_hms(dur)})"]
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
        sp = self.spikes.summary()
        if sp:
            out.append(f"  RTT spikes  {sp['count']} (packets dropped during them: {sp['dropped']})   last: {sp['last']}")
        out.append(f"  Events    {self.event_count} connect/disconnect")
        out.extend(self.diagnosis_lines())
        if self.a.export:
            out.append(f"  Exported  {self.rows} rows -> {self.a.export}")
        rc = self.proc.returncode
        if rc not in (0, None) and not self.stopping:
            tail = f": {self.stderr_tail[-1]}" if self.stderr_tail else ""
            out.append(paint(f"  {SRT_BIN} exited with code {rc}{tail}", RED))
        return "\n".join(out)


def report_meta(a, started):
    return {
        "title": getattr(a, "title", None), "customer": getattr(a, "customer", None),
        "mode": getattr(a, "mode", None), "host": getattr(a, "host", None),
        "port": getattr(a, "port", None), "latency": getattr(a, "latency", None),
        "streamid": getattr(a, "streamid", None), "target_mbps": getattr(a, "target_mbps", None),
        "redact": getattr(a, "redact", False), "started": started,
        "encrypted": getattr(a, "encrypted", False), "pbkeylen": getattr(a, "pbkeylen", None),
        "silence_alarm": getattr(a, "silence_alarm", 10.0),
    }


def cmd_report(a):
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import report
    out = a.out or os.path.splitext(a.source)[0] + ".html"
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(os.path.getmtime(a.source)))
    try:
        report.report_from_file(a.source, out, report_meta(a, started))
    except (RuntimeError, SystemExit) as e:
        print(paint(f"error: {e}", RED))
        return 1
    print(paint(f"Report written: {out}", GREEN))
    return 0


WAITING_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>srtdebug - waiting</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><rect width='24' height='24' rx='5' fill='%230b1220'/><path d='M3 12h4l2-6 4 12 2-6h6' fill='none' stroke='%230a84ff' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>body{font:15px/1.5 system-ui,sans-serif;background:#f5f6fa;color:#1c2030;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}p{text-align:center}</style></head>
<body><main><p>Waiting for the first samples from the stream...</p></main>
<script>setInterval(function(){location.reload();},2000);</script></body></html>"""


class LiveWeb:
    """Builds the live dashboard from the samples received so far. The monitor fills in .mon."""

    def __init__(self, a, started, results):
        self.a, self.started, self.results = a, started, results
        self.mon = None

    def gop_summary(self):
        """Keyframe interval and GOP size of the incoming video, from the stream details. {} until measured."""
        mon = self.mon
        out = {}
        if mon is not None and getattr(mon, "peer_asked_ms", None) is not None:
            out["asked"] = f"{mon.peer_asked_ms} ms"
        sp = mon.spikes.summary() if mon is not None and getattr(mon, "spikes", None) else {}
        if sp:
            out["spikes"] = "%d (%d packets dropped during them). Last: %s" % (sp["count"], sp["dropped"], sp["last"])
        if mon is not None and getattr(mon, "a", None) is not None and mon.a.latency is not None:
            out["ours"] = f"{mon.a.latency} ms"
        snap = getattr(mon, "last_probe_snap", None) if mon is not None else None
        if not snap:
            return out
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import tsanalyze
        try:
            g = tsanalyze.gop_probe(snap)
        except Exception:
            g = None
        if g and g.get("gop"):
            out["gop"] = f"{g['gop']} frames"
        try:
            ki = tsanalyze.keyframe_interval(list(mon.ts.ri_pts)) if mon.ts is not None else None
        except Exception:
            ki = None
        if ki:
            out["keyframe"] = f"every {ki[0]:.2f} s"
        try:
            hc = tsanalyze.keyframe_header_check(snap)
        except Exception:
            hc = None
        if hc and hc["keyframes"]:
            out["headers"] = "%d of %d keyframes" % (hc["with_headers"], hc["keyframes"]) + (
                " (every one)" if hc["with_headers"] == hc["keyframes"] else " (some missing)")
        return out

    def page(self):
        mon = self.mon
        rows = list(mon.rows_list) if mon else []
        # until the first bytes arrive, show the connection log (rows are written every second even with no data)
        if not rows or (mon is not None and mon.first_rx is None):
            return self._waiting(mon)
        payload = None
        try:
            payload = mon.ts and mon.payload_view()
        except Exception:  # a half-written window should not break the page
            payload = None
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import report
        viewer = mon.viewer.status() if mon.viewer is not None else None
        return report.build_html(rows, report_meta(self.a, self.started), checks=self.results,
                                 events=mon.all_events(), payload=payload, live=True, viewer=viewer,
                                 usage=mon.usage_last, usage_hist=list(mon.usage_hist))

    def _waiting(self, mon):
        """Before any samples: say so, and show the connection log, which is what explains a stream that
        will not connect. Refreshes itself so new attempts appear."""
        import html as _h
        entries = [ev for ev in (mon.events if mon else []) if ev[1] == "connection"][-40:]
        items = "".join(f"<li><span class='t'>{_h.escape(t)}</span> {_h.escape(text)}</li>"
                        for t, _, text, _ in entries)
        body = "<p>Waiting for the first samples from the stream...</p>"
        if items:
            body += f"<h2>Connection log</h2><ol class='log'>{items}</ol>"
        page = WAITING_PAGE.replace('<main><p>Waiting for the first samples from the stream...</p></main>',
                                    f"<main>{body}</main>")
        page = page.replace("<head>", '<head><meta http-equiv="refresh" content="2">', 1)
        return page.replace("</style>", "main{width:100%;max-width:720px;padding:16px}h2{font-size:16px;margin:18px 0 6px}"
                            ".log{font-size:13px;padding-left:20px}.log li{margin:4px 0}.log .t{color:#64748b;"
                            "font-variant-numeric:tabular-nums;margin-right:6px}</style>", 1)

    def _snapshot(self):
        mon = self.mon
        rows = list(mon.rows_list) if mon else []
        payload = None
        if mon and mon.ts:
            try:
                payload = mon.payload_view()
            except Exception:  # a half-written window should not break the export
                payload = None
        return rows, payload

    def export_csv(self):
        rows, payload = self._snapshot()
        fields = ROW_FIELDS + (PAYLOAD_FIELDS if payload else [])
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buf.getvalue().encode("utf-8")

    def export_pdf(self):
        import report
        rows, payload = self._snapshot()
        tmp = tempfile.mkdtemp(prefix="srtdebug-pdf-")
        try:
            out = os.path.join(tmp, "report.pdf")
            report.write_output(out, rows, report_meta(self.a, self.started), checks=self.results,
                                events=self.mon.all_events() if self.mon else [], payload=payload)
            with open(out, "rb") as f:
                return f.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _live_handler(live):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path.startswith("/video/"):
                self._video(path[len("/video/"):])
                return
            if path in ("/export.csv", "/export.pdf"):
                self._export(path)
                return
            if path == "/gop.json":
                body = json.dumps(live.gop_summary()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path not in ("/", "/live"):
                self.send_error(404)
                return
            try:
                body, code = live.page().encode("utf-8"), 200
            except Exception as e:  # keep serving; the next poll tries again
                body, code = f"<p>error building the page: {e}</p>".encode("utf-8"), 500
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _export(self, path):
            try:
                if path == "/export.csv":
                    body, ctype = live.export_csv(), "text/csv; charset=utf-8"
                else:
                    body, ctype = live.export_pdf(), "application/pdf"
                code = 200
            except Exception as e:  # say why, instead of failing silently
                body, ctype, code = str(e).encode("utf-8"), "text/plain; charset=utf-8", 500
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _video(self, name):
            mon = live.mon
            file = mon.viewer.file_path(name) if (mon and mon.viewer is not None) else None
            if file is None:
                self.send_error(404)
                return
            with open(file, "rb") as f:
                body = f.read()
            ctype = ("application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # keep the terminal for the monitor
            pass

    return Handler


def start_web(live, bind, port):
    try:
        server = http.server.ThreadingHTTPServer((bind, port), _live_handler(live))
    except OSError as e:
        raise SystemExit(f"error: cannot serve on {bind}:{port} ({e.strerror or e}). "
                         f"Pick another port with --web.")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def cmd_monitor(a):
    if a.interval <= 0:
        raise SystemExit("error: --interval must be greater than 0")

    results = None
    if not a.skip_check:
        results = check_environment(a.mode, a.host, a.port)
        failed, _ = print_checks(results, f"Preflight - {a.mode} UDP {a.port}")
        if a.mode == "listener" and not failed and sys.stdin.isatty():
            results = ask_port_forwarding(results, a.port)
            print_checks([r for r in results if r[1] == "port forwarding"], "Port forwarding")
        print()
        if failed and not a.force:
            print(paint("Not starting: fix the FAIL items above (or use --force).", RED))
            return 1
    if not shutil.which(SRT_BIN):
        print(f"error: {SRT_BIN} not found (sudo apt install srt-tools)")
        return 1

    export_fh = open(a.export, "w", newline="", buffering=1) if a.export else None
    passphrase = resolve_passphrase(a)
    a.encrypted = bool(passphrase)
    uri = build_uri(a.mode, a.host, a.port, a.latency, a.streamid, passphrase, a.pbkeylen)
    tmp = tempfile.mkdtemp(prefix="srtdebug-")
    stats_path = os.path.join(tmp, "stats.jsonl")
    # -f: cumulative counters (without it srt-live-transmit reports per-interval counts)
    cmd = [SRT_BIN, "-s", str(a.stats_every), "-f", "-pf", "json", "-statsout", stats_path, "-loglevel:note"]
    if a.no_reconnect:
        cmd += ["-a", "no"]
    cmd += [uri, SINK]

    # payload analysis needs the bytes: send them to a pipe instead of /dev/null
    viewer = None
    if a.view:
        if not a.web:
            print(paint("Note: --view shows the video in the browser dashboard, so add --web PORT.", YELLOW))
        if shutil.which("ffmpeg"):
            sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
            import viewer as viewer_mod
            viewer = viewer_mod.Viewer(os.path.join(tmp, "hls"))
        else:
            print(paint("Warning: --view needs ffmpeg (sudo apt install ffmpeg, or brew install ffmpeg). "
                        "Continuing without video.", YELLOW))
    need_ts = bool(a.payload or viewer)
    proc_out = subprocess.PIPE if need_ts else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdout=proc_out, stderr=subprocess.PIPE)
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    live = LiveWeb(a, started, results)
    server = None
    if a.web:
        server = start_web(live, a.web_bind, a.web)
        shown = "localhost" if a.web_bind in ("127.0.0.1", "localhost") else a.web_bind
        print(paint(f"Live dashboard: http://{shown}:{a.web}/", GREEN))
        if a.web_bind not in ("127.0.0.1", "localhost"):
            print(paint("Warning: the dashboard is reachable from other machines on this network.", YELLOW))
    mon = Monitor(a, proc, stats_path, export_fh)
    mon.viewer = viewer
    live.mon = mon
    if need_ts:
        threading.Thread(target=mon._read_payload, daemon=True).start()
        threading.Thread(target=mon._probe_loop, daemon=True).start()
        threading.Thread(target=mon._content_loop, daemon=True).start()
        threading.Thread(target=mon._health_loop, daemon=True).start()
    try:
        mon.run()
    except KeyboardInterrupt:
        pass
    finally:
        mon.shutdown()
        mon.write_health()
        if server:
            server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    print(mon.summary())
    if a.payload and mon.ts is not None:
        print(mon.payload_summary())
    if a.report:
        if mon.rows_list:
            sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
            import report
            try:
                payload = mon.ts and mon.payload_view()
                report.write_output(a.report, mon.rows_list, report_meta(a, started),
                                    checks=results, events=mon.all_events(), payload=payload)
                print(paint(f"Report written: {a.report}", GREEN))
            except RuntimeError as e:
                print(paint(f"error: {e}", RED))
        else:
            print(paint("No samples received, so no report was written.", YELLOW))
            if a.mode == "caller":
                for line in mon.diagnosis_lines():
                    print(line)
    return 0


# ----------------------------------------------------------- installation

SRT_GIT_URL = "https://github.com/Haivision/srt.git"
SRT_TAG = "v1.5.7"  # pinned release; override with install-srt --tag
SRT_DOC_URL = "https://github.com/Haivision/srt/blob/master/docs/apps/srt-live-transmit.md"
SRT_BUILD_DEPS = ["git", "tclsh", "pkg-config", "cmake", "libssl-dev", "build-essential"]


def _sudo(cmd):
    if os.geteuid() == 0 or not shutil.which("sudo"):
        return cmd
    return ["sudo"] + cmd


def _run_live(cmd, cwd=None, env=None):
    """Run a command with its output shown to the user. Returns the exit code."""
    print(paint("$ " + " ".join(cmd), DIM), flush=True)
    try:
        return subprocess.call(cmd, cwd=cwd, env=env)
    except OSError as e:
        print(f"error: {e}")
        return 127


def detect_platform():
    """'macos', 'wsl', 'linux' or 'other'. WSL is Linux, so it uses the same apt route."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/version", encoding="utf-8", errors="replace") as f:
                if "microsoft" in f.read().lower():
                    return "wsl"
        except OSError:
            pass
        return "linux"
    return "other"


PLATFORM_NAME = {"macos": "macOS", "wsl": "WSL (Ubuntu)", "linux": "Linux", "other": "this system"}


def install_via_brew():
    """macOS: Homebrew's srt formula (see the Haivision macOS build guide)."""
    if not shutil.which("brew"):
        print(paint("Homebrew is not installed. Install it from https://brew.sh, then run:", YELLOW))
        print("  brew install srt")
        return False
    if _run_live(["brew", "install", "srt"]) != 0:
        return False
    if not shutil.which(SRT_BIN):
        print(paint(f"The Homebrew formula finished, but {SRT_BIN} is not on PATH. "
                    f"Try the source build: srtdebug install-srt --method source", YELLOW))
        return False
    return True


def install_via_apt():
    """Install the packaged srt-tools (Ubuntu/Debian)."""
    if not shutil.which("apt-get"):
        print("apt-get not found; use --method source instead")
        return False
    for cmd in (_sudo(["apt-get", "update"]), _sudo(["apt-get", "install", "-y", "srt-tools"])):
        if _run_live(cmd) != 0:
            return False
    return shutil.which(SRT_BIN) is not None


def install_from_source(tag=SRT_TAG, prefix=None, platform=None):
    """Build Haivision's release tag from GitHub: ./configure, make, make install.
    Linux and WSL use apt for the build tools; macOS uses Homebrew and its OpenSSL."""
    platform = platform or detect_platform()
    env = None
    if platform in ("linux", "wsl"):
        if _run_live(_sudo(["apt-get", "install", "-y"] + SRT_BUILD_DEPS)) != 0:
            return False
    elif platform == "macos":
        if not shutil.which("brew"):
            print(paint("Source builds on macOS need Homebrew (https://brew.sh).", YELLOW))
            return False
        if _run_live(["brew", "install", "cmake", "openssl", "pkgconf"]) != 0:
            return False
        _, ssl_prefix = run(["brew", "--prefix", "openssl"])
        ssl_prefix = ssl_prefix.strip()
        env = dict(os.environ, OPENSSL_ROOT_DIR=ssl_prefix,
                   OPENSSL_LIB_DIR=os.path.join(ssl_prefix, "lib"),
                   OPENSSL_INCLUDE_DIR=os.path.join(ssl_prefix, "include"))
    else:
        print(paint(f"No automatic build for {PLATFORM_NAME[platform]}. See {SRT_DOC_URL}", YELLOW))
        return False
    src = os.path.join(os.path.expanduser("~"), ".cache", "srtdebug", f"srt-{tag}")
    if os.path.exists(src):
        shutil.rmtree(src)
    os.makedirs(os.path.dirname(src), exist_ok=True)
    steps = [
        (["git", "clone", "--depth", "1", "--branch", tag, SRT_GIT_URL, src], None, None),
        (["./configure"] + ([f"--prefix={prefix}"] if prefix else []), src, env),
        (["make", f"-j{os.cpu_count() or 2}"], src, env),
        (_sudo(["make", "install"]), src, None),
    ]
    if platform in ("linux", "wsl"):
        steps.append((_sudo(["ldconfig"]), None, None))
    for cmd, cwd, step_env in steps:
        if _run_live(cmd, cwd=cwd, env=step_env) != 0:
            return False
    return shutil.which(SRT_BIN) is not None


def default_method(platform):
    return "brew" if platform == "macos" else "apt"


def cmd_install(method, tag, prefix):
    if shutil.which(SRT_BIN):
        print(f"{SRT_BIN} is already installed ({shutil.which(SRT_BIN)}).")
        return 0
    platform = detect_platform()
    if method == "auto":
        method = default_method(platform)
    print(f"Platform: {PLATFORM_NAME[platform]}. Method: {method}.")
    if method == "apt":
        ok = install_via_apt()
    elif method == "brew":
        ok = install_via_brew()
    else:
        ok = install_from_source(tag, prefix, platform)
    if ok:
        _, out = run([SRT_BIN, "-version"])
        print(paint(f"Installed {SRT_BIN}. {out.splitlines()[0] if out else ''}", GREEN))
        return 0
    print(paint(f"Installation failed. Reference: {SRT_DOC_URL}", RED))
    return 1


def ensure_srt(assume_yes=False):
    """If srt-live-transmit is missing, offer to install it. Returns True when it is available."""
    if shutil.which(SRT_BIN):
        return True
    print(paint(f"{SRT_BIN} is not installed.", YELLOW))
    print(f"Upstream reference: {SRT_DOC_URL}")
    platform = detect_platform()
    print(f"Platform: {PLATFORM_NAME[platform]}")
    if platform == "other":
        print(f"No installer for this system. Install srt-live-transmit by hand: {SRT_DOC_URL}")
        return False
    if platform == "macos":
        quick = ("b", "install the Homebrew srt formula (quick)")
    else:
        quick = ("a", "install the packaged srt-tools with apt (quick, SRT 1.5.3)")
    if assume_yes:
        choice = quick[0]
    elif not sys.stdin.isatty():
        print("Run:  srtdebug install-srt   (add --yes to skip the prompt)")
        return False
    else:
        print(f"  [{quick[0]}] {quick[1]}")
        print(f"  [s] build Haivision's {SRT_TAG} from GitHub (source, several minutes)")
        print("  [n] do nothing")
        choice = input(f"Choose {quick[0]}/s/n [{quick[0]}]: ").strip().lower() or quick[0]
    if choice == quick[0]:
        ok = install_via_brew() if platform == "macos" else install_via_apt()
    elif choice == "s":
        ok = install_from_source(SRT_TAG, None, platform)
    else:
        return False
    if not ok:
        print(paint("Installation did not complete. Try: srtdebug install-srt --method source", RED))
    return ok


def _deps():
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import deps
    return deps


def _run_install(dep, platform):
    deps = _deps()
    steps, kind = deps.install_steps(dep, platform)
    if not steps:
        print(f"  Install it by hand: {dep['manual']}")
        return False
    for step in steps:
        cmd = _sudo(step) if kind == "apt" else step
        if _run_live(cmd) != 0:
            print(paint(f"  {dep['name']}: the install did not complete.", RED))
            return False
    found, _ = deps.installed(dep)
    if found:
        print(paint(f"  {dep['name']} installed.", GREEN))
    else:
        print(paint(f"  {dep['name']} installed, but it is not on PATH yet. Open a new terminal.", YELLOW))
    return found


def offer_missing(keys, assume_yes=False):
    """For optional programs a command needs: say what is missing and offer to install it.
    Without a terminal it only prints the install command."""
    deps = _deps()
    platform = detect_platform()
    interactive = sys.stdin.isatty()
    for key in keys:
        dep = deps.BY_KEY[key]
        if deps.installed(dep)[0]:
            continue
        print(paint(f"{dep['name']}: not installed.", YELLOW))
        print(f"  Needed for: {dep['needed_for']}")
        steps, kind = deps.install_steps(dep, platform)
        if not steps:
            print(f"  Install it by hand: {dep['manual']}")
            continue
        shown = " && ".join(" ".join((["sudo"] if kind == "apt" and os.geteuid() != 0 else []) + st)
                            for st in steps)
        print(f"  Install command: {shown}")
        if not interactive and not assume_yes:
            print("  Run 'srtdebug deps --install' to install it.")
            continue
        if assume_yes or input("  Install now? [y/N]: ").strip().lower() in ("y", "yes"):
            _run_install(dep, platform)
        else:
            print(f"  Skipped. Without it: {dep['needed_for']} will be unavailable.")


def cmd_deps(a):
    deps = _deps()
    platform = detect_platform()
    print(f"Platform: {PLATFORM_NAME[platform]}")
    missing = []
    for dep in deps.DEPENDENCIES:
        found, _ = deps.installed(dep)
        kind = "required" if dep["required"] else "optional"
        if found:
            mark = paint("OK      ", GREEN)
        else:
            mark = paint("MISSING ", RED if dep["required"] else YELLOW)
            missing.append(dep["key"])
        print(f"  {mark}{dep['name']}  ({kind})")
        print(f"          needed for: {dep['needed_for']}")
    if not missing:
        print("\nAll programs are installed.")
        return 0
    if a.install:
        print()
        offer_missing(missing, assume_yes=a.yes)
    else:
        print(f"\nRun 'srtdebug deps --install' to install the {len(missing)} missing program(s).")
    return 0


def report_opts(p):
    p.add_argument("--title", help="report title (e.g. the stream or site name)")
    p.add_argument("--customer", help="customer name shown on the report")
    p.add_argument("--target-mbps", type=float, help="expected bitrate; enables target checks")
    p.add_argument("--redact", action="store_true",
                   help="leave host and streamid out of the report (for sharing)")


def _on_term(signum, frame):
    raise KeyboardInterrupt


def cmd_samples(a):
    """Make the test clips into a folder: one per frame rate, resolution and scan type, plus one clip
    that changes format between segments."""
    import samples
    try:
        samples.make(a.dir, log=lambda m: print(m, flush=True))
    except RuntimeError as e:
        print(f"error: {e}")
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: ffmpeg failed while making a clip ({e.cmd[-1]})")
        return 1
    print(f"Done. Clips are in {a.dir}. Use one with: srtdebug feed --file <clip> ...")
    return 0


def cmd_feed(a):
    """Send a clip over SRT as if it were a live stream. Run a monitor on the other end to see it."""
    ffmpeg, slt = shutil.which("ffmpeg"), shutil.which("srt-live-transmit") or SRT_BIN
    if not ffmpeg:
        print("error: ffmpeg is needed to send a clip. Install it, for example: sudo apt install -y ffmpeg")
        return 1
    if not os.path.isfile(a.file):
        print(f"error: clip not found: {a.file}. Make some with: srtdebug samples")
        return 1
    import tsanalyze
    print("Reading the source format...", flush=True)
    sections, err = tsanalyze.describe_source(a.file)
    if err:
        print(f"error: {err}")
        return 1
    for line in tsanalyze.source_report_lines(sections):
        print(line)
    if a.check:
        return 0
    passphrase = os.environ.get(a.passphrase_env) if a.passphrase_env else None
    if a.passphrase_env and not passphrase:
        print(f"error: environment variable {a.passphrase_env} is empty or not set")
        return 1
    if a.url:
        if not a.url.lower().startswith("srt://"):
            print("error: the destination must start with srt:// (for example srt://203.0.113.5:9000)")
            return 1
        uri = a.url                      # used as given, so its own options (streamid, latency...) apply
        a.mode = "caller" if "mode=caller" in a.url else "listener" if "mode=listener" in a.url else "caller"
        a.host, a.port = None, None
    else:
        if not a.mode or a.port is None:
            print("error: give --url, or both --mode and --port")
            return 1
        uri = build_uri(a.mode, a.host, a.port, latency=a.latency, streamid=a.streamid,
                        passphrase=passphrase)
    src = [ffmpeg, "-hide_banner", "-loglevel", "error", "-re"]
    if a.loop:
        src += ["-stream_loop", "-1"]
    src += ["-i", a.file, "-c", "copy", "-f", "mpegts", "-"]
    dst = [slt, "file://con", uri]
    what = "waiting for a monitor to connect" if a.mode == "listener" else f"connecting to {uri.split('?')[0]}"
    print(f"Sending {os.path.basename(a.file)} ({what}). Stop with Ctrl-C.", flush=True)
    p1 = subprocess.Popen(src, stdout=subprocess.PIPE)
    p2 = subprocess.Popen(dst, stdin=p1.stdout)
    p1.stdout.close()
    try:
        p2.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in (p2, p1):
            if p.poll() is None:
                p.terminate()
    return 0


RTT_MULTIPLIER = 4        # Haivision's rule of thumb for a good network: SRT latency = 4 x RTT


def _percentile(values, pct):
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


def cmd_rtt(a):
    """Run the monitor for a while, read the RTT it reports, and say what SRT latency that supports."""
    import csv
    import subprocess
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    cmd = [sys.executable, os.path.abspath(__file__), "monitor", "--mode", a.mode, "--port", str(a.port),
           "--duration", str(a.duration), "--export", path]
    if a.host:
        cmd += ["--host", a.host]
    if getattr(a, "streamid", None):
        cmd += ["--streamid", a.streamid]
    if getattr(a, "passphrase_env", None):
        cmd += ["--passphrase-env", a.passphrase_env]
    if getattr(a, "pbkeylen", None):
        cmd += ["--pbkeylen", str(a.pbkeylen)]
    print(f"Measuring the round trip for {a.duration:.0f} s on {a.mode} port {a.port}...")
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=a.duration + 60)
    except subprocess.TimeoutExpired:
        pass
    rtts, losses = [], []
    try:
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    rtt = float(row.get("rtt_ms") or 0)
                    loss = float(row.get("loss_pct") or 0)
                except ValueError:
                    continue
                if rtt > 0:
                    rtts.append(rtt)
                    losses.append(loss)
    finally:
        os.unlink(path)
    if not rtts:
        print("No round-trip readings: the link did not connect in that time. Check the connection first "
              "(run srtdebug check, and try again with the other end running).")
        return 1
    lo, med, p95, hi = min(rtts), statistics.median(rtts), _percentile(rtts, 95), max(rtts)
    loss = statistics.mean(losses)
    # rule of thumb from the SRT documentation, rounded up to 10 ms
    need = max(20, math.ceil(RTT_MULTIPLIER * p95 / 10.0) * 10)   # SRT accepts latency from 20 ms upward
    print(f"Round trip: min {lo:.1f} ms, median {med:.1f} ms, 95th percentile {p95:.1f} ms, max {hi:.1f} ms "
          f"({len(rtts)} readings)")
    print(f"Average loss: {loss:.2f}%")
    print(f"Minimum SRT latency to suggest: {need} ms  ({RTT_MULTIPLIER} x the 95th percentile round trip)")
    if loss > 0.2:
        print(f"Loss is above 0.2%, where the rule of thumb is not enough. Set the latency higher than {need} ms, "
              f"for example {RTT_MULTIPLIER * 2} x the median or more, and check the path.")
    print("This is measured while it is running, so it covers the path at the time of the test. Repeat it "
          "at the times the stream runs.")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="srtdebug",
                                 description="Live SRT stream inspector (wraps srt-live-transmit).")
    ap.add_argument("--version", action="version", version=f"srtdebug {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--mode", choices=["caller", "listener"], required=True,
                       help="caller: connect out to a remote listener; "
                            "listener: wait for a remote caller")
        p.add_argument("--port", type=int, required=True, help="SRT (UDP) port")
        p.add_argument("--host", help="caller: remote address; listener: local bind address "
                                      "(default 0.0.0.0)")

    c = sub.add_parser("check", help="verify tooling, port and firewall before a test")
    common(c)
    c.add_argument("--selftest", action="store_true",
                   help="also run a short local SRT loopback test")

    sm = sub.add_parser("samples", help="make test clips covering frame rates, resolutions and scan types")
    sm.add_argument("--dir", default=os.path.join(os.path.expanduser("~"), "srtdebug-samples"),
                    help="folder for the clips (default ~/srtdebug-samples)")
    fd = sub.add_parser("feed", help="send a clip over SRT, as caller or listener, as if it were a live stream")
    fd.add_argument("--url", metavar="SRT_URL",
                    help="destination as a full SRT URL, for example srt://203.0.113.5:9000?streamid=live/cam1 "
                         "(used as given; replaces --mode, --host and --port)")
    fd.add_argument("--mode", choices=["caller", "listener"],
                    help="caller: connect to a remote listener; listener: wait for a monitor to connect")
    fd.add_argument("--port", type=int, help="SRT (UDP) port")
    fd.add_argument("--host", help="caller: remote address; listener: local bind address (default 0.0.0.0)")
    fd.add_argument("--file", required=True,
                    help="source to send: a clip from samples, or your own video (MP4, MOV, MKV or TS). "
                         "Its format is reported before sending")
    fd.add_argument("--check", action="store_true",
                    help="only report the source's format (aspect, scan, frame rate), and do not send")
    fd.add_argument("--loop", action="store_true", help="repeat the clip until you press Ctrl-C")
    fd.add_argument("--latency", type=int, default=None, help="SRT latency in ms (default: library default)")
    fd.add_argument("--streamid", help="SRT stream ID to send")
    fd.add_argument("--passphrase-env", metavar="VAR", help="read the passphrase from this environment variable")
    rt = sub.add_parser("rtt", help="measure the round trip between the two ends and suggest a minimum SRT latency")
    common(rt)
    rt.add_argument("--duration", type=float, default=60.0,
                    help="seconds to measure (default 60; longer gives a steadier figure)")
    m = sub.add_parser("monitor", help="receive a stream and show live health")
    common(m)
    m.add_argument("--interval", type=float, default=1.0,
                   help="seconds between samples (default 1)")
    m.add_argument("--latency", type=int, default=DEFAULT_LATENCY_MS,
                   help=f"SRT latency in ms (default {DEFAULT_LATENCY_MS}; must cover 3-4 x RTT)")
    m.add_argument("--silence-alarm", type=float, default=10.0,
                   help="seconds without stream data before the dashboard shows NO SIGNAL (default 10)")
    add_crypto_args(m)
    m.add_argument("--streamid", help="SRT streamid, if the sender uses one")
    m.add_argument("--export", metavar="FILE",
                   help="log every sample to FILE (.csv or .jsonl), flushed live")
    m.add_argument("--duration", type=float, help="stop after N seconds")
    m.add_argument("--stats-every", type=int, default=20,
                   help="stats cadence in packets (default 20; lower it for low bitrates)")
    m.add_argument("-v", "--verbose", action="store_true",
                   help="also show link bandwidth estimate and flow window")
    m.add_argument("--no-reconnect", action="store_true",
                   help="exit when the stream ends instead of auto-reconnecting")
    m.add_argument("--skip-check", action="store_true", help="skip the preflight check")
    m.add_argument("--force", action="store_true", help="start even if preflight fails")
    m.add_argument("--web", type=int, metavar="PORT",
                   help="also serve a live dashboard at http://localhost:PORT (open it in a browser)")
    m.add_argument("--web-bind", default="127.0.0.1", metavar="ADDR",
                   help="address for --web (default 127.0.0.1; 0.0.0.0 shares it on the network)")
    m.add_argument("--report", metavar="FILE",
                   help="write a dashboard when the run ends: FILE.html or FILE.pdf")
    m.add_argument("--view", action="store_true",
                   help="show the live video in the --web dashboard (needs ffmpeg; adds about 8 s of delay)")
    m.add_argument("--payload", action="store_true",
                   help="analyse the MPEG-TS inside: codecs, per-stream bitrate, null packets, "
                        "continuity errors, restarts, and suspected cause")
    m.add_argument("--health", metavar="FILE", help=argparse.SUPPRESS)  # the control panel's stream report
    report_opts(m)

    rp = sub.add_parser("report", help="build an HTML dashboard from an export file")
    rp.add_argument("source", help="CSV or JSONL export from monitor or ui")
    rp.add_argument("--out", metavar="FILE",
                    help="output file: .html or .pdf (default: source name with .html)")
    report_opts(rp)

    u = sub.add_parser("ui", help="interactive full-screen view; change settings live")
    u.add_argument("--mode", choices=["caller", "listener"], default="listener")
    u.add_argument("--port", type=int, default=9000, help="SRT (UDP) port (default 9000)")
    u.add_argument("--host", help="caller: remote address; listener: bind address")
    u.add_argument("--latency", type=int, default=DEFAULT_LATENCY_MS,
                   help=f"SRT latency in ms (default {DEFAULT_LATENCY_MS})")
    add_crypto_args(u)
    u.add_argument("--streamid", help="SRT streamid")
    u.add_argument("--interval", type=float, default=1.0, help="sample interval in seconds")
    u.add_argument("--stats-every", type=int, default=20, help="stats cadence in packets")
    u.add_argument("--export", metavar="FILE", help="start exporting to FILE (.csv or .jsonl)")
    u.add_argument("-v", "--verbose", action="store_true", help="show extra fields")
    u.add_argument("--payload", action="store_true",
                   help="also analyse the MPEG-TS inside (codecs, rates, anomalies)")

    ins = sub.add_parser("install-srt", help="install srt-live-transmit (apt or source build)")
    ins.add_argument("--method", choices=["auto", "apt", "brew", "source"], default="auto",
                     help="auto: apt on Linux/WSL, Homebrew on macOS; apt/brew: package manager; source: Haivision GitHub release")
    ins.add_argument("--tag", default=SRT_TAG, help=f"git tag for source builds (default {SRT_TAG})")
    ins.add_argument("--prefix", help="install prefix for source builds (default /usr/local)")

    dp = sub.add_parser("deps", help="show the external programs srtdebug uses, and install the missing ones")
    dp.add_argument("--install", action="store_true", help="offer to install the missing programs")
    dp.add_argument("--yes", action="store_true", help="install without asking (with --install)")
    ev = sub.add_parser("evidence", help="frames and a report for frozen or black pictures in a recorded TS file")
    ev.add_argument("file", help="an MPEG-TS file")
    ev.add_argument("--out", metavar="DIR", help="folder for the frames and report.html (default: next to the file)")
    sv = sub.add_parser("serve", help="browser control panel: configure, start and stop, and watch live")
    sv.add_argument("--port", type=int, default=9999, help="port for the control panel (default 9999)")
    sv.add_argument("--bind", default="127.0.0.1", metavar="ADDR",
                    help="address for the control panel (default 127.0.0.1; 0.0.0.0 shares it on the network)")

    a = ap.parse_args()
    if getattr(a, "port", None) is not None and not 1 <= a.port <= 65535:
        ap.error("--port must be 1-65535")

    signal.signal(signal.SIGTERM, _on_term)
    # A parent shell may have started us with SIGINT ignored, and that is inherited.
    # Restore the default so Ctrl-C (or the control panel's stop) runs the clean shutdown,
    # which also stops srt-live-transmit.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if a.cmd == "rtt":
        return cmd_rtt(a)
    if a.cmd == "samples":
        return cmd_samples(a)
    if a.cmd == "feed":
        return cmd_feed(a)
    if a.cmd == "install-srt":
        return cmd_install(a.method, a.tag, a.prefix)
    if a.cmd == "deps":
        return cmd_deps(a)
    if a.cmd == "report":  # reads a file only; does not need srt-live-transmit
        keys = _deps().needed_keys(a)
        if keys:
            offer_missing(keys)
        return cmd_report(a)
    if not shutil.which(SRT_BIN) and not ensure_srt(assume_yes=False):
        return 1
    keys = _deps().needed_keys(a)
    if keys:
        offer_missing(keys)
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "evidence":
        import tempfile
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import content, evidence
        if not os.path.isfile(a.file):
            ap.error(f"no such file: {a.file}")
        with open(a.file, "rb") as f:
            data = f.read()
        out = a.out or os.path.splitext(a.file)[0] + "-evidence"
        result = content.analyse(data, tempfile.mkdtemp(prefix="srtdebug-evidence-"))
        entries = evidence.build(a.file, out, result, title=os.path.basename(a.file))
        for e in entries:
            print(f"{e['start']:8.2f} s  {e['kind']:6}  {e['text']}  {e.get('png') or '(no frame)'}")
        print(f"{len(entries)} problem picture(s). Report: {os.path.join(out, 'report.html')}")
        return 0
    if a.cmd == "serve":
        if not 1 <= a.port <= 65535:
            ap.error("--port must be 1-65535")
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import serve
        return serve.run(a)
    if a.cmd == "ui":
        if a.interval <= 0:
            ap.error("--interval must be greater than 0")
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        import tui
        return tui.run(a)
    return cmd_monitor(a)


if __name__ == "__main__":
    sys.exit(main())
