@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [FrameAnalyzer] yt-dlp 등 패키지를 최신으로 업데이트합니다...
".venv\Scripts\python.exe" -m pip install -U -r requirements.txt
pause
