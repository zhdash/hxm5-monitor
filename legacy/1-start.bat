@echo off
cd /d "%~dp0"
set PYW=pythonw

if exist monitor.pid (
  set /p OLDPID=<monitor.pid
  tasklist /FI "PID eq %OLDPID%" 2>nul | find "%OLDPID%" >nul && (
    echo [hxm5] Monitor already running, PID %OLDPID%
    goto :done
  )
)

start "" "%PYW%" "%~dp0hxm5_monitor.py"
echo [hxm5] Monitor started.
timeout /t 2 >nul

:done
