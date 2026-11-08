#!/bin/sh
# Starts the srtdebug control panel in the background and opens it in the browser.
# Runs from "Launch srtdebug.command" (macOS), or from the app-menu entry (Ubuntu).
# The panel keeps running after this script ends, until "Stop srtdebug" is used.

HERE="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/.srtdebug"
PORT="${SRTDEBUG_PORT:-9999}"
URL="http://127.0.0.1:$PORT/"
PIDFILE="$STATE/panel.pid"
LOG="$STATE/panel.log"

# Finder and the desktop start with a short PATH, so add the usual places for python3, ffmpeg and srt tools
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PATH

# Shows a message in a window, since there may be no terminal to print to
tell_user() {
  if [ "$(uname)" = "Darwin" ]; then
    osascript -e "display dialog \"$1\" with title \"srtdebug\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1
  elif command -v zenity >/dev/null 2>&1; then
    zenity --info --title="srtdebug" --text="$1" 2>/dev/null
  else
    echo "$1"
  fi
}

open_browser() {
  # the Windows launcher opens the browser itself, from Windows
  [ -n "$SRTDEBUG_NO_BROWSER" ] && return
  if [ "$(uname)" = "Darwin" ]; then
    open "$URL"
  else
    xdg-open "$URL" >/dev/null 2>&1 &
  fi
}

mkdir -p "$STATE"

# Already running: if a window is open, the panel brings it forward and no new tab is opened (exit 3)
if curl -s -o /dev/null --max-time 2 "$URL"; then
  if curl -s -X POST --max-time 2 "$URL/window/focus" | grep -q '"open": true'; then
    exit 3
  fi
  open_browser
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  tell_user "Python 3 was not found. Install it, then launch srtdebug again."
  exit 1
fi

cd "$HERE" || exit 1
nohup python3 "$HERE/srtdebug.py" serve --port "$PORT" >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

# Wait for the panel to answer (it can take a second or two to start)
i=0
while [ $i -lt 30 ]; do
  if curl -s -o /dev/null --max-time 1 "$URL"; then
    open_browser
    exit 0
  fi
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    break
  fi
  sleep 0.5
  i=$((i + 1))
done

rm -f "$PIDFILE"
tell_user "The control panel did not start. The log is in $LOG. Port $PORT may already be in use; set SRTDEBUG_PORT to another number to use a different one."
exit 1
