@echo off
cd /d "%~dp0"
if not exist monitor.pid (
  echo [hxm5] No pid file, nothing to stop.
  goto :done
)
set /p PID=<monitor.pid
taskkill /F /PID %PID% >nul 2>&1 && (
  echo [hxm5] Monitor stopped, PID %PID%
) || (
  echo [hxm5] Process %PID% not found, cleaning up.
)
del monitor.pid >nul 2>&1

:done
timeout /t 2 >nul
