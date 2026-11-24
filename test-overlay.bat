@echo off
rem Test the in-game overlay with the mock client (mock must run with MOCK_PHASE=InProgress)
cd /d "%~dp0"
set LCU_PORT=28999
set LCU_TOKEN=test
set LCU_PROTOCOL=http
set LIVE_BASE=http://127.0.0.1:28999
set LOLSTATS_DEBUG=1
.venv\Scripts\python app.py
