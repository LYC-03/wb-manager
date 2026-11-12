@echo off
chcp 65001 >nul
title wb-manager
cd /d "%~dp0"

set "PY="

rem 1) WorkBuddy 自带的 Python（有就用，没有跳过）
for /d %%i in ("%USERPROFILE%\.workbuddy\binaries\python\versions\*") do set "PY=%%i\python.exe"
if not defined PY (
  for /d %%i in ("%LOCALAPPDATA%\Programs\WorkBuddy\resources\binaries\python\versions\*") do set "PY=%%i\python.exe"
)

rem 2) 系统的 python
if not defined PY (
  where /q python
  if not errorlevel 1 set "PY=python"
)

rem 3) Windows 的 py 启动器
if not defined PY (
  where /q py
  if not errorlevel 1 set "PY=py"
)

if not defined PY (
  echo.
  echo   Python not found.
  echo.
  echo   Install Python 3.9 or newer from python.org,
  echo   and tick "Add Python to PATH" during setup.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting wb-manager ...
echo   Close this window or press Ctrl+C to stop.
echo.

"%PY%" wbmanager.py

echo.
echo   Stopped.
pause
