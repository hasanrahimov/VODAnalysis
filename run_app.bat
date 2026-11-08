@echo off
REM Double-click to launch the R6M VOD Analyzer GUI.
cd /d "%~dp0"
python src\app.py
if errorlevel 1 pause
