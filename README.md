# srtdebug

An inspector and test tool for SRT streams, built on `srt-live-transmit`.

- **Decode:** receive a stream and check its health. It runs a readiness check, then
  shows live bitrate, loss, round-trip time and buffer health, and produces reports
  you can keep.
- **Encode:** send a test clip, or your own video, over SRT to a receiver, so you can
  check the far end.

It works two ways:

- **Browser control panel (GUI).** Open **srtdebug** from the app menu (or run
  `srtdebug-panel`). Configure, start and stop monitoring or sending, read the live
  dashboard and download reports, all in your browser.
- **Command line.** Everything the panel does is also available from the `srtdebug`
  command, for scripts, remote machines and terminal use.

Python 3 standard library only. Requires the `srt-tools` package.

## Install

Download the latest release from the Releases page of this repository. Pick the file for your computer.

### Ubuntu

1. Download the `.deb` file from the Releases page.
2. Double-click it, or open a terminal in the folder where you saved it and run `sudo apt install ./` followed by the file's name.
3. Open **srtdebug** from the app menu, or run `srtdebug-panel`. The control panel opens in your browser.

The installer adds the tools srtdebug needs (ffmpeg and the SRT tools). Nothing else is required.

### Windows (through WSL)

srtdebug runs inside WSL, the Ubuntu environment for Windows. This section assumes WSL with Ubuntu is already installed and working.

1. Download the `.deb` file from the Releases page and double-click it, or run `sudo apt install ./` followed by the file's name in Ubuntu.

On Windows the installer also adds **srtdebug** and **Stop srtdebug** buttons to your Desktop and Start menu. It asks whether to pin them to the Start menu and the taskbar. The **srtdebug** button starts the control panel and opens it in your browser, without a terminal window.

Installing a newer version, or reinstalling, refreshes the buttons, so they always start the installed version. You are asked about the Start menu and the taskbar only the first time, or again after the package has been purged.

To remove srtdebug, run `sudo apt purge srtdebug` in Ubuntu. The Windows buttons are removed with it.

### macOS

The Mac download includes `launcher/INSTALL-macOS.txt`, with step-by-step instructions. This route has not been tested on a Mac yet, so please report anything that does not match.

## Start and stop the control panel

| Platform | Start | Stop |
|---|---|---|
| Ubuntu | **srtdebug** in the app menu, or `srtdebug-panel` | **Stop srtdebug** in the app menu, or `srtdebug-panel-stop` |
| Windows (WSL) | **srtdebug** on the Desktop or Start menu | **Stop srtdebug** on the Desktop or Start menu |
| macOS | double-click `Launch srtdebug.command` (the first time, right-click and choose Open) | double-click `Stop srtdebug.command` |
| Any, from a terminal | `srtdebug serve` (Ctrl-C stops it) | Ctrl-C |

The panel opens at `http://localhost:9999/`. If it is already open, starting srtdebug again
brings the existing window forward instead of opening a second copy.

To use a different port, set `SRTDEBUG_PORT` for the launchers, or pass `--port` to `srtdebug serve`.
The panel listens on 127.0.0.1 only and has no login. `srtdebug serve --bind 0.0.0.0` shares it
with your network, and then anyone there can start and stop monitoring, so only do that on a network you trust.

**Quitting.** Press **Quit** in the header to stop the panel. It also stops by itself a few
minutes after its last browser window is closed.

## Using the control panel

The panel opens on a start screen with two buttons: **Decode stream** and **Encode stream**.
Press **‹ Start** in the top bar at any time to come back to it.

### Decode stream

Use this to receive a stream coming in to this computer, or to connect out to one.

1. **Connection.** Choose **Listener** to wait for a sender, or **Caller** to connect to a
   remote listener. Enter the SRT port and the latency (20 ms by default, suitable only for
   short paths; use 3-4 x the round-trip time on a WAN). Enter the remote host for a caller,
   and a stream ID if the sender uses one. **Scheduled test length** (in minutes) is optional:
   the monitor stops by itself after that long.
2. **Security.** If the sender uses a passphrase, enter it here, with the key length (AES-128,
   192 or 256, or the SRT default). The passphrase is used for this run only and is not stored
   or saved in presets.
3. **Analysis.** Leave **Analyse the transport stream** ticked to see codecs, bitrates and
   stream checks (codec detail needs ffmpeg). Tick **Live video in the dashboard** to play the
   stream in the dashboard (needs ffmpeg; about 8 seconds behind the stream).
   Before you start, the panel also runs a **Before you listen** check of this computer.
4. **Presets.** Save the current settings under a name and load them again from the drop-down
   list. Presets are kept on this computer. Passphrases are never saved in a preset.
