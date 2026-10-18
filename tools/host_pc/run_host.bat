@echo off
REM  Start the Y75 host tool: prefer the packaged exe, fall back to source.
setlocal
cd /d "%~dp0"
if exist dist\Y75_Host.exe (
  start "" dist\Y75_Host.exe
) else (
  python y75_app.py
)
endlocal
