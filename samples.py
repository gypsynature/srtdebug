"""Test sample generator: short MPEG-TS clips covering frame rates, resolutions and scan types, for
feeding into the monitor over SRT. Built with ffmpeg; each clip is a few seconds long.

What stock ffmpeg can make: progressive at every rate listed, and interlaced with top field first (tff)
or bottom field first (bff). It cannot make PsF (progressive frames carried as field pairs) on its own,
so PsF needs a real capture from a broadcast encoder."""

import os
import re
import shutil
import subprocess

SEGMENT_S = 15

# (name, width, height, fps as an ffmpeg rate string, codec, scan, field order)
# scan: "p" progressive, "i" interlaced. field order applies to interlaced only: "tt" or "bb".
FORMATS = [
    # progressive, HD and UHD at the usual frame rates
    ("1080p23.976",  1920, 1080, "24000/1001", "h264", "p", None),
    ("1080p25",      1920, 1080, "25",         "h264", "p", None),
    ("1080p30",      1920, 1080, "30",         "h264", "p", None),
    ("1080p50",      1920, 1080, "50",         "h264", "p", None),
    ("1080p59.94",   1920, 1080, "60000/1001", "h264", "p", None),
    ("720p25",       1280, 720,  "25",         "h264", "p", None),
    ("720p50",       1280, 720,  "50",         "h264", "p", None),
    ("720p59.94",    1280, 720,  "60000/1001", "h264", "p", None),
    ("2160p25",      3840, 2160, "25",         "h264", "p", None),
    ("640x360p25",   640,  360,  "25",         "h264", "p", None),
    # standard definition, MPEG-2
    ("576p25",       720,  576,  "25",         "mpeg2", "p", None),
    ("480p29.97",    720,  480,  "30000/1001", "mpeg2", "p", None),
    # interlaced: the number after i is the field rate, two fields per frame
    ("1080i50_tff",  1920, 1080, "25",         "h264", "i", "tt"),
    ("1080i50_bff",  1920, 1080, "25",         "h264", "i", "bb"),
    ("1080i59.94_tff", 1920, 1080, "30000/1001", "h264", "i", "tt"),
    ("1080i59.94_bff", 1920, 1080, "30000/1001", "h264", "i", "bb"),
    ("576i50_tff",   720,  576,  "25",         "mpeg2", "i", "tt"),
    ("576i50_bff",   720,  576,  "25",         "mpeg2", "i", "bb"),
    ("480i59.94_tff", 720, 480,  "30000/1001", "mpeg2", "i", "tt"),
    ("480i59.94_bff", 720, 480,  "30000/1001", "mpeg2", "i", "bb"),
]


def _double_rate(rate):
    """Frame rate string doubled, for building interlaced frames from fields: 25 -> 50, 30000/1001 -> 60000/1001."""
    if "/" in rate:
        n, d = rate.split("/")
        return f"{int(n) * 2}/{d}"
    return str(int(rate) * 2)


# Test tone: 1 kHz sine, peak at -18 dBFS (the usual alignment level), 48 kHz stereo.
TONE_HZ = 1000
TONE_DB = -18
TONE_AMP = round(10 ** (TONE_DB / 20), 6)   # 0.125893 for -18 dBFS
PCR_MS = 40                              # PCR repetition in milliseconds


def _gop(fps):
    """Frames between keyframes: one second at this frame rate ('25' or '24000/1001'). One second keeps each
    clip within the feed's keyframe limit, so the feed sends it without re-encoding."""
    n, _, d = str(fps).partition("/")
    return round(int(n) / (int(d) if d else 1))


def _command(spec, out):
    name, w, h, fps, codec, scan, order = spec
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if scan == "i":
        # build each frame from two consecutive source frames, one per field
        src_rate = _double_rate(fps)
        field = "interleave_top" if order == "tt" else "interleave_bottom"
        vsrc = f"testsrc2=size={w}x{h}:rate={src_rate}"
        vf = f"scale={w}:{h},tinterlace=mode={field}"
        top = "1" if order == "tt" else "0"
    else:
        vsrc = f"testsrc2=size={w}x{h}:rate={fps}"
        vf = None
        top = None
    # video input 0, audio input 1
    cmd += ["-f", "lavfi", "-i", vsrc, "-f", "lavfi", "-i", f"aevalsrc={TONE_AMP}*sin(2*PI*{TONE_HZ}*t):s=48000:c=stereo"]
    cmd += ["-t", str(SEGMENT_S)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-map", "0:v", "-map", "1:a"]
    if codec == "h264":
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", _bitrate(w, h, fps)]
        if scan == "i":
            cmd += ["-flags", "+ilme+ildct"]
            # -top does not set bottom-field-first in H.264; x264's bff flag does
            cmd += ["-x264-params", "bff=1"] if order == "bb" else ["-top", "1"]
        cmd += ["-g", str(_gop(fps)), "-keyint_min", str(_gop(fps)), "-force_key_frames", "expr:gte(t,n_forced*1)"]
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c:v", "mpeg2video", "-b:v", "8M" if w >= 1280 else "5M", "-g", str(_gop(fps)),
                "-force_key_frames", "expr:gte(t,n_forced*1)"]
        if scan == "i":
            cmd += ["-flags", "+ilme+ildct", "-top", top]
        cmd += ["-c:a", "mp2", "-b:a", "192k"]
    # broadcast timing: PCR at least every 40 ms (DVB TR 101 290 limit), PAT and PMT every 100 ms, SDT every 500 ms
    cmd += ["-pcr_period", str(PCR_MS), "-pat_period", "0.1", "-sdt_period", "0.5"]
    cmd += ["-f", "mpegts", out]
    return cmd


