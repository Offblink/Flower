@echo off
chcp 65001 >nul
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app.py"
