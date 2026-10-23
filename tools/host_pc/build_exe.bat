@echo off
REM ---------------------------------------------------------------
REM  Build Y75_Host.exe (one-file, windowed) with PyInstaller.
REM  Requirements: a python that HAS tkinter (see README.md).
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

REM ---- pick the interpreter -------------------------------------
set "PY=C:\Users\y3010\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] this python has no tkinter: %PY%
  echo         the built exe would fail with: No module named 'tkinter'
  echo         use the managed venv instead:
  echo         %%USERPROFILE%%\.workbuddy\binaries\python\envs\default\Scripts\python.exe
  exit /b 1
)

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo [INFO] installing pyinstaller ...
  "%PY%" -m pip install pyinstaller
  if errorlevel 1 exit /b 1
)

REM ---- move the old exe away, never delete it --------------------
REM  (some sandboxes / AV block os.remove on .exe; "move" always works)
if exist dist\Y75_Host.exe (
  if not exist dist\old mkdir dist\old
  move /y dist\Y75_Host.exe "dist\old\Y75_Host_%RANDOM%.exe" >nul
)

REM ---- a FRESH workpath every run: avoids touching old temp dirs --
set "WORK=build_y75_%RANDOM%%RANDOM%"

echo [INFO] building with %PY% ...
REM  --icon zkmd.ico      → 写进 exe 的图标资源（资源管理器 / 任务栏）
REM  --add-data zkmd.ico  → 解到 _MEIPASS，运行时给 Tk 设窗口左上角角标用
"%PY%" -m PyInstaller --onefile --windowed --name Y75_Host --icon zkmd.ico ^
  --add-data "zkmd.ico;." ^
  --version-file version_info.py --clean --noconfirm ^
  --workpath "%WORK%" --distpath dist y75_app.py
if errorlevel 1 (
  echo [ERROR] build failed
  exit /b 1
)

echo [INFO] self test ...
dist\Y75_Host.exe --selftest
if errorlevel 1 (
  echo [ERROR] selftest failed - the exe is probably missing tkinter
  exit /b 1
)

for %%A in (dist\Y75_Host.exe) do echo [OK] built dist\Y75_Host.exe  %%~zA bytes
endlocal
