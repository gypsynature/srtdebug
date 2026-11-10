#!/bin/sh
# Stops the srtdebug control panel that "Launch srtdebug" started. A running feed or monitor is stopped with it.

STATE="$HOME/.srtdebug"
PIDFILE="$STATE/panel.pid"

tell_user() {
  [ -n "$SRTDEBUG_QUIET" ] && return   # the caller shows its own message
  if [ "$(uname)" = "Darwin" ]; then
    osascript -e "display dialog \"$1\" with title \"srtdebug\" buttons {\"OK\"} default button \"OK\"" >/dev/null 2>&1
  elif command -v zenity >/dev/null 2>&1; then
    zenity --info --title="srtdebug" --text="$1" 2>/dev/null
  else
    echo "$1"
  fi
}

stopped=0
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")" && stopped=1
fi
rm -f "$PIDFILE"
# also stop any srtdebug process this user owns (the panel, and the monitors or feeds it started)
if command -v pkill >/dev/null 2>&1 && pkill -u "$(id -u)" -f "/opt/srtdebug/srtdebug.py" 2>/dev/null; then
  stopped=1
fi

if [ "$stopped" = 1 ]; then
  tell_user "srtdebug has been stopped."
else
  tell_user "srtdebug is not running."
fi
