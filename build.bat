@echo off
rem Build dist\LOLStats.exe (single file, no Python needed on the target PC)
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
tasklist /fi "imagename eq LOLStats.exe" | find /i "LOLStats.exe" >nul
if not errorlevel 1 (
  echo BUILD FAILED: LOLStats.exe is still running. Close it first.
  exit /b 1
)
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed ^
  --name LOLStats --icon icon.ico --add-data "web;web" app.py
if errorlevel 1 (
  echo BUILD FAILED: see the PyInstaller output above.
  exit /b 1
)
echo.
echo Done: dist\LOLStats.exe
