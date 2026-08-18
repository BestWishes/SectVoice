@echo off
set SECTVOICE_ROOT=%~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0source"
"%~dp0source\.venv\Scripts\python.exe" -m sectvoice
pause
