@echo off
setlocal
set "HERE=%~dp0"

REM All English on purpose: cmd reads .bat as GBK, Chinese here turns into garbage.
REM The exe pops its own dialog with the result, so no echo/pause needed.

if not exist "%HERE%wb-manager.exe" (
  echo.
  echo   wb-manager.exe not found in this folder.
  echo   Keep this file next to wb-manager.exe.
  echo.
  pause
  exit /b 1
)

"%HERE%wb-manager.exe" --install-shortcut
