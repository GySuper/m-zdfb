#!/usr/bin/env bash
# wxsp macOS 打包脚本(M11)。PyInstaller standalone → .app bundle → .dmg。
# CI 用 macos-latest 跑;本地编译需要 brew install create-dmg。
#
# 设计说明:
# 之前试过 Nuitka(60+ min)和 Nuitka-module+PyInstaller(Python ABI 不兼容),
# 都不行。这版改用纯 PyInstaller:把 Python 3.11 解释器 + 全依赖 + wxsp 源码
# (.pyc 字节码)打成 onedir 形式的 .app bundle。
#
# 保护强度:Python 3.11 字节码,公开反编译工具(uncompyle6/decompyle3)对 3.11
# 支持很差,够拦"运营 / 一般技术人员"。
set -euo pipefail

VERSION="${WXSP_VERSION:-0.1.0}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "==> 安装 PyInstaller(若缺)"
uv add --dev pyinstaller || true

echo "==> 清理旧产物"
rm -rf dist build wxsp.spec

echo "==> 写 launcher"
mkdir -p build
cat > build/launcher.py <<'EOF'
"""wxsp PyInstaller 入口。"""
import sys

from wxsp.cli import app

if __name__ == "__main__":
    # 双击 .app 时 sys.argv 只有可执行路径无子命令;默认起 Web UI(运营要的体验)。
    if len(sys.argv) == 1:
        sys.argv.append("web")
    app()
EOF

echo "==> PyInstaller 打包"
uv run pyinstaller \
  --onedir \
  --windowed \
  --name wxsp \
  --osx-bundle-identifier com.wxsp.app \
  --collect-all wxsp \
  --collect-all jinja2 \
  --collect-all fastapi \
  --collect-all uvicorn \
  --collect-all lark_oapi \
  --collect-all patchright \
  --noconfirm \
  build/launcher.py

APP_PATH="dist/wxsp.app"
if [ ! -d "$APP_PATH" ]; then
  echo "PyInstaller 未生成 $APP_PATH" >&2
  exit 1
fi

echo "==> 内嵌 patchright chromium 到 _internal/chromium/"
# patchright/playwright 把 chromium 装在 PLAYWRIGHT_BROWSERS_PATH 指定的目录
# (默认 mac: ~/Library/Caches/ms-playwright,win: %LOCALAPPDATA%\ms-playwright)
CHROMIUM_SRC="$(uv run python -c "
import os, glob
root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.expanduser('~/Library/Caches/ms-playwright')
candidates = sorted(glob.glob(os.path.join(root, 'chromium-*')))
assert candidates, f'未在 {root} 找到 chromium-* 目录;先跑 uv run patchright install chromium'
print(candidates[-1])
")"
echo "    chromium 源: $CHROMIUM_SRC"
# PyInstaller .app 在 mac 下:bundle/Contents/Frameworks/ = _MEIPASS
CHROMIUM_DST="$APP_PATH/Contents/Frameworks/chromium"
mkdir -p "$CHROMIUM_DST"
cp -R "$CHROMIUM_SRC/." "$CHROMIUM_DST/"

echo "==> 修可执行位"
find "$CHROMIUM_DST" -name "Chromium" -exec chmod +x {} \; 2>/dev/null || true
find "$CHROMIUM_DST" -name "chrome" -exec chmod +x {} \; 2>/dev/null || true

echo "==> 用 create-dmg 打包"
if ! command -v create-dmg >/dev/null 2>&1; then
  echo "未装 create-dmg。本地: brew install create-dmg;CI: workflow 里已加。" >&2
  exit 1
fi

rm -f "dist/wxsp-${VERSION}.dmg"
create-dmg \
  --volname "wxsp Installer" \
  --window-size 600 400 \
  --app-drop-link 450 200 \
  --icon "wxsp.app" 150 200 \
  --hide-extension "wxsp.app" \
  "dist/wxsp-${VERSION}.dmg" \
  "$APP_PATH"

echo "==> 完成: dist/wxsp-${VERSION}.dmg"
ls -lh "dist/wxsp-${VERSION}.dmg"
