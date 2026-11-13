@echo off
setlocal
set "HERE=%~dp0"
if not exist "%HERE%wb-manager.exe" (
  echo.
  echo   wb-manager.exe not found in this folder.
  echo   Keep this file next to wb-manager.exe.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$r = '%HERE%'.TrimEnd('\'); $e = Join-Path $r 'wb-manager.exe'; $d = [Environment]::GetFolderPath('Desktop'); $p = Join-Path $d 'wb-manager.lnk'; $w = New-Object -ComObject WScript.Shell; $s = $w.CreateShortcut($p); $s.TargetPath = $e; $s.WorkingDirectory = $r; $s.IconLocation = $e + ',0'; $s.Description = 'WorkBuddy session manager'; $s.Save(); Write-Host ''; Write-Host '  Shortcut created:'; Write-Host ('  ' + $p)"

echo.
pause