5. Press **Start monitoring**. The button becomes **Stop monitoring**, and **Open live dashboard**
   opens the live view in a new browser tab.

**Reading the status.** The status is **Healthy** (no warnings or errors in the last 5 seconds),
**Degraded** (at least one warning in that time) or **Failing** (at least one error). Click the
badge to see the issues. The strip below it has one cell per 5 seconds, coloured by the worst
state in that time. Grey means no data. Problems stay listed after they clear.

**Health thresholds.** Under **Health thresholds** you can change the levels that count as a
warning or a failure, or restore the defaults. Changes apply to the next monitor you start, and
each report lists the thresholds it used.

**Stopping and reports.** Press **Stop monitoring**. A summary lists the reports and log to
download: the HTML report, the PDF (if Google Chrome, Chromium or Edge is installed), the CSV,
and the log. **All reports (zip)** gives you everything in one file. A report needs some received
data; with none, you get the CSV and the log.

What is measured:
- **Transport:** bitrate, round-trip time and packet loss. SRT repairs most lost packets by
  retransmitting them, so only packets that arrive too late count as loss in the verdict.
- **Transport stream** (when analysis is on): codecs, bitrates, keyframe interval, rate mode,
  and the ETSI TR 101 290 checks. The likely encoder is the name the sender wrote into the stream.
- **Picture and sound** (needs ffmpeg): black and white levels, loudness, silence and freeze.
  These are noted in the report but do not change the verdict.

### Encode stream

Use this to send a clip out to a receiver, as if it were a live feed. The screen has two steps.

**1. Where it goes**
- **Saved destinations.** Choose a saved destination from the drop-down list, or fill in the
  fields and press **Save** to keep it for next time. Destinations keep the name, address and
  options, but not the passphrase.
- **Address and port.** The IP address or name of the receiving monitor or media server, and
  its port (default 9100). Or enter a full `srt://` address in one field instead.
- **Stream ID** and **Passphrase**, if the receiver needs them. The passphrase is not stored.

**2. Send**
- **Sample clip.** Pick one of the test clips. Press **Make sample clips** to write a set of
  clips, one per video format, into a folder you choose.
- **Your own video** (optional). Drop a file on the box, or type its path. H.264, H.265 or
  MPEG-2 in MP4, MOV, MKV or TS work best.
- **Encoding.** **Send as it is** passes the clip on without re-encoding. **Re-encode to H.264**
  converts it using settings suited to this computer. Use **Check format** to see the clip's
  aspect ratio, scan type and frame rate before sending.
- **Bit rate.** The re-encode bit rate defaults to 6 Mb/s. The panel suggests a limit based on
  this computer's CPU core count, and explains why beside the setting. You can go higher, up
  to 50 Mb/s, but the panel asks you to confirm first because a slow CPU can fall behind.
- **Show a clock on the picture** adds a clock to the video, which helps to tell a frozen feed
  from a live one.
- **Custom settings** holds the less common options: connection mode (caller or listener),
  latency (500 ms by default, or 5000 ms on Wi-Fi), encryption strength (AES-128, 192 or 256,
  used with a passphrase), and the bit rate.
- **Repeat until stopped** loops the clip. Untick it to send once.

Press **Start feed** to send and **Stop feed** to end. **How this is sent** shows the command
that is being run, with the passphrase hidden, and a button to copy it.

**Debug log.** The log shows what happened, with a level picker from warnings to debug output.
**Download** saves the whole log. Changing the level briefly restarts a running feed; the log
carries on. **Clear from view** only hides lines on the screen.

## Command line

Every panel feature has a command-line equivalent. Run `srtdebug --help`, or `srtdebug <command> --help`,
for the full options.

| Command | What it does |
|---|---|
| `srtdebug serve` | start the control panel in a browser |
| `srtdebug check` | check tools, port and firewall before a test |
| `srtdebug monitor` | receive a stream and show live health |
| `srtdebug ui` | full-screen terminal view, with settings you can change while it runs |
| `srtdebug feed` | send a clip over SRT, as caller or listener |
| `srtdebug rtt` | measure the round trip between the two ends and suggest a minimum latency |
| `srtdebug report` | build a dashboard (HTML or PDF) from an export file |
| `srtdebug samples` | make test clips covering frame rates, resolutions and scan types |
| `srtdebug evidence` | extract frames and a report for frozen or black pictures in a recorded TS file |
| `srtdebug deps` | show the external programs srtdebug uses, and install missing ones |
| `srtdebug install-srt` | install `srt-live-transmit` (apt, Homebrew or source build) |

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

