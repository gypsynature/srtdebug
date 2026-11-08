#!/bin/sh
# Stops the srtdebug control panel that "Launch srtdebug" started. A running feed or monitor is stopped with it.

STATE="$HOME/.srtdebug"
PIDFILE="$STATE/panel.pid"

tell_user() {
  if [ "$(uname)" = "Darwin" ]; then
    osascript -e "display dialog \"$1\" with title \"srtdebug\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1
  elif command -v zenity >/dev/null 2>&1; then
    zenity --info --title="srtdebug" --text="$1" 2>/dev/null
  else
    echo "$1"
  fi
}

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")"
  rm -f "$PIDFILE"
  tell_user "srtdebug has been stopped."
else
  rm -f "$PIDFILE"
  tell_user "srtdebug is not running."
fi
