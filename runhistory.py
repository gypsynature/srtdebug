"""Saved test-run history for srtdebug 3.0.

Each feed or monitor run is written to its own folder, nested by date:

    ~/.srtdebug/runs/YYYY/MM/DD/HH-MM-SS <label>/
        summary.json      start/end, settings used (never secrets), totals, status
        numbers.csv       one row per sample, written as the run goes
        logs/             readable and raw logs for the run
        graphs/           images produced for the run (optional)

The history is a soft-limited collection: 45 runs triggers a warning and
50 runs triggers a prompt, but a new run is never refused.

Standard library only, like the rest of srtdebug.
"""

import csv
import datetime
import json
import os
import collections
import platform
import re
import shutil
import zipfile

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".srtdebug", "runs")
WARN_AT = 45
SOFT_LIMIT = 50
SUMMARY = "summary.json"
NUMBERS = "run.csv"
_SECRET_KEY = re.compile(r"pass|secret|token|private|api_?key", re.IGNORECASE)
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]+")


# ---------------------------------------------------------------- helpers

def human_size(num_bytes):
    """Format a byte count for people: 0 B, 812 B, 1.5 KB, 3.4 GB."""
    if num_bytes is None:
        return "unknown"
    n = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % n
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n  # pragma: no cover - loop always returns


def clean_settings(settings):
    """Return a copy of settings with anything secret-looking removed.

    Passphrases are never written to disk. A boolean records whether
    encryption was on, and its strength, which is not secret.
    """
    if isinstance(settings, dict):
        out = {}
        for key, value in settings.items():
            if _SECRET_KEY.search(str(key)) or str(key).startswith("_"):
                continue   # secrets, and the panel's own internal fields
            out[key] = clean_settings(value)
        return out
    if isinstance(settings, list):
        return [clean_settings(v) for v in settings]
    return settings


