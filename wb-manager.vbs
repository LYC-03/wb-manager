' wb-manager launcher - starts the tool with no console window.
' Double-click this file (or the desktop shortcut) to open the app.
Option Explicit

Dim fso, sh, here, script, py, args, cmd, i

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(here, "wbmanager.py")

If Not fso.FileExists(script) Then
  MsgBox "wbmanager.py not found. Keep this launcher in the same folder.", 16, "wb-manager"
  WScript.Quit 1
End If

args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " """ & WScript.Arguments(i) & """"
Next

py = FindExe("pythonw.exe")
If py = "" Then py = FindExe("python.exe")
If py = "" Then
  If HasOnPath("python") Then
    py = "python"
  ElseIf HasOnPath("py") Then
    py = "py"
  Else
    MsgBox "Python 3.9 or newer was not found." & vbCrLf & vbCrLf & _
           "Install it from python.org and tick ""Add Python to PATH""," & vbCrLf & _
           "or run WorkBuddy once so its bundled Python shows up.", 16, "wb-manager"
    WScript.Quit 1
  End If
End If

cmd = """" & py & """ """ & script & """" & args
sh.CurrentDirectory = here
sh.Run cmd, 0, False

WScript.Quit 0

Function FindExe(fileName)
  Dim roots(1), k, fld
  FindExe = ""
  roots(0) = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\versions"
  roots(1) = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\WorkBuddy\resources\binaries\python\versions"
  For k = 0 To 1
    If fso.FolderExists(roots(k)) Then
      For Each fld In fso.GetFolder(roots(k)).SubFolders
        If fso.FileExists(fso.BuildPath(fld.Path, fileName)) Then
          FindExe = fso.BuildPath(fld.Path, fileName)
        End If
      Next
    End If
  Next
End Function

Function HasOnPath(name)
  Dim ex
  HasOnPath = False
  On Error Resume Next
  Set ex = sh.Exec(sh.ExpandEnvironmentStrings("%COMSPEC%") & " /c where " & name)
  If Err.Number <> 0 Then Exit Function
  ex.StdOut.ReadAll
  If ex.ExitCode = 0 Then HasOnPath = True
  On Error GoTo 0
End Function
