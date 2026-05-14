#!/usr/bin/env bash
# wxsp macOS 打包脚本(M11)。Nuitka standalone → app bundle → .dmg。
# CI 用 macos-latest 跑;本地编译需要 brew install create-dmg。
set -euo pipefail

VERSION="${WXSP_VERSION:-0.1.0}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "==> 安装 nuitka(若缺)"
uv add --dev nuitka || true

echo "==> 清理旧产物"
rm -rf dist build

echo "==> Nuitka 编译"
# --lto=no: 关 LTO,链接快 5-10 倍,代价是产物体积大些(0.1.0 不在乎)
# --jobs=2: 限制并行 clang 进程数,避免 GHA 14GB runner 内存爆掉
# --show-scons: 让 clang 输出可见,卡住时能定位
uv run python -m nuitka \
  --standalone \
  --lto=no \
  --jobs=2 \
  --show-scons \
  --macos-create-app-bundle \
  --macos-app-name=wxsp \
  --macos-app-icon=assets/icon.icns \
  --include-package=wxsp \
  --include-package-data=wxsp \
  --include-data-dir=wxsp/templates=wxsp/templates \
  --include-data-files=deploy/wxsp.plist.tmpl=deploy/wxsp.plist.tmpl \
  --include-data-files=deploy/wxsp-task.xml.tmpl=deploy/wxsp-task.xml.tmpl \
  --output-dir=dist \
  --assume-yes-for-downloads \
  --remove-output \
  wxsp/__main__.py

APP_PATH="dist/__main__.app"
# Nuitka 默认按 __main__ 命名,改成 wxsp.app
mv "$APP_PATH" "dist/wxsp.app"
APP_PATH="dist/wxsp.app"

echo "==> 内嵌 patchright chromium"
CHROMIUM_SRC="$(uv run python -c "
import patchright, os
driver = os.path.join(os.path.dirname(patchright.__file__), 'driver')
candidates = [d for d in os.listdir(driver) if d.startswith('chromium')]
assert candidates, f'未在 {driver} 找到 chromium 目录'
print(os.path.join(driver, candidates[0]))
")"
echo "    chromium 源: $CHROMIUM_SRC"
mkdir -p "$APP_PATH/Contents/Resources/chromium"
cp -R "$CHROMIUM_SRC/." "$APP_PATH/Contents/Resources/chromium/"

echo "==> 修可执行位"
find "$APP_PATH/Contents/Resources/chromium" -name "Chromium" -exec chmod +x {} \; 2>/dev/null || true
find "$APP_PATH/Contents/Resources/chromium" -name "chrome" -exec chmod +x {} \; 2>/dev/null || true

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
