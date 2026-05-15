# wxsp Windows 打包(M11)。PyInstaller → onedir bundle → Inno Setup → setup.exe
# 同 macOS 设计:纯 PyInstaller,字节码保护对付运营/一般技术人员够用。
$ErrorActionPreference = "Stop"

$Version = if ($env:WXSP_VERSION) { $env:WXSP_VERSION } else { "0.1.0" }
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

Write-Host "==> 装 PyInstaller(若缺)"
uv add --dev pyinstaller 2>&1 | Out-Null

Write-Host "==> 清理旧产物"
Remove-Item -Recurse -Force dist, build, wxsp.spec -ErrorAction SilentlyContinue

Write-Host "==> 写 launcher"
New-Item -ItemType Directory -Force -Path build | Out-Null
@'
"""wxsp PyInstaller 入口。"""
import sys

from wxsp.cli import app

if __name__ == "__main__":
    # 双击 .exe 等同于无参调用;默认起 Web UI。
    if len(sys.argv) == 1:
        sys.argv.append("web")
    app()
'@ | Out-File -Encoding utf8 build/launcher.py

try {
  Write-Host "==> 注入 APC 凭据"
  foreach ($k in @("APC_ENDPOINT","APC_APP_ID","APC_APP_SECRET","APC_PUBLIC_KEY","APC_CERT_FP")) {
    if (-not (Test-Path env:$k)) {
      throw "$k env var 必填(GitHub Actions secrets;无自签证书时 APC_CERT_FP 设为空字符串)"
    }
  }

  uv run python -c @"
import os, pathlib
p = pathlib.Path('wxsp/apc_config.py')
content = p.read_text()
for key in ('APC_ENDPOINT','APC_APP_ID','APC_APP_SECRET','APC_PUBLIC_KEY','APC_CERT_FP'):
    val = os.environ[key]
    content = content.replace(f'\"__{key}__\"', repr(val))
p.write_text(content)
print('==> 凭据已注入')
"@

  Write-Host "==> PyInstaller 打包"
  uv run pyinstaller `
    --onedir `
    --console `
    --name wxsp `
    --collect-all wxsp `
    --collect-all apc_sdk `
    --collect-all jinja2 `
    --collect-all fastapi `
    --collect-all uvicorn `
    --collect-all lark_oapi `
    --collect-all patchright `
    --noconfirm `
    build/launcher.py

  $DistDir = "dist/wxsp"
  if (-not (Test-Path $DistDir)) {
    throw "PyInstaller 未生成 $DistDir"
  }

  Write-Host "==> 内嵌 patchright chromium 到 _internal/chromium/"
  # patchright 跑 launch 时按 <PLAYWRIGHT_BROWSERS_PATH>/chromium-<版本>/... 查找,
  # 所以保留 chromium-<版本> 这一层目录。
  $ChromiumSrc = uv run python -c "
import os, glob
root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.expandvars(r'%LOCALAPPDATA%\ms-playwright')
c = sorted(glob.glob(os.path.join(root, 'chromium-*')))
assert c, f'no chromium-* in {root}'
print(c[-1])
"
  Write-Host "    chromium 源: $ChromiumSrc"
  $ChromiumDst = "$DistDir/_internal/chromium"
  New-Item -ItemType Directory -Force -Path $ChromiumDst | Out-Null
  # 注意:Copy-Item 用 $src 整个目录(不加 \*)让 chromium-XXXX 名字保留
  Copy-Item -Recurse -Force $ChromiumSrc $ChromiumDst

  Write-Host "==> 运行 Inno Setup"
  $InnoPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
  if (-not (Test-Path $InnoPath)) {
    throw "未找到 Inno Setup,装它: choco install innosetup -y"
  }
  & $InnoPath /Qp "/DAppVersion=$Version" "/DSourceDir=$ProjectRoot\dist\wxsp" scripts/setup.iss

  Write-Host "==> 完成"
  Get-ChildItem "dist/*setup*.exe" | Format-Table Name, Length
} finally {
  Write-Host "==> 恢复 apc_config.py 占位符"
  git checkout -- wxsp/apc_config.py 2>$null | Out-Null
}
