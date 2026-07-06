#!/bin/bash
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "[错误] 请先装 Python3"; read -p "回车退出"; exit 1; }
command -v node    >/dev/null || { echo "[错误] 请先装 Node.js"; read -p "回车退出"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python gift_dj.py
read -p "回车退出"
