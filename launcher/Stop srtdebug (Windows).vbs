' srtdebug for Windows: double-click to stop it. No window is shown.
Option Explicit
Dim sh, fso, t, here, distro, dpart
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = sh.ExpandEnvironmentStrings("%USERPROFILE%")
here = fso.GetParentFolderName(WScript.ScriptFullName)
distro = ""
If fso.FileExists(here & "\distro.txt") Then
  Set t = fso.OpenTextFile(here & "\distro.txt", 1)
  If Not t.AtEndOfStream Then distro = Trim(t.ReadAll)
  t.Close
End If
dpart = ""
If distro <> "" Then dpart = "-d """ & distro & """ "
sh.Run "wsl " & dpart & "-e sh -c ""/opt/srtdebug/launcher/srtdebug-stop.sh""", 0, True
MsgBox "srtdebug is stopped (or was not running).", 64, "srtdebug"
