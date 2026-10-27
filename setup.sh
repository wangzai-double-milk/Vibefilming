#!/usr/bin/env bash
# VibeFilming 一键安装脚本
# 用法：bash setup.sh
set -e

cd "$(dirname "$0")"

echo "==> 1/4 检查 Python 版本"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
    candidate=$(command -v "$c" || true)
    if [ -n "$candidate" ] && "$candidate" -c \
        'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 14)))'
    then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[X] 没找到受支持的 Python（需要 >=3.10,<3.14）。建议安装 Python 3.12："
    echo "    macOS:  brew install python@3.12"
    echo "    Linux:  apt install python3.12 python3.12-venv"
    exit 1
fi
echo "    使用 $PY ($($PY --version))"

echo "==> 2/4 创建虚拟环境 .venv"
# 如果 .venv 存在但没有 pip（如 uv venv 创建的），删掉重建
if [ -d ".venv" ] && { [ ! -x ".venv/bin/pip" ] || [ ! -x ".venv/bin/python" ] \
    || ! .venv/bin/python -c \
        'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 14)))'; }
then
    echo "    检测到 .venv 缺少 pip 或 Python 版本不受支持，重建..."
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
    "bleach>=6.0" \
    "bottle>=0.12" \
    "simple-websocket-server>=0.4" \
    "aiohttp>=3.9" \
    "imageio-ffmpeg>=0.4" \
    "mistune>=3.0,<4" \
    "prompt_toolkit>=3.0" \
    "cozeloop>=0.1.28,<0.2"
echo "    依赖装好"

echo "==> 4/4 检查 vibefilming.config.json"
if [ ! -f "vibefilming.config.json" ]; then
    cp vibefilming.config.example.json vibefilming.config.json
    echo "[!] 已生成 vibefilming.config.json 模板，请编辑填入你的 ARK API key"
    echo "    然后运行：.venv/bin/python agentmain.py"
    exit 0
fi

if grep -q "ark-XXXXXXXX" vibefilming.config.json; then
    echo "[!] vibefilming.config.json 里 ark.api_key 还没填，请先编辑"
    echo "    然后运行：.venv/bin/python agentmain.py"
    exit 0
fi

echo ""
echo "✅ 全部就绪！启动 agent："
echo ""
echo "    .venv/bin/python agentmain.py"
echo ""
echo "（如果 shell 里有 'alias python3=...'，激活 venv 后 python3 仍会指向系统 Python，"
echo "  所以这里直接用 .venv/bin/python）"
echo ""
