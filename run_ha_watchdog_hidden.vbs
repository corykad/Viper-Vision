Option Explicit

Dim shell, scriptPath, command
Set shell = CreateObject("WScript.Shell")
scriptPath = "C:\viper_publish_work\watch_home_assistant_vm.ps1"
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & scriptPath & Chr(34) & " -Once"

shell.Run command, 0, True
