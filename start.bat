@echo off
rem train-12306 launcher - pure ASCII; python-path.txt (gitignored) can override the interpreter
cd /d %~dp0
set PY=python
if exist "%~dp0python-path.txt" set /p PY=<"%~dp0python-path.txt"
"%PY%" -c "import requests" 2>nul || "%PY%" -m pip install requests
start "" http://127.0.0.1:8831
"%PY%" server.py
