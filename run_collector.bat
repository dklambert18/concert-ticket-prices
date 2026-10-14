@echo off
rem Runs the collector and appends output to collector.log.
rem Point Windows Task Scheduler at this file to run it daily.
rem If the run fails, a Windows notification pops up.
cd /d "%~dp0"
"C:\Python314\python.exe" collector.py >> collector.log 2>&1
if errorlevel 1 powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0notify_failure.ps1"
