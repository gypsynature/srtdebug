' srtdebug for Windows: double-click to start it inside WSL (Ubuntu) and open it in your browser. No window is shown.
Option Explicit
Dim sh, rc
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = sh.ExpandEnvironmentStrings("%USERPROFILE%")
rc = sh.Run("wsl -e sh -c ""SRTDEBUG_NO_BROWSER=1 /opt/srtdebug/launcher/srtdebug-launch.sh""", 0, True)
If rc = 3 Then
  ' already open in a window: that window has been brought forward, so no new tab is opened
ElseIf rc <> 0 Then
  MsgBox "srtdebug did not start. Check that WSL (Ubuntu) is installed and srtdebug is installed inside Ubuntu. Its log is in ~/.srtdebug/panel.log inside Ubuntu.", 16, "srtdebug"
Else
  sh.Run "http://localhost:9999/"
End If
