"""Audio and video content checks on a window of the stream, using ffmpeg's own detectors.

- Loudness to EBU R128: integrated loudness (LUFS) and true peak (dBTP) of the audio.
- Black and frozen video, and silent audio: the durations inside the window.

Each check runs ffmpeg on a short window of the stream bytes. Nothing is kept beyond the result.
"""

import os
import re
import subprocess
import tempfile

LOUDNESS_TARGET_LUFS = -23.0      # EBU R128 programme target
LOUDNESS_TOLERANCE_LU = 1.0       # how far from the target before a warning
TRUE_PEAK_MAX_DBTP = -1.0         # EBU R128 maximum true peak

# ffmpeg filter outputs are read from stderr
_LUFS = re.compile(r"I:\s+(-?[\d.]+)\s+LUFS")
_TRUE_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s+(-?[\d.]+)\s+dBFS", re.S)
_BLACK = re.compile(r"black_duration:([\d.]+)")
_FREEZE = re.compile(r"freeze_duration:\s*([\d.]+)")
_SILENCE = re.compile(r"silence_duration:\s*([\d.]+)")
_BLACK_SPAN = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)")
_FREEZE_START = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_END = re.compile(r"freeze_end:\s*([\d.]+)")


def _run(args, timeout):
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info"] + args,
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return p.stderr, None


def analyse(ts_bytes, workdir, timeout=40):
    """Measure one window of MPEG-TS bytes. Returns a dict; 'error' is set if ffmpeg could not run."""
    path = os.path.join(workdir, "window.ts")
    with open(path, "wb") as f:
        f.write(ts_bytes)
    out = {"lufs": None, "true_peak": None, "black_s": 0.0, "freezes": [], "silence_s": 0.0, "error": None}

    err, e = _run(["-i", path, "-map", "0:a:0?", "-af", "ebur128=peak=true", "-f", "null", "-"], timeout)
    if e:
        out["error"] = e
        return out
    m = _LUFS.findall(err or "")
    if m:
        out["lufs"] = float(m[-1])
    m = _TRUE_PEAK.search(err or "")
    if m:
        out["true_peak"] = float(m.group(1))
    out["silence_s"] = sum(float(x) for x in _SILENCE.findall(
        _run(["-i", path, "-map", "0:a:0?", "-af", "silencedetect=n=-60dB:d=1", "-f", "null", "-"], timeout)[0] or ""))

    err, e = _run(["-i", path, "-map", "0:v:0", "-vf",
                   "scale=320:-2,blackdetect=d=1:pix_th=0.10,freezedetect=n=0.003:d=2",
                   "-f", "null", "-"], timeout)
    if e:
        out["error"] = e
        return out
    out["black_s"] = sum(float(x) for x in _BLACK.findall(err or ""))
    out["freezes"] = [float(x) for x in _FREEZE.findall(err or "")]
    # when each event starts and ends, in seconds from the start of this window
    out["black_events"] = [(float(a), float(b)) for a, b in _BLACK_SPAN.findall(err or "")]
    starts = [float(x) for x in _FREEZE_START.findall(err or "")]
    ends = [float(x) for x in _FREEZE_END.findall(err or "")]
    out["freeze_events"] = [(a, ends[i] if i < len(ends) else None) for i, a in enumerate(starts)]
    return out


def findings(result):
    """Plain-language findings for one analysed window, as (kind, level, text).
    The kind lets the monitor log an event when a problem starts, not on every window."""
    rows = []
    if result.get("error"):
        return [("error", "info", f"content check could not run: {result['error']}")]
    lufs = result.get("lufs")
    # below -60 LUFS is treated as silence (the EBU R128 absolute gate): silence is reported on its own
    if lufs is not None and lufs > -60.0 and abs(lufs - LOUDNESS_TARGET_LUFS) > LOUDNESS_TOLERANCE_LU:
        rows.append(("loudness", "warn", f"loudness {lufs:.1f} LUFS; EBU R128 target is "
                                         f"{LOUDNESS_TARGET_LUFS:.0f} LUFS (+/-{LOUDNESS_TOLERANCE_LU:.0f} LU)"))
    tp = result.get("true_peak")
    if tp is not None and tp > TRUE_PEAK_MAX_DBTP:
        rows.append(("true_peak", "warn", f"true peak {tp:.1f} dBTP is above {TRUE_PEAK_MAX_DBTP:.0f} dBTP "
                                          "(clipping risk)"))
    if result.get("black_s", 0) >= 1:
        rows.append(("black", "warn", f"black picture for {result['black_s']:.1f} s in the last window"))
    if result.get("freezes"):
        rows.append(("freeze", "warn", f"picture frozen for {max(result['freezes']):.1f} s"))
    if result.get("silence_s", 0) >= 1:
        rows.append(("silence", "warn", f"audio silent for {result['silence_s']:.1f} s in the last window"))
    return rows


def window_dir():
    return tempfile.mkdtemp(prefix="srtdebug-content-")
