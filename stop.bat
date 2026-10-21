@echo off
setlocal EnableExtensions
rem ===========================================================
rem  WorkBuddy proxy - stop (precise)
rem
rem  Usage: stop.bat          stop everything this project runs
rem         stop.bat --dry    only list what would be stopped
rem
rem  Why not "taskkill /f /im python.exe":
rem    * that kills EVERY python on the machine, including
rem      unrelated work;
rem    * and it misses the services this project runs headless
rem      with pythonw.exe, so the watchdog happily restarts
rem      them right away and the stop looks like it failed.
rem
rem  Order matters: the watchdog goes first, otherwise it
rem  resurrects wb_proxy / wb_429_fixer a second later.
rem
rem  ASCII-only on purpose: .bat files are parsed using the
rem  console code page, so non-ASCII text breaks the parser.
rem ===========================================================

if /i "%~1"=="--dry" set "WB_STOP_DRY=1"

echo Stopping WorkBuddy proxy stack ...
powershell -NoProfile -Command "$pats = @('wb_watchdog.py','wb_tray.py','wb_429_fixer.py','wb_proxy.py','wb_updater_web.py'); $dry = ($env:WB_STOP_DRY -eq '1'); $all = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine }); $n = 0; foreach ($pat in $pats) { $hits = @($all | Where-Object { $_.CommandLine -like ('*' + $pat + '*') }); foreach ($h in $hits) { $n++; if ($dry) { Write-Host ('  would stop: PID ' + $h.ProcessId + '  ' + $pat) } else { Write-Host ('  stopping:   PID ' + $h.ProcessId + '  ' + $pat); Stop-Process -Id $h.ProcessId -Force -ErrorAction SilentlyContinue } }; if (-not $dry -and $hits.Count -gt 0) { Start-Sleep -Milliseconds 500 } }; if ($n -eq 0) { Write-Host '  nothing to stop.' }; if ($dry) { Write-Host '' ; Write-Host ('[dry run] ' + $n + ' process(es) matched, none stopped.') }"

if errorlevel 1 (
    echo [WARN] stopping reported an error.
    exit /b 1
)
if defined WB_STOP_DRY exit /b 0

echo.
echo Verifying ports 8787-8789 are free ...
powershell -NoProfile -Command "$busy = @(); foreach ($p in @(8787,8788,8789)) { $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue; if ($c) { $busy += $p } }; if ($busy.Count -gt 0) { Write-Host ('  [WARN] still listening: ' + ($busy -join ', ')) } else { Write-Host '  ports are free.' }"

echo.
echo Done. Start again with start-wb-proxy.bat
timeout /t 2 /nobreak >nul
endlocal