The dashboard in the terminal is deliberately small:

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

Add `--view` to `monitor` (or tick **Show the live video** in the control panel) and the
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

The dashboard (live or saved) is split into tabs. **Overview** and **Link** are always there.
With payload analysis on you also get **Stream**, **Anomalies** and **Config**. **Checks** and
**Events** appear when there is something to show, and **Run details and thresholds** is always
last. The open tab is kept in the page address (for example `#events`), so a refresh stays on
the same tab.

- **CSV** (live dashboard only) downloads every sample so far, with the payload columns
  when `--payload` is on.
- **PDF** (live dashboard only) prints the whole report, all tabs included, using
  Chrome or Chromium on the same machine.
- **PNG** on each chart (Bitrate, Packet loss, Round-trip time) saves the chart as an image.

Codec-change events in the Events tab expand to show each field that changed, with the
stream, the value before and the value after. Only real changes are listed: values the
probe could not see yet, and the bit rate (which moves every probe), are left out.

### 4d. Config checks and picture checks

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
them needs a picture analysis.

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

### 5. Full-screen terminal view (`ui`)

`srtdebug ui` is a full-screen view. Settings can be changed while the stream runs,
and each change reconnects where it needs to. Keys:

- `m` switch between listener and caller
- `p` change the port
- `h` change the host (caller)
- `l` change the latency
- `s` change the stream ID
- `+` / `-` change the sample interval
- `[` / `]` change the stats cadence
- `c` run the preflight check
- `e` start or stop export (asks for a file name)
- `v` toggle verbose fields
- `r` reset the counters
- `space` pause or resume the display (counting continues)
- `R` write a dashboard report of the session so far (asks for a file name)
- `q` quit

### 6. Send a clip (`feed`)

`srtdebug feed` is the command-line side of **Encode stream**. It sends a clip, or your own
video, over SRT. It needs `ffmpeg` and `srt-live-transmit`.

```bash
# Check the source's format without sending anything
srtdebug feed --file clip.mp4 --check

# Send to a listener, repeating until Ctrl-C
srtdebug feed --mode caller --host 203.0.113.5 --port 9100 --file clip.mp4 --loop

# The same, with a full SRT address and a passphrase from an environment variable
SRT_PASS='correct horse battery' srtdebug feed --url "srt://203.0.113.5:9000?streamid=live/cam1" \
    --file clip.mp4 --passphrase-env SRT_PASS --loop
```

`--file` takes a clip from `srtdebug samples`, or your own video (MP4, MOV, MKV or TS).
`--url` replaces `--mode`, `--host` and `--port`. `--latency` sets the SRT latency in ms.

### 7. Round trip and latency (`rtt`)

```bash
srtdebug rtt --mode caller --host 10.0.0.5 --port 9000 --duration 120
```

Run it between the two ends of a link. It measures the round trip and suggests a minimum
SRT latency, which is what you need for `--latency` on both ends. A longer `--duration`
gives a steadier figure.

### 8. Test clips (`samples`) and recorded files (`evidence`)

```bash
srtdebug samples                          # clips in ~/srtdebug-samples
srtdebug samples --dir /path/to/folder    # or somewhere else
srtdebug evidence recording.ts            # frames and report.html for frozen or black pictures
```

`samples` writes a set of clips covering common frame rates, resolutions and scan types.
`evidence` pulls out frames from a recorded MPEG-TS file where the picture looks frozen or
black, with a report beside them.

### 9. Dashboard report (`report`)

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

