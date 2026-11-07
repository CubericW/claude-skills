#!/usr/bin/env bash
# 安装/修复 handwriting-svg 的运行环境(可重复执行)。
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v potrace >/dev/null 2>&1; then
  brew install potrace
fi
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt

# 可选: bash setup.sh --with-waifu2x  安装 waifu2x-ncnn-vulkan(MIT, GitHub 官方发布版, 含 Apple Silicon), 用于轮廓放大
W2X=tools/waifu2x-ncnn-vulkan-20250915-macos/waifu2x-ncnn-vulkan
if [ "${1:-}" = "--with-waifu2x" ] && [ ! -x "$W2X" ]; then
  mkdir -p tools
  curl -L --fail -o tools/waifu2x-macos.zip \
    https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20250915/waifu2x-ncnn-vulkan-20250915-macos.zip
  echo "a5b58b239eb3aa030db5464f6759637fe412d8c07891a4346ea57f708b514d42  tools/waifu2x-macos.zip" | shasum -a 256 -c -
  unzip -q -o tools/waifu2x-macos.zip -d tools && rm tools/waifu2x-macos.zip
fi
[ -x "$W2X" ] && echo "waifu2x: $W2X" || echo "waifu2x 未安装(轮廓放大将用双三次; 安装: bash setup.sh --with-waifu2x)"
.venv/bin/python -c "import cv2, rapidocr, onnxruntime; print('OpenCV', cv2.__version__, '| RapidOCR OK')"
potrace --version | head -1
