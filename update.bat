@echo off
chcp 65001 >nul
echo ========================================
echo   WorkBuddy2API-Hub 一键更新脚本
echo ========================================
echo.

cd /d "%~dp0"

echo [1/4] 检查 Git...
git --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 未安装 Git，请先安装 Git
    pause
    exit /b 1
)
echo ✅ Git 已安装
echo.

echo [2/4] 拉取最新代码...
git pull
if errorlevel 1 (
    echo ❌ 拉取失败，请检查网络或手动解决冲突
    pause
    exit /b 1
)
echo ✅ 代码已更新
echo.

echo [3/4] 重启服务...
echo 正在停止旧进程...
taskkill /f /im python.exe 2>nul
timeout /t 2 /nobreak >nul
echo 正在启动新版本...
start "" python wb_proxy.py --port 8788
echo.

echo [4/4] 等待服务启动...
timeout /t 5 /nobreak >nul
echo.

echo ========================================
echo   ✅ 更新完成！
echo   服务地址: http://127.0.0.1:8788/
echo ========================================
echo.
pause
