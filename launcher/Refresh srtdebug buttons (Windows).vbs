' srtdebug for Windows: sets up or refreshes the Desktop, Start menu and taskbar buttons.
' The WSL package runs it every time srtdebug is installed or upgraded, with the WSL distro name as argument.
' The first time, it also asks about the Start menu and the taskbar. After that it only refreshes the buttons.
Option Explicit
Dim fso, sh, src, dst, desk, progs, distro, first, wantStart, manual, t, choice

Function MakeLink(path, target, description)
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

' the WSL distro that srtdebug is installed in; empty means the default distro
distro = ""
If WScript.Arguments.Count > 0 Then distro = Trim(WScript.Arguments(0))
Set t = fso.CreateTextFile(dst & "\distro.txt", True)
t.Write distro
t.Close

' copy the files every time, so the buttons always run the installed version
' (Windows cannot show an icon from a network path, so the icon is copied too)
fso.CopyFile src & "\srtdebug.ico", dst & "\srtdebug.ico", True
fso.CopyFile src & "\Launch srtdebug (Windows).vbs", dst & "\Launch srtdebug (Windows).vbs", True
fso.CopyFile src & "\Stop srtdebug (Windows).vbs", dst & "\Stop srtdebug (Windows).vbs", True

desk = sh.SpecialFolders("Desktop")
progs = sh.SpecialFolders("Programs")
first = Not fso.FileExists(dst & "\setup-done.txt")

MakeLink desk & "\srtdebug.lnk", dst & "\Launch srtdebug (Windows).vbs", "Open srtdebug"
MakeLink desk & "\Stop srtdebug.lnk", dst & "\Stop srtdebug (Windows).vbs", "Stop srtdebug"

' Start menu entries: refreshed when they exist; on the first setup, added if the user says yes
wantStart = fso.FileExists(progs & "\srtdebug.lnk")
If first Then
  wantStart = (MsgBox("Add srtdebug to the Start menu (All apps and search)?", vbYesNo + vbQuestion, "srtdebug") = vbYes)
End If
If wantStart Then
  MakeLink progs & "\srtdebug.lnk", dst & "\Launch srtdebug (Windows).vbs", "Open srtdebug"
  MakeLink progs & "\Stop srtdebug.lnk", dst & "\Stop srtdebug (Windows).vbs", "Stop srtdebug"
End If

If first Then
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
  Set t = fso.CreateTextFile(dst & "\setup-done.txt", True)
  t.Write "setup done"
  t.Close
End If
