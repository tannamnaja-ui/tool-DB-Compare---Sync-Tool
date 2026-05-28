@echo off
chcp 65001 > nul
echo ============================================
echo   ยกเลิก Auto-Start Server
echo ============================================
echo.

set TASK_NAME=FulfillDBServer

schtasks /delete /tn "%TASK_NAME%" /f

if %errorlevel% equ 0 (
    echo [OK] ยกเลิก Auto-Start สำเร็จ
) else (
    echo [INFO] ไม่พบ Task "%TASK_NAME%"
)
echo.
pause
