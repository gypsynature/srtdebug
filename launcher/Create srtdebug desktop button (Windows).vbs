' srtdebug for Windows: double-click once to set up srtdebug buttons on your Desktop, and optionally in the Start menu and on the taskbar.
' Keep this file in the same folder as the other .vbs files and srtdebug.ico. No window is shown.
Option Explicit
Dim fso, sh, src, dst, desk, progs, lnk, choice, pinned, manual

Function MakeLink(path, target, arguments, description)
  Dim l
  Set l = sh.CreateShortcut(path)
  l.TargetPath = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\wscript.exe"
  l.Arguments = """" & target & """"
  l.WorkingDirectory = dst
  l.IconLocation = dst & "\srtdebug.ico,0"
  l.Description = description
  l.Save
End Function

' Asks Windows to run its own "Pin to taskbar" or "Pin to Start" command for a shortcut.
' Windows 11 no longer offers these commands to scripts, so this returns False there.
Function PinTo(path, word)
  Dim shell, folder, item, v, n
  PinTo = False
  ' Windows may refuse the pin command, so any error here only means the manual steps are shown
  On Error Resume Next
  Set shell = CreateObject("Shell.Application")
  Set folder = shell.NameSpace(Left(path, InStrRev(path, "\") - 1))
  Set item = folder.ParseName(Mid(path, InStrRev(path, "\") + 1))
  Err.Clear
  If Not item Is Nothing Then
    For Each v In item.Verbs
      n = LCase(Replace(v.Name, "&", ""))
      If InStr(n, "pin") > 0 And InStr(n, "unpin") = 0 And InStr(n, word) > 0 Then
        Err.Clear
        v.DoIt
        PinTo = (Err.Number = 0)
        Err.Clear
        Exit Function
      End If
    Next
  End If
  Err.Clear
End Function

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
src = fso.GetParentFolderName(WScript.ScriptFullName)
dst = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\srtdebug"
If Not fso.FolderExists(dst) Then fso.CreateFolder dst
' Windows cannot show an icon from a network path, so the files are copied to a local folder first
fso.CopyFile src & "\srtdebug.ico", dst & "\srtdebug.ico", True
fso.CopyFile src & "\Launch srtdebug (Windows).vbs", dst & "\Launch srtdebug (Windows).vbs", True
fso.CopyFile src & "\Stop srtdebug (Windows).vbs", dst & "\Stop srtdebug (Windows).vbs", True

desk = sh.SpecialFolders("Desktop")
MakeLink desk & "\srtdebug.lnk", dst & "\Launch srtdebug (Windows).vbs", "", "Open srtdebug"
MakeLink desk & "\Stop srtdebug.lnk", dst & "\Stop srtdebug (Windows).vbs", "", "Stop srtdebug"

progs = sh.SpecialFolders("Programs")
choice = MsgBox("Add srtdebug to the Start menu (All apps and search)?", vbYesNo + vbQuestion, "srtdebug")
If choice = vbYes Then
  MakeLink progs & "\srtdebug.lnk", dst & "\Launch srtdebug (Windows).vbs", "", "Open srtdebug"
  MakeLink progs & "\Stop srtdebug.lnk", dst & "\Stop srtdebug (Windows).vbs", "", "Stop srtdebug"
End If

manual = ""
choice = MsgBox("Pin srtdebug to the Start menu?", vbYesNo + vbQuestion, "srtdebug")
If choice = vbYes Then
  If Not PinTo(desk & "\srtdebug.lnk", "start") Then manual = manual & "Start menu: right-click the srtdebug button on your Desktop and choose Pin to Start (on Windows 11, choose Show more options first)." & vbCrLf
End If
choice = MsgBox("Pin srtdebug to the taskbar?", vbYesNo + vbQuestion, "srtdebug")
If choice = vbYes Then
  If Not PinTo(desk & "\srtdebug.lnk", "taskbar") Then manual = manual & "Taskbar: right-click the srtdebug button on your Desktop and choose Pin to taskbar (on Windows 11, choose Show more options first)." & vbCrLf
End If

If manual = "" Then
  MsgBox "srtdebug is set up as you chose.", vbInformation, "srtdebug"
Else
  MsgBox "srtdebug is set up on your Desktop. Windows would not pin it for setup, so please do this once:" & vbCrLf & vbCrLf & manual, vbInformation, "srtdebug"
End If
