' srtdebug for Windows: double-click to start it inside WSL (Ubuntu) and open it in your browser. No window is shown.
Option Explicit
Dim sh, fso, t, here, distro, dpart, rc
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = sh.ExpandEnvironmentStrings("%USERPROFILE%")
' the WSL distro is written here when srtdebug is installed; empty means the default distro
here = fso.GetParentFolderName(WScript.ScriptFullName)
distro = ""
If fso.FileExists(here & "\distro.txt") Then
  Set t = fso.OpenTextFile(here & "\distro.txt", 1)
  If Not t.AtEndOfStream Then distro = Trim(t.ReadAll)
  t.Close
End If
dpart = ""
If distro <> "" Then dpart = "-d """ & distro & """ "
rc = sh.Run("wsl " & dpart & "-e sh -c ""SRTDEBUG_NO_BROWSER=1 /opt/srtdebug/launcher/srtdebug-launch.sh""", 0, True)
If rc = 3 Then
  ' already open: its window has been brought to the front. Offer to stop it, so nothing looks like it failed to start
  If MsgBox("srtdebug is already open, and its window has been brought to the front." & vbCrLf & vbCrLf & _
            "Stop srtdebug now? This also stops any monitor or feed that is running. Choose No to keep it running.", _
            vbYesNo + vbQuestion, "srtdebug") = vbYes Then
    sh.Run "wsl " & dpart & "-e sh -c ""SRTDEBUG_QUIET=1 /opt/srtdebug/launcher/srtdebug-stop.sh""", 0, True
    MsgBox "srtdebug has been stopped.", vbInformation, "srtdebug"
  End If
ElseIf rc <> 0 Then
  MsgBox "srtdebug did not start. Check that WSL (Ubuntu) is installed and srtdebug is installed inside Ubuntu. Its log is in ~/.srtdebug/panel.log inside Ubuntu.", 16, "srtdebug"
Else
  sh.Run "http://localhost:9999/"
End If