## Install from source (for developers)

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
sudo ln -sf "$PWD/srtdebug.py" /usr/local/bin/srtdebug
srtdebug install-srt                 # apt srt-tools (SRT 1.5.3)
srtdebug install-srt --method source # build Haivision v1.5.7 from GitHub
```

### macOS (from source)

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

### Running the control panel from source

After the symlink above, `srtdebug serve` starts the control panel. The app-menu and
Desktop buttons come from the `.deb` (Ubuntu and WSL) or the macOS download, not from a source checkout.

## Dependencies

`srtdebug` itself uses only the Python standard library. It calls these programs:

| Program | Required | Needed for | Ubuntu / WSL | macOS |
|---|---|---|---|---|
| `srt-live-transmit` | yes | receiving and sending SRT streams | `sudo apt-get install srt-tools` | `brew install srt` |
| `ffmpeg` and `ffprobe` | no | codec, chroma and colour details and picture checks (`--payload`); live video (`--view`); sending clips and re-encoding (Encode stream, `feed`, sample clips) | `sudo apt-get install ffmpeg` | `brew install ffmpeg` |
| Chrome or Chromium | no | PDF reports and the PDF export | install by hand (Ubuntu's `chromium-browser` is a snap wrapper); Google Chrome `.deb` works | `brew install --cask google-chrome` |
| Python 3 | yes | running the tool (tested on 3.13 only) | `python3` | `python3` |

The browser loads hls.js from cdnjs.cloudflare.com for the live video. That needs internet
access from the viewer's browser, not from the monitoring machine.

Check what is installed at any time:

```bash
srtdebug deps             # show each program, whether it is found, and what needs it
srtdebug deps --install   # offer to install whatever is missing
```

When a command or panel action needs an optional program that is missing, `srtdebug` says so
and offers to install it, for example when you start with `--payload` or `--view`, or export a PDF.
Without a terminal it prints the install command instead.

## Options

These are the main options for `monitor`. The other commands share the same `--mode`, `--host`
and `--port` options.

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
| `--web PORT` | Serve the live dashboard on this port |
| `--payload` / `--view` | Transport-stream analysis / live video in the dashboard |
| `--report FILE` | Write the dashboard when the run ends (`.html` or `.pdf`) |
| `-v` | Show extra fields |
| `--no-reconnect` | Exit when the stream ends instead of reconnecting |
| `--skip-check` / `--force` | Skip preflight / start despite FAILs |

Set `NO_COLOR=1` to turn colour off.

## Latency and encryption

- **Latency:** SRT recovers lost packets within its latency window. SRT recommends at least 3-4 x the round-trip time.
  The default is 20 ms, which only suits paths with RTT under about 6 ms (typically the same LAN). For a WAN, measure the
  RTT (`srtdebug rtt`), then set `--latency` to at least 3 x RTT. The report warns when measured RTT times 3 exceeds the latency.
  The effective latency is the larger of your setting and the sender's, so the sender must also allow it.
- **Passphrase:** both ends must use the same passphrase and key length, or the connection carries no data.
  The passphrase is never written to the report, the logs, the browser status or a saved preset. Prefer `--passphrase` with no value, or
  `--passphrase-env`, over typing it on the command line.

## Notes and limits

- **Outages and bursts.** When no stats arrive for a full interval, the rate
  table records 0 Mbps for that second, so a dropout stays visible in the table and
  the export. The first sample after a gap starts a fresh baseline, so a burst after
  the link recovers is not averaged over the outage. Packets held longer than the
  SRT latency are dropped by the receiver, so a burst that outlasts the latency is
  lost rather than delivered late.
- **Caller and listener are about who opens the connection**, not who sends
  the data. In both modes `srtdebug` receives the stream and measures it. To send, use
  **Encode stream**, `srtdebug feed`, `srt-live-transmit` or your encoder.
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
- **The control panel has no login.** Keep it on 127.0.0.1 unless you need it on a network you trust.

## Verified

Tested on this machine (Ubuntu 24.04, srt-tools 1.5.3, Python 3.13). macOS and WSL are not yet tested:
- readiness check, including a loopback test that passes 362 MB
- a temporary iptables DROP rule was detected and blocked the start, then removed
- monitor as listener against a paced 5 Mbps sender: measured 5.15 Mbps, 0 loss,
  CSV export correct
- no-sender waiting state, and the interactive redraw in a pseudo-terminal
- control panel: start screen, saved presets and destinations, and the Encode bit rate
  limit, checked in a headless browser
- the Ubuntu `.deb` installed and removed cleanly here

Not yet tested on Ubuntu 22.04 itself. The code avoids Python 3.11+ features,
and `srt-tools` is packaged there too, but please run `srtdebug check` on the
22.04 machine first.

## Troubleshooting

- `srt-live-transmit not installed`: `sudo apt install srt-tools` (Ubuntu, WSL) or `brew install srt` (macOS)
- The control panel does not open: check the log in `~/.srtdebug/panel.log`. Another program may be
  using port 9999; set `SRTDEBUG_PORT` to a different number for the launchers.
- Listener shows NO DATA and the sender says connected: check the latency and
  streamid match, and that the sender's port and address are right.
- Monitor shows zero bitrate at a low data rate: lower `--stats-every`.
- Caller can't connect: the remote listener is not up, or a firewall on either
  side blocks UDP. Run `srtdebug check` on the remote box too.
- Encode stream stutters at a high bit rate: lower the bit rate, or check that the computer can keep up with the re-encode.

## License

srtdebug is free software, released under the GNU General Public License v3.0 (see `LICENSE`).
It runs `srt-live-transmit` (Mozilla Public License 2.0) and ffmpeg (LGPL / GPL) as separate programs.
Neither is included in this project.
