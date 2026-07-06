@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo [错误] 请先装 Python: https://www.python.org/downloads/ & pause & exit /b 1)
where node   >nul 2>nul || (echo [错误] 请先装 Node.js: https://nodejs.org/zh-cn & pause & exit /b 1)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python gift_dj.py
pause
