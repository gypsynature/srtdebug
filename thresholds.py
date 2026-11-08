"""Health thresholds: the defaults, and the admin's saved overrides.

The overrides live in a small JSON file in the user's own config folder, so no sudo is needed:
    $XDG_CONFIG_HOME/srtdebug/thresholds.json   (or ~/.config/srtdebug/thresholds.json)
The monitor, the report and the picture checks read it when they start. A running monitor keeps the
values it started with, so a change applies to the next run and to reports made after the change.

Standard library only.
"""
import json
import os

# key, label, default, lowest allowed, highest allowed, unit, what it does
SPEC = [
    ("loss_warn_pct", "Loss warning", 0.1, 0.0, 100.0, "%",
     "Detected packet loss (including packets SRT recovered) at or above this rate is a warning."),
    ("loss_bad_pct", "Loss failure", 1.0, 0.0, 100.0, "%",
     "Detected packet loss at or above this rate is a failure."),
    ("unrecovered_fail_pct", "Unrecovered loss failure", 0.1, 0.0, 100.0, "%",
     "Packets SRT gave up on (unrecovered) at or above this rate fail the run. Any unrecovered packet warns."),
    ("rtt_warn_ms", "RTT warning", 100, 0, 10000, "ms",
     "Round-trip time at or above this value is a warning."),
    ("rtt_bad_ms", "RTT failure", 200, 0, 10000, "ms",
     "Round-trip time at or above this value is a failure."),
    ("target_warn_ratio", "Bitrate warning", 0.95, 0.0, 1.0, "of target",
     "Average bitrate below this share of the expected bitrate is a warning (needs --target-mbps)."),
    ("target_fail_ratio", "Bitrate failure", 0.80, 0.0, 1.0, "of target",
     "Average bitrate below this share of the expected bitrate is a failure."),
    ("clip_warn_pct", "Clipping warning", 0.5, 0.0, 100.0, "% of pixels",
     "Share of pixels piled at the peak or floor level that raises a picture warning."),
    ("out_of_range_warn_pct", "Out-of-range warning", 1.0, 0.0, 100.0, "% of pixels",
     "Share of pixels outside the legal range that raises a picture warning."),
]
DEFAULTS = {key: default for key, _, default, *_ in SPEC}
_BY_KEY = {row[0]: row for row in SPEC}
# pairs that must stay in order: the warning level must not exceed the failure level
ORDERED = [("loss_warn_pct", "loss_bad_pct"), ("rtt_warn_ms", "rtt_bad_ms"),
           ("target_fail_ratio", "target_warn_ratio")]


def path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "srtdebug", "thresholds.json")


def _clean(raw):
    """Keep only valid numbers inside their allowed range; anything else falls back to the default.
    Returns (values, problems)."""
    values, problems = dict(DEFAULTS), []
    if not isinstance(raw, dict):
        return values, problems
    for key, _, default, lo, hi, unit, _ in SPEC:
        if key not in raw:
            continue
        try:
            v = float(raw[key])
        except (TypeError, ValueError):
            problems.append(f"{key}: not a number")
            continue
        if not (lo <= v <= hi):
            problems.append(f"{key}: must be between {lo:g} and {hi:g} {unit}")
            continue
        values[key] = int(v) if float(default).is_integer() and v.is_integer() else v
    for warn, bad in ORDERED:
        if values[warn] > values[bad] and not (warn in problems or bad in problems):
            problems.append(f"{_BY_KEY[warn][1]} must not be above {_BY_KEY[bad][1]}")
    return values, problems


def load():
    """Active thresholds: the saved overrides where valid, otherwise the defaults."""
    try:
        with open(path(), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return _clean(raw)[0]


def save(raw):
    """Validate and write the overrides. Returns (values, problems); nothing is written if there are
    problems."""
    values, problems = _clean(raw)
    if problems:
        return values, problems
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: values[k] for k in DEFAULTS}, f, indent=2)
    os.replace(tmp, p)
    return values, []


def reset():
    try:
        os.remove(path())
    except FileNotFoundError:
        pass
    return dict(DEFAULTS)


def describe(values=None):
    """Rows for display: (label, value text, default text, what it does)."""
    values = values or load()
    rows = []
    for key, label, default, _, _, unit, why in SPEC:
        v = values[key]
        rows.append((label, f"{v:g} {unit}", f"{default:g} {unit}", why))
    return rows
