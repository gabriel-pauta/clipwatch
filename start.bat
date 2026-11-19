@echo off
REM Clipwatch: runs app\start.bat (backend + console at http://localhost:8000/console)
call "%~dp0app\start.bat" %*