def _check(path):
    """Raises if the clip lacks a video or audio stream (for example, a failed or erased write)."""
    if not shutil.which("ffprobe"):
        return
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    kinds = {k.strip(",") for k in out.stdout.split()}
    if out.returncode != 0 or not {"video", "audio"} <= kinds:
        raise RuntimeError(f"{os.path.basename(path)} did not check out (video and audio are needed)")


def _bitrate(w, h, fps):
    pixels = w * h
    kbps = 2000 if pixels <= 640 * 360 else 6000 if pixels <= 1280 * 720 else 12000 if pixels <= 1920 * 1080 else 30000
    return f"{kbps}k"


def make(outdir, log=print):
    """Write one clip per format into outdir, plus one combined clip that changes format between clips.
    Each clip is written under a temporary name and only renamed once it checks out, so an
    interrupted or failed run never leaves a broken clip under a real name.
    Returns the list of files written. Requires ffmpeg."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is needed to make samples")
    os.makedirs(outdir, exist_ok=True)
    written = []
    for spec in FORMATS:
        out = os.path.join(outdir, f"{spec[0]}.ts")
        tmp = out + ".part"
        log(f"making {spec[0]}")
        try:
            subprocess.run(_command(spec, tmp), check=True)
            _check(tmp)
            os.replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        written.append(out)
    # No combined "all formats" clip: joining the clips' bytes makes a stream with conflicting parameter sets
    # and tables, which receivers cannot decode. Each format has its own clip instead.
    return written


# ---- Custom clips: pick the source options yourself -----------------------------------------------
# Every choice is checked against these lists before anything reaches ffmpeg.

CUSTOM_VIDEO = {"h264": "libx264", "hevc": "libx265", "mpeg2": "mpeg2video"}
CUSTOM_SIZES = {
    "640x360": (640, 360), "720x480": (720, 480), "720x576": (720, 576),
    "1280x720": (1280, 720), "1920x1080": (1920, 1080), "3840x2160": (3840, 2160),
}
# MPEG-2 stays at standard definition up to 1080 lines; 4K is not a valid MPEG-2 size for broadcast use.
CUSTOM_SIZES_MPEG2 = {"720x480", "720x576", "1280x720", "1920x1080"}
CUSTOM_RATES = {"23.976": "24000/1001", "25": "25", "29.97": "30000/1001", "50": "50", "59.94": "60000/1001"}
CUSTOM_LAYOUTS = {"mono": 1, "stereo": 2, "quad": 4, "5.1": 6}
# Which channel layouts each audio codec can carry in this build.
CUSTOM_AUDIO = {
    "aac":  {"layouts": ("mono", "stereo", "quad", "5.1"), "kbps": {"mono": 64, "stereo": 128, "quad": 256, "5.1": 384}},
    "mp2":  {"layouts": ("mono", "stereo"),                 "kbps": {"mono": 96, "stereo": 192}},
    "ac3":  {"layouts": ("mono", "stereo", "5.1"),          "kbps": {"mono": 96, "stereo": 192, "5.1": 448}},
    "eac3": {"layouts": ("mono", "stereo", "5.1"),          "kbps": {"mono": 96, "stereo": 192, "5.1": 448}},
}
CUSTOM_AUDIO_NAME = {"aac": "AAC", "mp2": "MP2", "ac3": "AC-3", "eac3": "E-AC-3"}
CUSTOM_AUDIO_CODEC = {"aac": "aac", "mp2": "mp2", "ac3": "ac3", "eac3": "eac3"}
CUSTOM_TRACKS = (1, 2, 3, 4)
CUSTOM_SECONDS = (5, 300)
CUSTOM_NAME_MAX = 60


def custom_spec(body):
    """Checks the choices from the custom clip dialog and returns a clean spec. Raises ValueError with a
    sentence the person can act on."""
    if not isinstance(body, dict):
        raise ValueError("The custom clip request was not understood.")
    codec = str(body.get("codec") or "")
    if codec not in CUSTOM_VIDEO:
        raise ValueError("Choose a video codec: H.264, HEVC or MPEG-2.")
    size = str(body.get("size") or "")
    if size not in CUSTOM_SIZES:
        raise ValueError("Choose a resolution from the list.")
    if codec == "mpeg2" and size not in CUSTOM_SIZES_MPEG2:
        raise ValueError("MPEG-2 is offered at 720x480, 720x576, 1280x720 and 1920x1080 only.")
    rate = str(body.get("rate") or "")
    if rate not in CUSTOM_RATES:
        raise ValueError("Choose a frame rate from the list.")
    audio = str(body.get("audio") or "")
    if audio not in CUSTOM_AUDIO:
        raise ValueError("Choose an audio codec: AAC, MP2, AC-3 or E-AC-3.")
    layout = str(body.get("layout") or "")
    if layout not in CUSTOM_LAYOUTS:
        raise ValueError("Choose a channel layout: mono, stereo, quad (4 channels) or 5.1.")
    if layout not in CUSTOM_AUDIO[audio]["layouts"]:
        raise ValueError(f"{CUSTOM_AUDIO_NAME[audio]} cannot carry a {layout} layout in this build. Choose another layout or codec.")
    try:
        tracks = int(body.get("tracks", 1))
        seconds = int(body.get("seconds", SEGMENT_S))
    except (TypeError, ValueError):
        raise ValueError("Audio tracks and duration must be whole numbers.")
    if tracks not in CUSTOM_TRACKS:
        raise ValueError("Audio tracks must be 1 to 4.")
    lo, hi = CUSTOM_SECONDS
    if not lo <= seconds <= hi:
        raise ValueError(f"Duration must be {lo} to {hi} seconds.")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(body.get("name") or "")).strip(".-")[:CUSTOM_NAME_MAX]
    if not stem:
        stem = f"{codec}_{size}_{rate.replace('/', '-')}_{audio}_{layout}x{tracks}_{seconds}s"
    return {"codec": codec, "size": size, "rate": rate, "audio": audio, "layout": layout,
            "tracks": tracks, "seconds": seconds, "file": f"custom-{stem}.ts"}


def _custom_tone_expr(first_channel, channels):
    """One sine tone per channel, each at its own pitch so every channel can be told apart on a meter."""
    return "|".join(f"{TONE_AMP}*sin(2*PI*{500 + 250 * (first_channel + j)}*t)" for j in range(channels))


def custom_command(spec, out):
    w, h = CUSTOM_SIZES[spec["size"]]
    rate = CUSTOM_RATES[spec["rate"]]
    gop = _gop(rate)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={rate}"]
    layout, tracks = spec["layout"], spec["tracks"]
    channels = CUSTOM_LAYOUTS[layout]
    for t in range(tracks):
        cmd += ["-f", "lavfi", "-i",
                f"aevalsrc=exprs={_custom_tone_expr(t * channels, channels)}:s=48000:c={layout}"]
    cmd += ["-t", str(spec["seconds"]), "-map", "0:v"]
    for t in range(tracks):
        cmd += ["-map", f"{t + 1}:a"]
    bitrate = _bitrate(w, h, rate)
    if spec["codec"] == "h264":
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", bitrate]
    elif spec["codec"] == "hevc":
        cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-b:v", bitrate, "-x265-params", "log-level=error"]
    else:
        cmd += ["-c:v", "mpeg2video", "-b:v", bitrate]
    cmd += ["-g", str(gop), "-keyint_min", str(gop), "-force_key_frames", "expr:gte(t,n_forced*1)"]
    for t in range(tracks):
        kbps = CUSTOM_AUDIO[spec["audio"]]["kbps"][layout]
        cmd += [f"-c:a:{t}", CUSTOM_AUDIO_CODEC[spec["audio"]], f"-b:a:{t}", f"{kbps}k"]
    cmd += ["-pcr_period", str(PCR_MS), "-pat_period", "0.1", "-sdt_period", "0.5"]
    cmd += ["-f", "mpegts", out]
    return cmd


def make_custom(spec, outdir, log=print):
    """Writes one custom clip into outdir under the name the spec gives. Same safety steps as make():
    temporary name, check, then rename. Returns the path written. Requires ffmpeg."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is needed to make a custom clip")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, spec["file"])
    tmp = out + ".part"
    log(f"making {spec['file']}")
    try:
        subprocess.run(custom_command(spec, tmp), check=True)
        _check(tmp)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return out


def estimated_mb():
    """About how much the make step writes: one clip per format, plus the combined clip that repeats them all."""
    total = 0.0
    for name, w, h, fps, codec, scan, order in FORMATS:
        video = int(_bitrate(w, h, fps)[:-1]) * 1000 if codec == "h264" else (8_000_000 if w >= 1280 else 5_000_000)
        audio = 128_000 if codec == "h264" else 192_000
        total += (video + audio) * SEGMENT_S / 8
    return round(total * 2 / 1_000_000)


def list_formats():
    return [spec[0] for spec in FORMATS]
