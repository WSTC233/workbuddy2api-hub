@echo off
setlocal EnableExtensions
chcp 65001 >nul
echo ========================================
echo   WorkBuddy2API-Hub 一键更新脚本
echo ========================================
echo.

cd /d "%~dp0"

echo [1/4] Checking git ...
git --version >nul 2>&1
if errorlevel 1 (
    echo [x] Git not found. Install git first.
    pause
    exit /b 1
)
echo [ok] git present
echo.

echo [2/4] Pulling latest code ...
rem  --rebase --autostash: the local layer (429 fixer / tray / updater page)
rem  is committed here, upstream never ships those files. A plain pull would
rem  create a merge commit every time; a merge that goes wrong leaves the
rem  tree half-updated, which is worse than a refused fast-forward.
git pull --rebase --autostash
if errorlevel 1 (
    echo [x] pull failed - fix the conflict and run this again.
    pause
    exit /b 1
)
echo [ok] up to date
echo.

echo [3/4] Restarting services ...
rem  Was: taskkill /f /im python.exe  +  start python wb_proxy.py --port 8788.
rem  Two problems with that:
rem    * /im python.exe kills every python on the machine (the watchdog too),
rem      and misses the pythonw.exe helpers, so half the stack survives;
rem    * it puts the real proxy on 8788, where the 429 retry layer belongs.
rem      After one update the retry layer is silently gone.
echo      stop.bat then start-hidden.vbs (watchdog re-launches everything)
call "%~dp0stop.bat"
if errorlevel 1 (
    echo [x] stop failed - see above.
    pause
    exit /b 1
)
ping -n 3 127.0.0.1 >nul
echo      starting ...
wscript "%~dp0start-hidden.vbs"
echo.

echo [4/4] Waiting for 8788 ...
set /a TRIES=0
:wait8788
set /a TRIES+=1
curl.exe -s -m 2 -o nul http://127.0.0.1:8788/v1/models
if not errorlevel 1 goto ready
if %TRIES% geq 45 goto notready
ping -n 2 127.0.0.1 >nul
goto wait8788

:ready
echo [ok] 8788 is answering
echo.
echo ========================================
echo   Updated.
echo   Service:  http://127.0.0.1:8788/
echo   Retry layer self-check: http://127.0.0.1:8788/__fixer/health
echo   Logs: proxy-429.log, watchdog.log, upstream.log
echo ========================================
echo.
pause
exit /b 0

:notready
echo [!] 8788 did not answer within 45s. Check watchdog.log / proxy-429.log.
pause
exit /b 1
