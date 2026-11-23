@echo off
rem Build dist\LOLStats.exe (single file, no Python needed on the target PC)
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed ^
  --name LOLStats --icon icon.ico --add-data "web;web" app.py
echo.
echo Done: dist\LOLStats.exe
