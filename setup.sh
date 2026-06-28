#!/usr/bin/env bash
# VibeFilming 一键安装脚本
# 用法：bash setup.sh
set -e

cd "$(dirname "$0")"

echo "==> 1/4 检查 Python 版本"
PY=$(command -v python3.12 || command -v python3.11 || command -v python3 || true)
if [ -z "$PY" ]; then
    echo "[X] 没找到 python3。请先安装 Python 3.11 或 3.12："
    echo "    macOS:  brew install python@3.12"
    echo "    Linux:  apt install python3.12 python3.12-venv"
    exit 1
fi
echo "    使用 $PY ($($PY --version))"

echo "==> 2/4 创建虚拟环境 .venv"
# 如果 .venv 存在但没有 pip（如 uv venv 创建的），删掉重建
if [ -d ".venv" ] && [ ! -x ".venv/bin/pip" ]; then
    echo "    检测到 .venv 不带 pip（可能是 uv venv 建的），重建..."
    rm -rf .venv
fi
if [ ! -d ".venv" ]; then
    $PY -m venv .venv
    echo "    .venv 已创建"
else
    echo "    .venv 已存在，跳过"
fi

echo "==> 3/4 安装依赖"
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install --quiet \
    "requests>=2.28" \
    "beautifulsoup4>=4.12" \
    "bottle>=0.12" \
    "simple-websocket-server>=0.4" \
    "aiohttp>=3.9" \
    "imageio-ffmpeg>=0.4" \
    "prompt_toolkit>=3.0" \
    "cozeloop>=0.1.28,<0.2"
echo "    依赖装好"

echo "==> 4/4 检查 vibefilming.config.json"
if [ ! -f "vibefilming.config.json" ]; then
    cp vibefilming.config.example.json vibefilming.config.json
    echo "[!] 已生成 vibefilming.config.json 模板，请编辑填入你的 ARK API key"
    echo "    然后运行：source .venv/bin/activate && python3 agentmain.py"
    exit 0
fi

if grep -q "ark-XXXXXXXX" vibefilming.config.json; then
    echo "[!] vibefilming.config.json 里 ark.api_key 还没填，请先编辑"
    echo "    然后运行：source .venv/bin/activate && python3 agentmain.py"
    exit 0
fi

echo ""
echo "✅ 全部就绪！启动 agent："
echo ""
echo "    source .venv/bin/activate"
echo "    python3 agentmain.py"
echo ""
