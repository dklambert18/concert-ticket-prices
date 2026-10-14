@echo off
rem Runs the collector and appends output to collector.log.
rem Point Windows Task Scheduler at this file to run it daily.
cd /d "%~dp0"
"C:\Python314\python.exe" collector.py >> collector.log 2>&1
