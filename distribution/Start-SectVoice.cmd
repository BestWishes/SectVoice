@echo off
setlocal
set "SECTVOICE_ROOT=%~dp0"
set "SECTVOICE_APP_DIR=%~dp0app"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
start "" "%~dp0app\SectVoiceReader.exe"
