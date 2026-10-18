@echo off
REM  Start the device simulator on 127.0.0.1:15020 (for testing without hardware)
setlocal
cd /d "%~dp0"
python y75_simulator.py --port 15020
pause
endlocal
