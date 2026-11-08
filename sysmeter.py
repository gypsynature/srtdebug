"""CPU and memory of this computer, sampled once a second, for the meter in the top bar.

Read from /proc, so it works on Linux and under WSL. Under WSL the numbers are for the WSL virtual machine,
not the whole Windows PC. Elsewhere the meter says it is not available rather than showing made-up numbers.

Also reports how much of the CPU the feed and the monitor themselves are using, so the user can tell
their own work from everything else on the machine.
"""

import os
import threading
import time
from collections import deque

KEEP = 60            # seconds of history for the graph
INTERVAL_S = 1.0     # sampling interval; reading /proc once a second costs almost nothing


def _cpu_times():
    """(busy, total) jiffies summed over all cores, from the first line of /proc/stat."""
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts[:8]]
    user, nice, system, idle, iowait, irq, softirq, steal = (vals + [0] * 8)[:8]
    idle_all = idle + iowait
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return total - idle_all, total


def _mem():
    """(used fraction, total GB, used GB) from /proc/meminfo, counting cache as free like top does."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0])          # kB
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - avail
    return used / total, total / 1048576.0, used / 1048576.0


def _proc_ticks(pid):
    """utime + stime of one process, in jiffies, or None if it has gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return None


class Meter:
    def __init__(self, pids=lambda: []):
        self.available = os.path.exists("/proc/stat") and os.path.exists("/proc/meminfo")
        self.pids = pids                          # callable returning the pids of this program's processes
        self.hist = deque(maxlen=KEEP)            # (cpu fraction, memory fraction, ours fraction)
        self.now = {}
        self.lock = threading.Lock()
        self._prev_cpu = None
        self._prev_ours = {}

    def sample(self):
        busy, total = _cpu_times()
        mem_frac, mem_total, mem_used = _mem()
        ours_ticks = {}
        for pid in self.pids():
            t = _proc_ticks(pid)
            if t is not None:
                ours_ticks[pid] = t
        cpu = ours = 0.0
        if self._prev_cpu is not None:
            d_busy, d_total = busy - self._prev_cpu[0], total - self._prev_cpu[1]
            if d_total > 0:
                cpu = max(0.0, min(1.0, d_busy / d_total))
                d_ours = sum(t - self._prev_ours.get(pid, t) for pid, t in ours_ticks.items())
                ours = max(0.0, min(1.0, d_ours / d_total))
        self._prev_cpu = (busy, total)
        self._prev_ours = ours_ticks
        with self.lock:
            self.now = {"available": True, "cpu": round(cpu * 100, 1), "mem": round(mem_frac * 100, 1),
                        "mem_used_gb": round(mem_used, 1), "mem_total_gb": round(mem_total, 1),
                        "ours": round(ours * 100, 1)}
            self.hist.append((round(cpu * 100, 1), round(mem_frac * 100, 1), round(ours * 100, 1)))

    def snapshot(self):
        with self.lock:
            if not self.available:
                return {"available": False, "history": []}
            return dict(self.now, history=list(self.hist))

    def start(self):
        if not self.available:
            return

        def loop():
            while True:
                try:
                    self.sample()
                except (OSError, ValueError, KeyError, IndexError):
                    pass
                time.sleep(INTERVAL_S)

        threading.Thread(target=loop, daemon=True).start()
