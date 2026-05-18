@echo off
chcp 65001 > nul
echo ============================================
echo   DB Compare and Sync Tool
echo ============================================
echo.
echo กำลังติดตั้ง dependencies...
pip install -r requirements.txt --quiet
echo.
echo กำลังเริ่มต้น server...
echo เปิด browser ที่: http://localhost:8000
echo กด Ctrl+C เพื่อหยุด server
echo.
python app.py
pause
