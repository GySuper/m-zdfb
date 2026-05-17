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
import multiprocessing
import sys

if __name__ == "__main__":
    # frozen + multiprocessing 在 Windows 上必须;mac 上无害(POSIX fork 默认走)。
    # loguru / playwright 内部可能用 multiprocessing,没这行会 silent 失败。
    multiprocessing.freeze_support()
    from wxsp.cli import app
    # 双击 .app 时 sys.argv 只有可执行路径无子命令;默认起 Web UI(运营要的体验)。
    if len(sys.argv) == 1:
        sys.argv.append("web")
    app()
EOF

echo "==> 注入 APC 凭据"
: "${APC_ENDPOINT:?APC_ENDPOINT env var 必填(GitHub Actions secrets)}"
: "${APC_APP_ID:?APC_APP_ID env var 必填}"
: "${APC_APP_SECRET:?APC_APP_SECRET env var 必填}"
: "${APC_PUBLIC_KEY:?APC_PUBLIC_KEY env var 必填}"
${APC_CERT_FP+:} false || { echo "APC_CERT_FP env var 必填(无自签证书时设为空字符串)" >&2; exit 1; }

# 任何退出路径都恢复源码占位符状态(成功 / 失败 / Ctrl-C)
trap 'git checkout -- wxsp/apc_config.py 2>/dev/null || true' EXIT

uv run python - <<'PYEOF'
import os, pathlib
p = pathlib.Path("wxsp/apc_config.py")
# 显式 UTF-8:mac 默认 UTF-8 这里冗余但与 Windows 对齐,防 CI 某天 locale 改了出 cp1252 类问题
content = p.read_text(encoding="utf-8")
changed = []
for key in ("APC_ENDPOINT", "APC_APP_ID", "APC_APP_SECRET", "APC_PUBLIC_KEY", "APC_CERT_FP"):
    val = os.environ[key]
    pattern = f'"__{key}__"'
    if pattern not in content:
        raise SystemExit(f"FATAL: apc_config.py 找不到占位符 {pattern}")
    # 用 repr 把字符串转成合法 Python 字面量,处理特殊字符 + 换行(PUBLIC_KEY 多行 PEM)
    content = content.replace(pattern, repr(val))
    changed.append(key)
p.write_text(content, encoding="utf-8")
# 校验真改了(防止 silent skip):再读回来确认占位符都不在了
verify = p.read_text(encoding="utf-8")
for key in changed:
    if f'"__{key}__"' in verify:
        raise SystemExit(f"FATAL: {key} 占位符 replace 后仍残留")
print(f"==> 凭据已注入 + 校验通过:{','.join(changed)}(打包后 trap 会 revert)")
PYEOF

echo "==> PyInstaller 打包"
uv run pyinstaller \
  --onedir \
  --windowed \
  --name wxsp \
  --osx-bundle-identifier com.wxsp.app \
  --collect-all wxsp \
  --collect-all apc_sdk \
  --collect-all jinja2 \
  --collect-all fastapi \
  --collect-all uvicorn \
  --collect-all lark_oapi \
  --collect-all patchright \
  --noconfirm \
  build/launcher.py

RAW_APP_PATH="dist/wxsp.app"
if [ ! -d "$RAW_APP_PATH" ]; then
  echo "PyInstaller 未生成 $RAW_APP_PATH" >&2
  exit 1
fi

# 安装后用户在 Finder/Dock 看到的产品名 = "自动发布平台"。
# 可执行文件保持 wxsp(CFBundleExecutable 必须与 Contents/MacOS/ 里的二进制名一致)。
DISPLAY_NAME="自动发布平台"
APP_PATH="dist/${DISPLAY_NAME}.app"
rm -rf "$APP_PATH"
mv "$RAW_APP_PATH" "$APP_PATH"
PLIST="$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName ${DISPLAY_NAME}" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string ${DISPLAY_NAME}" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName ${DISPLAY_NAME}" "$PLIST"

echo "==> 内嵌 patchright chromium 到 _internal/chromium/"
# patchright 跑 launch 时按 <PLAYWRIGHT_BROWSERS_PATH>/chromium-<版本>/chrome-mac-arm64/...
# 查找,所以打包后我们要保留 chromium-<版本> 这一层目录(不是把它的内容铺平)。
CHROMIUM_SRC="$(uv run python -c "
import os, glob
root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.expanduser('~/Library/Caches/ms-playwright')
candidates = sorted(glob.glob(os.path.join(root, 'chromium-*')))
assert candidates, f'未在 {root} 找到 chromium-* 目录;先跑 uv run patchright install chromium'
print(candidates[-1])
")"
echo "    chromium 源: $CHROMIUM_SRC"
# PyInstaller .app 在 mac 下:bundle/Contents/Frameworks/ = _MEIPASS
# 必须保留 chromium-<版本> 这一层(patchright 按这个路径找浏览器)。
# BSD cp 的 `cp -R src dst/` 行为在 dst 已存在时会铺平内容 ——
# 显式拼出含版本号的 DST,然后用 `src/.` 把内容拷进去。
CHROMIUM_VERSION_DIR="$(basename "$CHROMIUM_SRC")"
CHROMIUM_DST="$APP_PATH/Contents/Frameworks/chromium/$CHROMIUM_VERSION_DIR"
mkdir -p "$CHROMIUM_DST"
cp -R "$CHROMIUM_SRC/." "$CHROMIUM_DST/"

echo "==> 修可执行位(Chrome for Testing / Chromium / chrome 都覆盖)"
find "$CHROMIUM_DST" -name "Google Chrome for Testing" -exec chmod +x {} \; 2>/dev/null || true
find "$CHROMIUM_DST" -name "Chromium" -exec chmod +x {} \; 2>/dev/null || true
find "$CHROMIUM_DST" -name "chrome" -exec chmod +x {} \; 2>/dev/null || true

echo "==> 用 create-dmg 打包"
if ! command -v create-dmg >/dev/null 2>&1; then
  echo "未装 create-dmg。本地: brew install create-dmg;CI: workflow 里已加。" >&2
  exit 1
fi

rm -f "dist/wxsp-${VERSION}.dmg"
create-dmg \
  --volname "${DISPLAY_NAME} 安装器" \
  --window-size 600 400 \
  --app-drop-link 450 200 \
  --icon "${DISPLAY_NAME}.app" 150 200 \
  --hide-extension "${DISPLAY_NAME}.app" \
  "dist/wxsp-${VERSION}.dmg" \
  "$APP_PATH"

echo "==> 完成: dist/wxsp-${VERSION}.dmg"
ls -lh "dist/wxsp-${VERSION}.dmg"
