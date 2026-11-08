"""How much CPU and memory this monitor uses: its own process plus the helpers it starts (srt-live-transmit
and, when the video is on, ffmpeg). Read-only and needs no sudo.

Linux and WSL read /proc; macOS reads ps. Anything that cannot be read is left out, never guessed.
The figure covers only these processes, not the rest of the PC.

Standard library only.
"""
import os
import subprocess
import sys
import time

try:
    _TICK = os.sysconf("SC_CLK_TCK")
except (AttributeError, ValueError, OSError):
    _TICK = 100


def _linux(pid):
    """(cpu seconds used so far, resident memory in kB) for a process, or None."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
        fields = data[data.rindex(")") + 2:].split()     # fields after the command name; state is first
        cpu = (int(fields[11]) + int(fields[12])) / _TICK  # utime and stime (fields 14 and 15)
        rss = None
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                    break
        return cpu, rss
    except (OSError, ValueError, IndexError):
        return None


def _parse_cputime(text):
    """ps cputime is [[dd-]hh:]mm:ss[.ff] (macOS)."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    parts = [float(p) for p in text.split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return days * 86400 + secs


def _mac(pid):
    try:
        out = subprocess.run(["ps", "-o", "cputime=,rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3).stdout.split()
        if len(out) < 2:
            return None
        return _parse_cputime(out[0]), int(out[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read(pid):
    if sys.platform == "darwin":
        return _mac(pid)
    return _linux(pid)


class Usage:
    """Keeps the last reading so CPU can be given as a rate between two samples."""

    def __init__(self):
        self.prev = {}       # pid -> cpu seconds at the last sample
        self.prev_t = None   # when that sample was taken

    def sample(self, pids):
        """CPU percent of one core, and resident memory (MB), over the given processes. The first sample
        has no rate yet, so its cpu_pct is None."""
        now = time.monotonic()
        cpu_now, rss_kb, found = {}, 0, 0
        for pid in pids:
            got = read(pid)
            if got is None:
                continue
            cpu_now[pid] = got[0]
            if got[1] is not None:
                rss_kb += got[1]
            found += 1
        if not found:
            self.prev, self.prev_t = {}, None
            return None
        cpu_pct = None
        if self.prev_t is not None and now > self.prev_t:
            used = sum(max(0.0, cpu_now[p] - self.prev[p]) for p in cpu_now if p in self.prev)
            cpu_pct = 100.0 * used / (now - self.prev_t)
        self.prev, self.prev_t = cpu_now, now
        return {"cpu_pct": cpu_pct, "rss_mb": rss_kb / 1024.0, "processes": found,
                "cores": os.cpu_count() or 1, "time": time.strftime("%H:%M:%S UTC", time.gmtime())}
