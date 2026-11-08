"""Round-trip spikes: a reading well above the recent normal, with any packet drops that follow it.

Shared by the sending panel and the monitor, so both report spikes the same way. Readings come every
half second from SRT's statistics. The normal is the median of the recent readings, and spikes are not
allowed to move it, so a long spike cannot make itself look normal."""
import statistics
import time
from collections import deque

FACTOR = 2.0        # a spike is at least this many times the normal...
FLOOR_MS = 40       # ...and at least this many ms above it, so small wobbles on a fast link are ignored
MIN_READINGS = 10   # how many readings before a normal can be judged (5 s at 500 ms)
AFTER_S = 1.0       # drops within this long after a spike count towards it


class SpikeWatch:
    def __init__(self, window=60):
        self.recent = deque(maxlen=window)   # about 30 s of normal readings
        self.active = None                   # the spike happening now
        self.done = []                       # finished spikes, oldest first
        self.last_dropped = None

    def add(self, rtt, dropped_total, now=None):
        """Feed one reading: the round trip in ms and the running total of packets dropped."""
        now = time.time() if now is None else now
        if self.last_dropped is None:
            self.last_dropped = dropped_total
        base = statistics.median(self.recent) if len(self.recent) >= MIN_READINGS else None
        is_spike = base is not None and rtt >= max(base * FACTOR, base + FLOOR_MS)
        if is_spike:
            if self.active is None:
                self.active = {"start": now, "normal": base, "peak": rtt,
                               "dropped_at_start": self.last_dropped, "end": None, "dropped": 0}
            self.active["peak"] = max(self.active["peak"], rtt)
        else:
            if self.active is not None:
                self.active["end"] = now
                self.active["dropped"] = max(dropped_total - self.active["dropped_at_start"], 0)
                self.done.append(self.active)
                self.active = None
            self.recent.append(rtt)
        self.last_dropped = dropped_total

    def last(self):
        return self.done[-1] if self.done else self.active

    def describe(self, spike):
        """One plain sentence about a spike."""
        at = time.strftime("%H:%M:%S", time.localtime(spike["start"]))
        text = "Round trip rose to %d ms at %s, normally about %d ms" % (spike["peak"], at, spike["normal"])
        if spike["dropped"]:
            text += "; %d packets were dropped at the time" % spike["dropped"]
        return text + "."

    def summary(self):
        """Count and the most recent spike, for the reports. {} when there have been none."""
        spikes = self.done + ([self.active] if self.active else [])
        if not spikes:
            return {}
        return {"count": len(spikes), "last": self.describe(spikes[-1]),
                "dropped": sum(s["dropped"] for s in spikes)}
