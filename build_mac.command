#!/bin/bash
cd "$(dirname "$0")"
set -e
echo "=========================================="
echo "  礼物点歌机 - Mac 一键打包"
echo "=========================================="
command -v python3 >/dev/null || { echo "[错误] 未装 Python3: https://www.python.org/downloads/"; read -p "回车退出"; exit 1; }
command -v node    >/dev/null || { echo "[错误] 未装 Node.js LTS: https://nodejs.org/zh-cn"; read -p "回车退出"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
pyinstaller --noconfirm --clean --onefile --name GiftDJ \
  --add-data "static:static" --add-data "node_modules:node_modules" --add-data "webui:webui" \
  --hidden-import pygame gift_dj.py
cp config.json dist/
cp -R songs dist/songs
cp -R bgm   dist/bgm
cp 使用说明.md dist/ 2>/dev/null || true
cp "$(command -v node)" dist/ 2>/dev/null || true
echo
echo "[完成] 可执行文件: dist/GiftDJ （双击后自动打开浏览器控制台）"
echo "       首次被拦截: 右键 GiftDJ -> 打开"
read -p "回车退出"
