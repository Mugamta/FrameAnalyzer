@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [FrameAnalyzer] 처음 실행: 가상환경을 만들고 필요한 패키지를 설치합니다...
  python -m venv .venv || (echo Python이 필요합니다. https://www.python.org 에서 설치하세요. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)
)
".venv\Scripts\python.exe" server.py %*
pause
