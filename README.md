# srtdebug

A small command-line inspector for SRT streams, built on `srt-live-transmit`.
It runs a readiness check, then shows live bitrate, loss, RTT and buffer
health, and can export every sample for later analysis.

Python 3 standard library only. Requires the `srt-tools` package.

## Install

Requirements: Python 3 (developed and tested on 3.13; older 3.x versions are not
verified yet), and `srt-live-transmit`. The tool itself has no Python packages to install.

`srtdebug` detects the platform and picks the right installer:

| Platform | srt-live-transmit comes from | Installer route |
|---|---|---|
| Ubuntu / Debian Linux | apt `srt-tools` (SRT 1.5.3) | `install-srt` uses apt |
| WSL (Ubuntu) | apt `srt-tools` (SRT 1.5.3) | same as Linux |
| macOS | Homebrew `srt` formula | `install-srt` uses `brew install srt` |
| Any | Haivision source, pinned tag v1.5.7 | `install-srt --method source` |

Windows without WSL is not supported. Use WSL.

### Ubuntu / Debian and WSL

```bash
sudo ln -sf /home/claude/srtdebug/srtdebug.py /usr/local/bin/srtdebug   # adjust the path
srtdebug install-srt                 # apt srt-tools (SRT 1.5.3)
srtdebug install-srt --method source # build Haivision v1.5.7 from GitHub
```

### macOS

```bash
# Homebrew is required: https://brew.sh
sudo mkdir -p /usr/local/bin   # only if it does not exist (Apple Silicon Macs may not have it)
sudo ln -sf "$PWD/srtdebug.py" /usr/local/bin/srtdebug
srtdebug install-srt           # runs: brew install srt
srtdebug install-srt --method source   # builds v1.5.7; needs Homebrew cmake, openssl, pkgconf
```

The Homebrew formula may not put `srt-live-transmit` on your PATH. If the install
finishes but the tool still reports it missing, use the source build. Check with
`which srt-live-transmit`.

### Every platform

If `srt-live-transmit` is missing, every command offers to install it first:

```
srt-live-transmit is not installed.
Platform: macOS
Upstream reference: https://github.com/Haivision/srt/blob/master/docs/apps/srt-live-transmit.md
  [b] install the Homebrew srt formula (quick)
  [s] build Haivision's v1.5.7 from GitHub (source, several minutes)
  [n] do nothing
```

- Nothing is installed without your choice. Without a terminal (cron, scripts) the
  tool stops and prints the command instead. Use `--yes` to skip the prompt.
- Source builds clone the pinned tag `v1.5.7` (not `master`) and run `./configure`,
  `make` and `make install`. The source is kept in `~/.cache/srtdebug/`.
- `--prefix` changes the install location and `--tag` picks another release.
- `srtdebug install-srt --method auto` is the default: apt on Linux and WSL, Homebrew on macOS.

### Browser control panel (`serve`)

```bash
srtdebug serve            # control panel at http://localhost:9999/ (Ctrl-C quits)
```

Configure a monitor in the browser, press Start, open the live dashboard, and press Stop
to save the report. The panel runs on one port; the dashboard is proxied through it. It
listens on 127.0.0.1 by default and has no login: `--bind 0.0.0.0` lets anyone on your
network start and stop monitoring.

## Dependencies

`srtdebug` itself uses only the Python standard library. It calls these programs:

