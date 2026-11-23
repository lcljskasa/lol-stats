@echo off
rem Test with the mock client: start mock_lcu.py, then open the app
cd /d "%~dp0"
start "lol-mock" /min python mock_lcu.py
set LCU_PORT=28999
set LCU_TOKEN=test
set LCU_PROTOCOL=http
python app.py
