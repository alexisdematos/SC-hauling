@echo off
rem Starts the hauling overlay without a console window.
cd /d "%~dp0"
start "" pythonw -m schauling %*