| Program | Required | Needed for | Ubuntu / WSL | macOS |
|---|---|---|---|---|
| `srt-live-transmit` | yes | receiving the SRT stream | `sudo apt-get install srt-tools` | `brew install srt` |
| `ffmpeg` and `ffprobe` | no | codec, chroma and colour details and picture checks (`--payload`); live video (`--view`) | `sudo apt-get install ffmpeg` | `brew install ffmpeg` |
| Chrome or Chromium | no | PDF reports and the PDF export | install by hand (Ubuntu's `chromium-browser` is a snap wrapper); Google Chrome `.deb` works | `brew install --cask google-chrome` |
| Python 3 | yes | running the tool (tested on 3.13 only) | `python3` | `python3` |

The browser loads hls.js from cdnjs.cloudflare.com for the live video. That needs internet
access from the viewer's browser, not from the monitoring machine.

Check what is installed at any time:

```bash
srtdebug deps             # show each program, whether it is found, and what needs it
srtdebug deps --install   # offer to install whatever is missing
```

When a command needs an optional program that is missing, `srtdebug` says so and offers to
install it, for example when you start with `--payload` or `--view`, or export a PDF. Without
a terminal it prints the install command instead.

## Usage

### 1. Check readiness before a test

```bash
srtdebug check --mode listener --port 9000            # we wait for the sender
srtdebug check --mode listener --port 9000 --selftest # also run a local loopback test
srtdebug check --mode caller --host 10.0.0.5 --port 9000  # we connect out
```

Each item is PASS / WARN / FAIL / INFO, and the verdict is one of:
`READY`, `READY WITH WARNINGS` or `NOT READY` (exit code 1).
FAIL lines include the exact command to fix them.

What it checks:
- `srt-live-transmit` is installed, and which SRT library version it uses
- Listener: the UDP port is free, and the local IPs you can be reached on
- Listener: `ufw` rules and the default incoming policy
- Listener: `iptables` rules and INPUT policy for the port (needs sudo for full detail)
- Caller: the remote host resolves
- `--selftest` (listener only): a short local SRT loopback, which confirms that the
  SRT library and tool work. It does **not** prove the firewall lets outside traffic in.

### 2. Monitor a stream

```bash
# Receive as LISTENER: wait for a remote caller to push to us
srtdebug monitor --mode listener --port 9000

# Receive as CALLER: connect out to a remote listener
srtdebug monitor --mode caller --host 10.0.0.5 --port 9000

# Match an encoder's latency and streamid, log to CSV, stop after 5 minutes
srtdebug monitor --mode caller --host 10.0.0.5 --port 9000 \
    --latency 120 --streamid live/cam1 --export run1.csv --duration 300

# Extra fields (link bandwidth estimate, flow window, belated packets)
srtdebug monitor --mode listener --port 9000 -v
```

The monitor runs the preflight check first and will not start if it finds a FAIL.
Use `--force` to start anyway, or `--skip-check` to skip the check.

The dashboard is deliberately small:

```
srtdebug | listener 0.0.0.0:9000 | latency default | up 00:01:23
Status   RECEIVING   last event 11:35:02: SRT source connected
Bitrate  5.12 Mbps   avg 4.98   min 4.10   peak 5.40
Packets  12,345 rx   lost 0 (0.00%)   retrans 3   dropped 0
Latency  RTT 12.0 ms   buffer 45 ms
Ctrl-C to stop   export: run1.csv (120 rows)
```

Status meanings:
- **WAITING FOR STREAM**: no data has arrived yet
- **RECEIVING**: data is flowing
- **CONNECTED, NO PAYLOAD**: connected but the bitrate is zero
- **NO DATA for Ns**: connected, but no stats for a while. The stream has stalled or dropped.
- **STOPPED**: `srt-live-transmit` exited (see the summary for the code)

Colour: loss ≥0.1% is yellow and ≥1% is red. RTT ≥100 ms is yellow and ≥200 ms is red.
Thresholds are constants at the top of `srtdebug.py`.

When output is not a terminal (piped or logged), each sample is one plain line.

### 3. Export

`--export FILE` writes one row per sample, flushed as it goes, so the file is
usable even if the session is killed:

- `.csv`: `time, elapsed_s, bitrate_mbps, rx_packets, lost, loss_pct, retrans, dropped, belated, rtt_ms, buf_ms, link_bw_mbps, flow_window`
- anything else (for example `.jsonl`): one JSON object per line

Sampling rate is `--interval` (default 1 second).

At the end, a session summary shows average, minimum and peak bitrate; total
packets, loss and retransmissions; RTT; and connect/disconnect events.

### 4. Transport-stream payload (`--payload`)

Add `--payload` to `monitor` or `ui` to look inside the stream as well as at the
SRT link. The tool reads the MPEG-TS packets that `srt-live-transmit` delivers and shows:

- mux rate, and the video and audio bitrates per PID
- codec and profile, from `ffprobe` on a snapshot of the stream: for example
  `High 4:2:2, 4:2:2 10-bit, 1280x720, BT.2020, PQ (HDR)`, or `4:2:0 8-bit`
- null-packet percentage
- continuity-counter errors, PCR discontinuities and PAT/PMT changes
- for each anomaly, a **suspected owner** (sender, network, receiver or link) with a
  confidence level. The rules combine the TS evidence with the SRT per-second
  counters (loss, drops, reconnects).

The owner is an inference from one end of the link. Treat it as a lead to check,
not as proof. `ffprobe` is needed for codec detail; without it the codec line
shows `codec detail: ffprobe not available`.

```bash
srtdebug monitor --mode listener --port 9000 --payload --export run.csv
srtdebug ui --mode listener --port 9000 --payload
```

### 4b. Live video viewer (`--view`)

Add `--view` to `monitor` (or tick "Show the live video" in the control panel) and the
dashboard plays the incoming stream in the browser. It needs `ffmpeg` on the machine.

- The video is re-encoded to H.264 for the browser: 8-bit 4:2:0 at 720p, about 3 Mbps,
  and played through HLS segments of 2 s. Expect roughly 8 s of delay behind the stream.
- HDR input (HLG or PQ) is tone-mapped to SDR BT.709, so the picture looks right in
  any browser. The dashboard's colour details still describe the original stream.
- The player loads hls.js from cdnjs; browsers that play HLS natively (Safari) don't need it.
- Video is for checking that the picture is there and roughly right, not for judging
  colour or quality. The payload analysis is the reference.

```bash
srtdebug monitor --mode listener --port 9000 --web 9999 --view --payload
```

### 4c. Dashboard tabs and exports

The dashboard (live or saved) is split into tabs: Overview, Link, Stream, Anomalies,
Checks, Events and Details. The open tab is kept in the page address (for example `#events`),
so a refresh stays on the same tab.

- **CSV** (live dashboard only) downloads every sample so far, with the payload columns
  when `--payload` is on.
- **PDF** (live dashboard only) prints the whole report, all tabs included, using
  Chrome or Chromium on the same machine.
- **PNG** on each chart (Bitrate, Packet loss, Round-trip time) saves the chart as an image.

Codec-change events in the Events tab expand to show each field that changed, with the
stream, the value before and the value after. Only real changes are listed: values the
probe could not see yet, and the bit rate (which moves every probe), are left out.

### 4d. Config checks and chart interaction

The **Config** tab checks the stream's own metadata for mismatches and says what each one
does on screen and what to check at the sender. Examples:

- HDR transfer (HLG or PQ) with primaries that are not BT.2020
- HDR at 8 bits: banding and clipping are likely
- transfer function not signalled on a 10-bit or BT.2020 stream: the decoder guesses, which
  is the most likely cause of clipping and banding on a 10-bit feed
- BT.2020 primaries with a non-BT.2020 matrix
- a profile that does not allow the bit depth or chroma (for example High with 10 bits)
- a level too low for the resolution and frame rate
- audio not at 48 kHz, or not AAC

These checks read the metadata only. Clipping and banding are in the picture, so finding
them needs a picture analysis, which this version does not do.

**Picture checks** decode a few frames from the stream about every 15 seconds and measure
the luma levels. They report:

- pixels piled at the peak level (clipping)
- pixels piled at the black floor (crushed shadows)
- levels above legal white or below legal black for limited-range video, which receivers
  that expect limited range clip

They are likelihoods from sampled frames, not a full picture review. Banding from 8-bit
material is not detected: after encoding, the lossy codec adds new code values, so the
signature cannot be seen reliably. Picture checks need ffmpeg, and each sample adds a short
decode on the monitoring machine.

The charts are drawn on a canvas. Hover for a crosshair with the clock time, time into the
run, and each value. Drag across a chart to zoom, double-click or press Reset to go back.
The zoom is kept across live refreshes. Runs longer than 2,000 samples are thinned evenly
for the page, so hover values are the nearest kept sample.

Stream details are merged across probes: a probe that misses the SPS no longer blanks
profile, chroma or colour. The probe reads about 6 seconds of stream at 20 Mbps.

### 5. Interactive view (`ui`)

`srtdebug ui` is a full-screen view. Settings can be changed while the stream runs
(stats cadence, verbosity, export, display). Keys:

- `c` run the preflight check
- `e` toggle export
- `v` toggle verbose fields
- `space` pause or resume the display
- `R` write a dashboard report of the session so far (asks for a file name)
- `q` quit

### 6. Dashboard report (`report`)

```bash
srtdebug report run.csv                        # writes run.html
srtdebug report run.jsonl --out run.pdf        # PDF, rendered with headless Chrome
srtdebug report run.csv --customer "ACME" --target-mbps 8 --redact
```

The report has a PASS / WARN / FAIL verdict with the reasons, summary cards,
health gauges, bitrate and loss charts, the readiness checks and the event log.
When payload data is present, it also has the stream contents, and the suspected
causes. `--redact` leaves the host and streamid out of the report, for sharing.

PDF output needs Google Chrome or Chromium on the machine. The tool searches for
the usual executables and prints a clear message when none is found.

monitor's `--report FILE` writes the same dashboard when the run ends (`.html` or `.pdf`).

## Options

| Option | Meaning |
|---|---|
| `--mode caller\|listener` | Direction of our connection (required) |
| `--port N` | SRT UDP port (required) |
| `--host H` | Caller: remote address. Listener: local bind address (default 0.0.0.0) |
| `--interval S` | Seconds between samples (default 1) |
| `--latency MS` | SRT latency, default 20 ms. The sender must use a compatible latency; SRT uses the larger of the two |
| `--passphrase [TEXT]` | Encrypt the stream (10-80 characters, must match the sender). Without TEXT you are prompted |
| `--passphrase-env VAR` | Read the passphrase from environment variable VAR (keeps it out of shell history) |
| `--pbkeylen 16\|24\|32` | AES key length in bytes (default: SRT's default) |
| `--streamid ID` | SRT streamid, if the sender uses one |
| `--export FILE` | Log samples to CSV or JSONL |
| `--duration S` | Stop after S seconds |
| `--stats-every N` | srt-live-transmit stats cadence in packets (default 20). Lower for low bitrates, otherwise status may show NO DATA. |
| `-v` | Show extra fields |
| `--no-reconnect` | Exit when the stream ends instead of reconnecting |
| `--skip-check` / `--force` | Skip preflight / start despite FAILs |

Set `NO_COLOR=1` to turn colour off.

## Latency and encryption

- **Latency:** SRT recovers lost packets within its latency window. SRT recommends at least 3-4 x the round-trip time.
  The default is 20 ms, which only suits paths with RTT under about 6 ms (typically the same LAN). For a WAN, measure the
  RTT, then set `--latency` to at least 3 x RTT. The report warns when measured RTT times 3 exceeds the latency.
  The effective latency is the larger of your setting and the sender's, so the sender must also allow it.
- **Passphrase:** both ends must use the same passphrase and key length, or the connection carries no data.
  The passphrase is never written to the report, the logs or the browser status. Prefer `--passphrase` with no value, or
  `--passphrase-env`, over typing it on the command line.

## Notes and limits

- **Outages and bursts.** When no stats arrive for a full interval, the rate
  table records 0 Mbps for that second, so a dropout stays visible in the table and
  the export. The first sample after a gap starts a fresh baseline, so a burst after
  the link recovers is not averaged over the outage. Packets held longer than the
  SRT latency are dropped by the receiver, so a burst that outlasts the latency is
  lost rather than delivered late.
- **Caller and listener are about who opens the connection**, not who sends
  the data. In both modes `srtdebug` receives the stream and measures it.
  To send a test stream, use `srt-live-transmit` or your encoder.
- **Firewall check is a best-effort reading of the rules.** Full detail needs
  `sudo`. The iptables check matches `--dport N` rules only, not multiport or range
  rules. On Ubuntu, `iptables` is usually the nftables-backed shim, so it also covers
  most nftables setups. The only real test of inbound reachability is an actual
  connection from the remote side.
- **Loopback self-test** uses `127.0.0.1` and needs the port to be free.
- **Counters reset on reconnect** in srt-live-transmit. `srtdebug` carries the
  totals across reconnects in the summary.
- `srt-live-transmit` only accepts `file://con` (stdin/stdout) for file URIs, so
  received data is discarded to /dev/null. The tool measures it without saving it.

## Verified

Tested on this machine (Ubuntu 24.04, srt-tools 1.5.3, Python 3.13). macOS and WSL are not yet tested:
- readiness check, including a loopback test that passes 362 MB
- a temporary iptables DROP rule was detected and blocked the start, then removed
- monitor as listener against a paced 5 Mbps sender: measured 5.15 Mbps, 0 loss,
  CSV export correct
- no-sender waiting state, and the interactive redraw in a pseudo-terminal

Not yet tested on Ubuntu 22.04 itself. The code avoids Python 3.11+ features,
and `srt-tools` is packaged there too, but please run `srtdebug check` on the
22.04 machine first.

## Troubleshooting

- `srt-live-transmit not installed`: `sudo apt install srt-tools` (Ubuntu, WSL) or `brew install srt` (macOS)
- Listener shows NO DATA and the sender says connected: check the latency and
  streamid match, and that the sender's port and address are right.
- Monitor shows zero bitrate at a low data rate: lower `--stats-every`.
- Caller can't connect: the remote listener is not up, or a firewall on either
  side blocks UDP. Run `srtdebug check` on the remote box too.
