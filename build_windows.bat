@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   礼物点歌机 - Windows 一键打包
echo ==========================================
where python >nul 2>nul || (echo [错误] 未检测到 Python，请装 3.10-3.12 并勾选 Add to PATH: https://www.python.org/downloads/ & pause & exit /b 1)
where node   >nul 2>nul || (echo [错误] 未检测到 Node.js LTS: https://nodejs.org/zh-cn & pause & exit /b 1)
if not exist node_modules npm install --no-audit --no-fund
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (echo [错误] 依赖安装失败 & pause & exit /b 1)
pyinstaller --noconfirm --clean --onefile --name GiftDJ ^
  --add-data "static;static" --add-data "node_modules;node_modules" --add-data "webui;webui" ^
  --hidden-import pygame --hidden-import websocket ^
  --hidden-import static.Live_pb2 --hidden-import static.Response_pb2 ^
  --hidden-import dy_apis.douyin_api --hidden-import builder.auth ^
  gift_dj.py
if errorlevel 1 (echo [错误] 打包失败 & pause & exit /b 1)
copy /y config.json dist\ >nul
xcopy /e /i /y songs dist\songs >nul
xcopy /e /i /y bgm   dist\bgm   >nul
copy /y 使用说明.md dist\ >nul 2>nul
for /f "delims=" %%i in ('where node') do (copy /y "%%i" dist\ >nul & goto :done)
:done
echo.
echo [完成] 可执行文件: dist\GiftDJ.exe （双击后会自动打开浏览器控制台）
echo        整个 dist 文件夹可拷到其他 Windows 电脑直接用（已带 node.exe）
pause
