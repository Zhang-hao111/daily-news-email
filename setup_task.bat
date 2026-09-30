@echo off
chcp 65001 >nul

echo 正在注册定时任务 DailyNewsEmail（每天 09:05）...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_task.ps1"
if %errorlevel%==0 (
    echo.
    echo 完成！任务已指向当前 Python，电池供电下也会运行。
) else (
    echo.
    echo 注册失败：请右键本文件，选择"以管理员身份运行"后重试。
)
pause
