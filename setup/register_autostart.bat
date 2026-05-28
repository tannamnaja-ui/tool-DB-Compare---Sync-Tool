@echo off
chcp 65001 > nul
echo ============================================
echo   ลงทะเบียน Auto-Start Server
echo ============================================
echo.

set TASK_NAME=FulfillDBServer
set VBS_PATH=d:\work\งาน IM\Project\fulfill3to4\startup_server.vbs

REM ลบ task เก่าถ้ามี
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

REM สร้าง task ใหม่: รันตอน logon ของ user ปัจจุบัน ไม่มีหน้าต่าง
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "wscript.exe \"%VBS_PATH%\"" ^
  /sc ONLOGON ^
  /delay 0000:15 ^
  /rl HIGHEST ^
  /f

if %errorlevel% equ 0 (
    echo.
    echo [OK] ลงทะเบียนสำเร็จ!
    echo Task: %TASK_NAME%
    echo รันอัตโนมัติตอน login ^(หน่วงเวลา 15 วินาที^)
    echo.
    echo หากต้องการยกเลิก ให้รัน unregister_autostart.bat
) else (
    echo.
    echo [ERROR] ลงทะเบียนไม่สำเร็จ
    echo ลองรันในฐานะ Administrator
)
echo.
pause
