#!/bin/sh
# Ubuntu: adds "srtdebug" and "Stop srtdebug" to the application menu, pointing at this copy of the tool.
# Run it once from the srtdebug folder: sh launcher/install-launcher.sh
# (macOS does not need it: double-click the .command files in the launcher folder.)

HERE="$(cd "$(dirname "$0")/.." && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"

write_entry() {   # name, comment, script, file
  cat >"$APPS/$4" <<EOF
[Desktop Entry]
Type=Application
Name=$1
Comment=$2
Exec=sh "$HERE/launcher/$3"
Terminal=false
Categories=Network;Utility;
EOF
  chmod +x "$APPS/$4"
}

write_entry "srtdebug" "Open the SRT debug control panel in your browser" "srtdebug-launch.sh" "srtdebug.desktop"
write_entry "Stop srtdebug" "Stop the SRT debug control panel" "srtdebug-stop.sh" "srtdebug-stop.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1
echo "Added srtdebug and Stop srtdebug to the application menu."
