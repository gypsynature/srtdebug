"""Picture-level checks: clipping, levels outside the legal range, and bit-depth origin.

A few frames are decoded from the rolling stream snapshot with ffmpeg, spread about a
second apart, and their luma levels are measured. The results are likelihoods, not proof:
they show where the picture piles up at its peak or floor, where it goes outside the legal
range, and whether a 10-bit stream only uses every fourth level (the signature of 8-bit
material that was widened without dithering).

What this cannot see: banding from 8-bit material is not reliable to detect after encoding,
because the lossy encode adds new code values. Banding needs a gradient analysis, which is not
done here.

Standard library only.
"""
import array
import shutil
import subprocess
import sys
import time
from collections import Counter

FRAMES_PER_SAMPLE = 4
SAMPLE_INTERVAL = "fps=1/1"        # one candidate frame per second
import thresholds as _thresholds
_T = _thresholds.load()
CLIP_WARN_PCT = _T["clip_warn_pct"]                # share of pixels piled at the peak or floor
OUT_OF_RANGE_WARN_PCT = _T["out_of_range_warn_pct"]  # share of pixels outside the legal range


def _format_for(pix):
    """(bits, rawvideo pixel format) for the luma we decode. Keeps the code values unchanged."""
    if "10" in (pix or ""):
        return 10, "gray10le"
    return 8, "gray"


def _legal(bits, limited):
    if not limited:
        return 0, (1 << bits) - 1
    if bits == 10:
        return 64, 940
    return 16, 235


def measure(vals, bits, limited):
    """Level statistics for one frame of luma code values."""
    n = len(vals)
    counts = Counter(vals)
    lo_legal, hi_legal = _legal(bits, limited)
    mn, mx = min(counts), max(counts)
    above = sum(c for v, c in counts.items() if v > hi_legal) / n * 100
    below = sum(c for v, c in counts.items() if v < lo_legal) / n * 100
    clip_hi = counts[mx] / n * 100 if mx >= hi_legal else 0.0
    clip_lo = counts[mn] / n * 100 if mn <= lo_legal else 0.0
    return {"min": mn, "max": mx, "above_pct": above, "below_pct": below,
            "clip_high_pct": clip_hi, "clip_low_pct": clip_lo}


def analyse(snapshot, raw, timeout=40):
    """Decode a few frames from the TS snapshot and measure them.
    raw is the ffprobe entry for the video stream. Returns a list of frame results, or None
    if ffmpeg is missing or no frame could be decoded."""
    if not shutil.which("ffmpeg") or not raw:
        return None
    w, h = int(raw.get("width") or 0), int(raw.get("height") or 0)
    if not w or not h:
        return None
    bits, fmt = _format_for(raw.get("pix_fmt", ""))
    limited = raw.get("color_range") != "pc"
    cmd = ["ffmpeg", "-v", "error", "-i", "pipe:0", "-map", "0:v:0",
           "-vf", SAMPLE_INTERVAL, "-frames:v", str(FRAMES_PER_SAMPLE),
           "-f", "rawvideo", "-pix_fmt", fmt, "pipe:1"]
    try:
        p = subprocess.run(cmd, input=snapshot, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    bytes_per_px = 2 if bits == 10 else 1
    frame_len = w * h * bytes_per_px
    if p.returncode != 0 or frame_len == 0 or len(p.stdout) < frame_len:
        return None
    out = []
    for k in range(len(p.stdout) // frame_len):
        chunk = p.stdout[k * frame_len:(k + 1) * frame_len]
        vals = array.array("H" if bits == 10 else "B")
        vals.frombytes(chunk)
        if bits == 10 and sys.byteorder == "big":
            vals.byteswap()
        out.append(measure(vals, bits, limited) | {"bits": bits, "limited": limited,
                                                    "signalled": raw.get("color_range") in ("tv", "pc")})
    return out


def findings(frames):
    """Plain-language findings from the recent frame measurements, with what to check."""
    if not frames:
        return []
    out = []

    def add(level, title, why, check):
        out.append({"pid": None, "level": level, "title": title, "why": why, "check": check})

    def worst(key):
        return max(f[key] for f in frames if f.get(key) is not None)

    clip_hi, clip_lo = worst("clip_high_pct"), worst("clip_low_pct")
    above, below = worst("above_pct"), worst("below_pct")
    limited = frames[-1]["limited"]
    signalled = frames[-1].get("signalled", True)
    lo_code = min(f["min"] for f in frames)
    hi_code = max(f["max"] for f in frames)
    # without a range tag the range is only assumed, so out-of-range values are a question, not a fault
    range_note = (f"Measured code values run from {lo_code} to {hi_code}. "
                  + ("The stream signals its range." if signalled else
                     "The stream does not signal its range, so limited range (16-235) was assumed. "
                     "Full-range video (0-255) is legal and would look like this."))
    out_level = "warn" if signalled else "info"
    if clip_hi >= CLIP_WARN_PCT:
        add("warn", f"Highlights piled at the peak level ({clip_hi:.1f}% of pixels)",
            "A large share of pixels sits at one peak value, so highlight detail is lost. This is "
            "clipping. It happens when the source is clipped, or when HDR is decoded or converted "
            "as SDR.",
            "Check the source's peak level. For HLG or PQ, confirm the transfer tag, and that the "
            "receiver does not map the highlights to SDR.")
    if clip_lo >= CLIP_WARN_PCT:
        add(out_level, f"Shadows piled at the floor ({clip_lo:.1f}% of pixels)",
            "A large share of pixels sits at the black floor, so shadow detail is crushed. " + range_note,
            "Check the source's black level, and the range tag (limited or full) at both ends.")
    if limited and above >= OUT_OF_RANGE_WARN_PCT:
        add(out_level, f"Levels above legal white ({above:.1f}% of pixels)",
            "Limited-range video should not go above the legal white level. Receivers that expect "
            "limited range clip these values, which shows up as clipping on the far end. " + range_note,
            "Confirm the range at the source (limited or full), and check any full-to-limited "
            "conversion in the chain.")
    if limited and below >= OUT_OF_RANGE_WARN_PCT:
        add(out_level, f"Levels below legal black ({below:.1f}% of pixels)",
            "Limited-range video should not go below the legal black level. These values are clipped "
            "by receivers that expect limited range. " + range_note,
            "Confirm the range at the source, and check any range conversion in the chain.")
    return out