def _safe_label(label):
    label = _UNSAFE.sub(" ", str(label or "run")).strip()
    label = re.sub(r"\s+", " ", label)[:60].strip()
    return label or "run"


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def folder_size(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _is_wsl():
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in platform.release().lower()
    except Exception:
        return False


def _inside(path, root):
    real_path = os.path.realpath(path)
    real_root = os.path.realpath(root)
    return real_path != real_root and real_path.startswith(real_root + os.sep)


# ---------------------------------------------------------------- recording

class RunRecorder:
    """Writes one run to disk while it is happening.

    open_run() creates the run folder and a summary marked 'running'. The
    numbers file (run.csv) is written by the sender as the run goes, or by
    add_sample() for runs where srtdebug itself collects the numbers.
    finish() writes the final summary and the totals.
    If the process dies before finish(), recover_interrupted() marks the run
    as ended early the next time the history is opened.
    """

    def __init__(self, root=DEFAULT_ROOT, now=None):
        self.root = root
        self._now = now or datetime.datetime.now
        self.run_dir = None
        self._csv_file = None
        self._writer = None
        self._summary = None
        self._fields = None
        self._tracked = False

    def open_run(self, kind, label, settings):
        if self.run_dir is not None:
            raise RuntimeError("run already started")
        started = self._now()
        day_dir = os.path.join(self.root, started.strftime("%Y"),
                               started.strftime("%m"), started.strftime("%d"))
        name = "%s %s" % (started.strftime("%H-%M-%S"), _safe_label(label))
        run_dir = os.path.join(day_dir, name)
        n = 2
        while os.path.exists(run_dir):
            run_dir = os.path.join(day_dir, "%s-%d" % (name, n))
            n += 1
        os.makedirs(run_dir)
        self.run_dir = run_dir
        self._summary = {
            "kind": kind,
            "label": label,
            "status": "running",
            "started": started.isoformat(timespec="seconds"),
            "ended": None,
            "settings": clean_settings(settings or {}),
            "samples": 0,
            "totals": {"lost": 0, "resent": 0, "dropped": 0,
                       "worst_rtt_ms": None},
            "result": None,
        }
        self._save_summary()
        return run_dir

    def start(self, kind, label, settings, fields):
        """open_run() plus a numbers file that add_sample() writes to."""
        run_dir = self.open_run(kind, label, settings)
        self._fields = list(fields)
        self._tracked = True
        self._csv_file = open(os.path.join(run_dir, NUMBERS), "w",
                              encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._csv_file,
                                      fieldnames=["time"] + self._fields,
                                      extrasaction="ignore")
        self._writer.writeheader()
        self._csv_file.flush()
        return run_dir

    def add_sample(self, values):
        """Write one row. Totals use the cumulative counters in the row."""
        if not self._tracked:
            raise RuntimeError("run was not started with a numbers file")
        row = {"time": self._now().isoformat(timespec="seconds")}
        row.update(values)
        self._writer.writerow(row)
        self._csv_file.flush()
        s = self._summary
        s["samples"] += 1
        t = s["totals"]
        for key, field in (("lost", "lost"), ("resent", "retrans"), ("dropped", "dropped")):
            if values.get(field) is not None:
                t[key] = max(t[key], int(values[field]))
        rtt = values.get("rtt_ms")
        if rtt is not None and (t["worst_rtt_ms"] is None or rtt > t["worst_rtt_ms"]):
            t["worst_rtt_ms"] = rtt
        self._save_summary()

    def write_log(self, name, text):
        self._require_started()
        logs = os.path.join(self.run_dir, "logs")
        os.makedirs(logs, exist_ok=True)
        path = os.path.join(logs, (_safe_label(name) or "log") + ".log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    def finish(self, result, status="complete", report=None):
        """Close the run. Totals come from run.csv when the sender wrote it.
        report is the stream report (health checks, streams, analysis CPU), kept in the summary."""
        self._require_started()
        self._close()
        if not self._tracked:
            self._summary["totals"], self._summary["samples"] = totals_from_csv(
                os.path.join(self.run_dir, NUMBERS))
        if report is not None:
            self._summary["report"] = report
        self._summary["status"] = status
        self._summary["result"] = result
        self._summary["ended"] = self._now().isoformat(timespec="seconds")
        self._save_summary()
        return self.run_dir

    def _save_summary(self):
        _write_json_atomic(os.path.join(self.run_dir, SUMMARY), self._summary)

    def _require_started(self):
        if self.run_dir is None:
            raise RuntimeError("run not started")

    def _close(self):
        if self._csv_file is not None:
            self._csv_file.close()
        self._csv_file = None
        self._writer = None


def totals_from_csv(path):
    """Totals and sample count from a numbers file written by a sender.

    Counters are cumulative in each row, so the largest value is the total.
    Returns (totals, samples). A missing or empty file gives zeros.
    """
    totals = {"lost": 0, "resent": 0, "dropped": 0, "worst_rtt_ms": None}
    samples = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                samples += 1
                for key, field in (("lost", "lost"), ("resent", "retrans"), ("dropped", "dropped")):
                    try:
                        totals[key] = max(totals[key], int(float(row.get(field) or 0)))
                    except ValueError:
                        pass
                try:
                    rtt = float(row.get("rtt_ms") or "")
                except ValueError:
                    continue
                if totals["worst_rtt_ms"] is None or rtt > totals["worst_rtt_ms"]:
                    totals["worst_rtt_ms"] = rtt
    except OSError:
        pass
    return totals, samples


class TransmitFollower:
    """Turns the sender's live statistics into run.csv rows, as the feed runs.

    The sender writes one JSON object per report to a stats file. This reads
    only the new complete lines, adds them up, and appends one row per report
    with cumulative counters, the same shape the monitor writes. It also copies
    the sender's log into the run folder. Call poll() every few seconds.
    """

    FIELDS = ["rtt_ms", "rate_mbps", "latency_ms", "lost", "retrans", "dropped"]

    def __init__(self, run_dir, min_interval=1.0):
        self.run_dir = run_dir
        self.min_interval = min_interval
        self._last_write = None
        self._pending = None
        self.recent = collections.deque(maxlen=600)   # the last rows, for the live graph
        self._stats_path = None
        self._stats_off = 0
        self._log_path = None
        self._log_off = 0
        self._index = 0
        self._tot = {"lost": 0, "retrans": 0, "dropped": 0}
        self._csv = None
        self._writer = None
        self._csv_path = os.path.join(run_dir, NUMBERS)

    def _ensure_csv(self):
        if self._csv is None:
            exists = os.path.exists(self._csv_path)
            self._csv = open(self._csv_path, "a", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._csv, fieldnames=["time"] + self.FIELDS,
                                          extrasaction="ignore")
            if not exists:
                self._writer.writeheader()

    def poll(self, stats_path, log_path, now=None):
        if stats_path and stats_path != self._stats_path:
            self._stats_path, self._stats_off = stats_path, 0
            self._index += 1
        if log_path and log_path != self._log_path:
            self._log_path, self._log_off = log_path, 0
        rows = self._read_new_lines()
        self._copy_log()
        moment = now or datetime.datetime.now()
        if rows:
            # Totals add up every report; rows are written at most once per min_interval.
            self._pending = dict(self._row(rows[-1]), time=moment.isoformat(timespec="seconds"))
            if self._last_write is None or (moment - self._last_write).total_seconds() >= self.min_interval:
                self._write_pending(moment)
        return len(rows)

    def _write_pending(self, moment):
        if self._pending is None:
            return
        self._ensure_csv()
        self._writer.writerow(self._pending)
        self._csv.flush()
        self.recent.append(dict(self._pending, t=self._pending["time"][11:19]))
        self._last_write = moment
        self._pending = None

    def live(self):
        return list(self.recent)

    def _read_new_lines(self):
        out = []
        if not self._stats_path or not os.path.exists(self._stats_path):
            return out
        try:
            with open(self._stats_path, "rb") as f:
                f.seek(self._stats_off)
                chunk = f.read()
        except OSError:
            return out
        end = chunk.rfind(b"\n")
        if end < 0:
            return out
        self._stats_off += end + 1
        for raw in chunk[:end].split(b"\n"):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                one = json.loads(line)
            except ValueError:
                continue
            snd = one.get("send", {})
            self._tot["lost"] += snd.get("packetsLost", 0)
            self._tot["retrans"] += snd.get("packetsRetransmitted", 0)
            self._tot["dropped"] += snd.get("packetsDropped", 0)
            out.append(one)
        return out

    def _row(self, one):
        link, recv, snd = one.get("link", {}), one.get("recv", {}), one.get("send", {})
        return {"rtt_ms": round(link.get("rtt", 0), 1),
                "rate_mbps": round(snd.get("mbitRate", 0), 2),
                "latency_ms": int(recv.get("msTsbPdDelay", 0) or 0),
                "lost": self._tot["lost"], "retrans": self._tot["retrans"],
                "dropped": self._tot["dropped"]}

    def _copy_log(self):
        if not self._log_path or not os.path.exists(self._log_path):
            return
        try:
            with open(self._log_path, "rb") as f:
                f.seek(self._log_off)
                data = f.read()
        except OSError:
            return
        if not data:
            return
        self._log_off += len(data)
        logs = os.path.join(self.run_dir, "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "transmit-%d.log" % max(1, self._index)), "ab") as f:
            f.write(data)

    def close(self):
        self._write_pending(self._last_write or datetime.datetime.now())
        if self._csv is not None:
            self._csv.close()
        self._csv = None
        self._writer = None


def recover_interrupted(root=DEFAULT_ROOT, active_dirs=()):
    """Mark runs left 'running' by a crash as 'ended early'.

    Pass the run folders that are live right now so they are left alone.
    Returns the number of runs recovered.
    """
    active = {os.path.realpath(p) for p in active_dirs}
    recovered = 0
    for run_dir in _iter_run_dirs(root):
        if os.path.realpath(run_dir) in active:
            continue
        path = os.path.join(run_dir, SUMMARY)
        try:
            data = _read_json(path)
        except (OSError, ValueError):
            continue
        if data.get("status") == "running":
            data["status"] = "ended early"
            data["ended"] = data.get("ended") or _last_sample_time(run_dir)
            _write_json_atomic(path, data)
            recovered += 1
    return recovered


def _last_sample_time(run_dir):
    last = None
    try:
        with open(os.path.join(run_dir, NUMBERS), encoding="utf-8") as f:
            for row in csv.DictReader(f):
                last = row.get("time") or last
    except OSError:
        pass
    return last


# ---------------------------------------------------------------- reading

def _iter_run_dirs(root):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        if SUMMARY in filenames:
            dirnames[:] = []  # a run folder holds no nested runs
            yield dirpath


def list_runs(root=DEFAULT_ROOT, date_from=None, date_to=None, kind=None,
              text=None, host=None, result=None, min_latency_ms=None,
              has_drops=None):
    """Return summaries of saved runs, newest first, filtered.

    date_from / date_to are 'YYYY-MM-DD' strings, inclusive.
    text matches the label, host, or result, case-insensitive.
    """
    rows = []
    for run_dir in _iter_run_dirs(root):
        try:
            s = _read_json(os.path.join(run_dir, SUMMARY))
        except (OSError, ValueError):
            continue
        s = dict(s)
        s["path"] = run_dir
        s["host"] = (s.get("settings") or {}).get("host", "")
        if not _matches(s, date_from, date_to, kind, text, host, result,
                        min_latency_ms, has_drops):
            continue
        rows.append(s)
    rows.sort(key=lambda s: s.get("started") or "", reverse=True)
    return rows


def _matches(s, date_from, date_to, kind, text, host, result,
             min_latency_ms, has_drops):
    day = (s.get("started") or "")[:10]
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    if kind and s.get("kind") != kind:
        return False
    if host and host.lower() not in str(s.get("host", "")).lower():
        return False
    if result and result.lower() not in str(s.get("result") or "").lower():
        return False
    totals = s.get("totals") or {}
    if min_latency_ms is not None:
        worst = totals.get("worst_rtt_ms")
        if worst is None or worst < min_latency_ms:
            return False
    if has_drops and not totals.get("dropped"):
        return False
    if text:
        needle = text.lower()
        hay = " ".join(str(s.get(k) or "") for k in ("label", "host", "result"))
        if needle not in hay.lower():
            return False
    return True


# ---------------------------------------------------------------- limits

def limit_status(count):
    """State of the soft limit for a given number of saved runs."""
    if count >= SOFT_LIMIT:
        return {"state": "limit", "count": count, "limit": SOFT_LIMIT,
                "message": "The history has reached %d runs. Keep saving, "
                           "or move, export or delete some first." % SOFT_LIMIT}
    if count >= WARN_AT:
        return {"state": "warning", "count": count, "limit": SOFT_LIMIT,
                "message": "The history has %d of %d runs." % (count, SOFT_LIMIT)}
    return {"state": "ok", "count": count, "limit": SOFT_LIMIT, "message": ""}


def usage(root=DEFAULT_ROOT):
    """Space used by the history, and the free space on its drive."""
    runs = list(_iter_run_dirs(root))
    used = folder_size(root) if os.path.isdir(root) else 0
    probe = root
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    total, _used, free = shutil.disk_usage(probe or os.sep)
    status = limit_status(len(runs))
    return {
        "root": root,
        "runs": len(runs),
        "limit": SOFT_LIMIT,
        "state": status["state"],
        "message": status["message"],
        "bytes_used": used,
        "used_human": human_size(used),
        "drive_free_bytes": free,
        "drive_total_bytes": total,
        "drive_human": "%s free of %s" % (human_size(free), human_size(total)),
        "on_wsl": _is_wsl(),
        "wsl_note": ("These figures are for the WSL virtual disk, not your "
                     "Windows C: drive.") if _is_wsl() else "",
    }


# ---------------------------------------------------------------- actions

def delete_run(run_dir, root=DEFAULT_ROOT):
    """Delete one run folder, then tidy empty date folders. Returns bytes freed."""
    if not _inside(run_dir, root) or not os.path.exists(os.path.join(run_dir, SUMMARY)):
        raise ValueError("not a saved run inside the history: %s" % run_dir)
    freed = folder_size(run_dir)
    shutil.rmtree(run_dir)
    _prune_empty(os.path.dirname(run_dir), root)
    return freed


def _prune_empty(path, root):
    real_root = os.path.realpath(root)
    while os.path.realpath(path) != real_root and _inside(path, root):
        try:
            os.rmdir(path)
        except OSError:
            break
        path = os.path.dirname(path)


def move_run(run_dir, dest_parent, root=DEFAULT_ROOT):
    """Move a run out of the history, for example to another drive."""
    if not _inside(run_dir, root) or not os.path.exists(os.path.join(run_dir, SUMMARY)):
        raise ValueError("not a saved run inside the history: %s" % run_dir)
    os.makedirs(dest_parent, exist_ok=True)
    dest = os.path.join(dest_parent, os.path.basename(run_dir))
    if os.path.exists(dest):
        raise ValueError("destination already exists: %s" % dest)
    shutil.move(run_dir, dest)
    _prune_empty(os.path.dirname(run_dir), root)
    return dest


def export_runs(run_dirs, dest_zip, root=DEFAULT_ROOT):
    """Write one or more runs into a single zip file. Returns the zip path."""
    if not run_dirs:
        raise ValueError("no runs to export")
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for run_dir in run_dirs:
            if not _inside(run_dir, root) or not os.path.exists(os.path.join(run_dir, SUMMARY)):
                raise ValueError("not a saved run inside the history: %s" % run_dir)
            base = os.path.basename(run_dir)
            for dirpath, _dirs, files in os.walk(run_dir):
                for name in files:
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, run_dir)
                    z.write(full, os.path.join(base, rel))
    return dest_zip


def _cli(argv=None):
    import argparse

    p = argparse.ArgumentParser(prog="runhistory",
                                description="Browse saved srtdebug test runs.")
    p.add_argument("--root", default=DEFAULT_ROOT, help="history folder")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="list saved runs, newest first")
    ls.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    ls.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    ls.add_argument("--text")
    ls.add_argument("--host")
    ls.add_argument("--min-latency", type=int, metavar="MS")
    ls.add_argument("--drops", action="store_true", help="only runs with dropped packets")
    sub.add_parser("usage", help="space used and drive free space")
    ex = sub.add_parser("export", help="export runs to one zip")
    ex.add_argument("zip")
    ex.add_argument("run", nargs="+", help="run folder(s) to export")
    dl = sub.add_parser("delete", help="delete one run folder")
    dl.add_argument("run")
    args = p.parse_args(argv)

    if args.cmd == "list":
        rows = list_runs(args.root, date_from=args.date_from, date_to=args.date_to,
                         text=args.text, host=args.host,
                         min_latency_ms=args.min_latency,
                         has_drops=True if args.drops else None)
        for r in rows:
            t = r.get("totals") or {}
            print("%-19s %-10s %-22s worst %s ms  dropped %s  %s" % (
                (r.get("started") or "")[:16].replace("T", " "),
                r.get("status"), (r.get("label") or "")[:22],
                t.get("worst_rtt_ms"), t.get("dropped"), r.get("path")))
        print("%d run(s)" % len(rows))
    elif args.cmd == "usage":
        u = usage(args.root)
        print("Runs:       %d of %d (%s)" % (u["runs"], u["limit"], u["state"]))
        print("Used:       %s" % u["used_human"])
        print("Drive:      %s" % u["drive_human"])
        if u["message"]:
            print(u["message"])
        if u["wsl_note"]:
            print(u["wsl_note"])
    elif args.cmd == "export":
        print("Wrote %s" % export_runs(args.run, args.zip, args.root))
    elif args.cmd == "delete":
        print("Deleted; freed %s" % human_size(delete_run(args.run, args.root)))


if __name__ == "__main__":
    _cli()
