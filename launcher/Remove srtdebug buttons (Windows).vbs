' srtdebug for Windows: removes the Desktop, Start menu and taskbar buttons and the local copies. Run by the package when it is removed.
Option Explicit
Dim fso, sh, f, dst
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
For Each f In Array(sh.SpecialFolders("Desktop") & "\srtdebug.lnk", sh.SpecialFolders("Desktop") & "\Stop srtdebug.lnk", _
                    sh.SpecialFolders("Programs") & "\srtdebug.lnk", sh.SpecialFolders("Programs") & "\Stop srtdebug.lnk", _
                    sh.ExpandEnvironmentStrings("%APPDATA%") & "\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\srtdebug.lnk")
  On Error Resume Next
  If fso.FileExists(f) Then fso.DeleteFile f, True
  On Error GoTo 0
Next
dst = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\srtdebug"
On Error Resume Next
If fso.FolderExists(dst) Then fso.DeleteFolder dst, True
