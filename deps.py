"""External programs srtdebug uses: what each one is for, whether it is required, and how to
install it on each platform. Standard library only."""
import shutil

DEPENDENCIES = [
    {
        "key": "srt",
        "name": "srt-live-transmit",
        "commands": ["srt-live-transmit"],
        "match": "all",
        "required": True,
        "needed_for": "receiving the SRT stream (every live command)",
        "apt": [["apt-get", "update"], ["apt-get", "install", "-y", "srt-tools"]],
        "brew": [["brew", "install", "srt"]],
        "manual": "https://github.com/Haivision/srt/blob/master/docs/apps/srt-live-transmit.md",
    },
    {
        "key": "ffmpeg",
        "name": "ffmpeg and ffprobe",
        "commands": ["ffmpeg", "ffprobe"],
        "match": "all",
        "required": False,
        "needed_for": "codec, chroma and colour details and picture checks (--payload), "
                      "and the live video in the dashboard (--view)",
        "apt": [["apt-get", "update"], ["apt-get", "install", "-y", "ffmpeg"]],
        "brew": [["brew", "install", "ffmpeg"]],
        "manual": "https://ffmpeg.org/download.html",
    },
    {
        "key": "chrome",
        "name": "Chrome or Chromium",
        "commands": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
        "match": "any",
        "required": False,
        "needed_for": "PDF reports and the PDF export",
        "apt": None,   # Ubuntu's chromium-browser is a snap wrapper; install it by hand
        "brew": [["brew", "install", "--cask", "google-chrome"]],
        "manual": "https://www.google.com/chrome/ (Google Chrome .deb works on Ubuntu and WSL)",
    },
]

BY_KEY = {d["key"]: d for d in DEPENDENCIES}


def installed(dep):
    """(found, missing_commands). 'all' needs every command; 'any' needs one of them."""
    found = [c for c in dep["commands"] if shutil.which(c)]
    if dep["match"] == "any":
        return bool(found), ([] if found else dep["commands"])
    missing = [c for c in dep["commands"] if not shutil.which(c)]
    return not missing, missing


def install_steps(dep, platform):
    """Commands to run for this platform, or None when the program has to be installed by hand.
    apt steps are run with sudo; brew steps run as the user."""
    if platform in ("linux", "wsl"):
        return dep["apt"] and [list(c) for c in dep["apt"]], "apt"
    if platform == "macos":
        return dep["brew"] and [list(c) for c in dep["brew"]], "brew"
    return None, None


def needed_keys(a):
    """Which optional programs a command needs, from its options."""
    cmd = getattr(a, "cmd", None) or "monitor"
    keys = []
    if cmd == "report":
        out = getattr(a, "out", None) or ""
        if out.lower().endswith(".pdf"):
            keys.append("chrome")
        return keys
    if cmd == "serve":
        return ["ffmpeg"]           # the control panel can turn on payload analysis and video
    if getattr(a, "payload", False) or getattr(a, "view", False):
        keys.append("ffmpeg")
    return keys
