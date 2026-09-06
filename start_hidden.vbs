Option Explicit

Dim shell, fso, root, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ _
    & root & "\launch_admin.ps1"" web"
shell.Run command, 0, False
