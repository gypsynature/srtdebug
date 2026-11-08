' srtdebug for Windows: double-click to stop it. No window is shown.
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = sh.ExpandEnvironmentStrings("%USERPROFILE%")
sh.Run "wsl -e sh -c ""/opt/srtdebug/launcher/srtdebug-stop.sh""", 0, True
MsgBox "srtdebug is stopped (or was not running).", 64, "srtdebug"
