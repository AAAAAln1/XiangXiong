@echo off
chcp 65001 >nul
echo ========================================
echo Xiangxiong Console - Phase 0 MVP
echo ========================================
echo.
echo Starting server...
echo Visit: http://localhost:8000
echo.
echo Press Ctrl+C to stop
echo.

cd /d "%~dp0console"
python server.py
