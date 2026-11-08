"""Test sample generator: short MPEG-TS clips covering frame rates, resolutions and scan types, for
feeding into the monitor over SRT. Built with ffmpeg; each clip is a few seconds long.

What stock ffmpeg can make: progressive at every rate listed, and interlaced with top field first (tff)
or bottom field first (bff). It cannot make PsF (progressive frames carried as field pairs) on its own,
so PsF needs a real capture from a broadcast encoder."""

import os
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
