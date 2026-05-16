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
import multiprocessing
import sys

if __name__ == "__main__":
    # frozen + multiprocessing 在 Windows 上必须;没这行 loguru / playwright 用
    # multiprocessing 的内部 worker 会 silent 失败(日志全卡 queue 不落盘等)。
    multiprocessing.freeze_support()
    from wxsp.cli import app
    # 双击 .exe 等同于无参调用;默认起 Web UI。
    if len(sys.argv) == 1:
        sys.argv.append("web")
    app()
'@ | Out-File -Encoding utf8 build/launcher.py

try {
  # 本地打包便利:有 .apc-env.ps1 就自动加载(gitignored)。CI 走环境变量,文件不存在跳过。
  if (Test-Path "$ProjectRoot\.apc-env.ps1") {
    Write-Host "==> 加载 .apc-env.ps1"
    . "$ProjectRoot\.apc-env.ps1"
  }

  Write-Host "==> 注入 APC 凭据"
  foreach ($k in @("APC_ENDPOINT","APC_APP_ID","APC_APP_SECRET","APC_PUBLIC_KEY","APC_CERT_FP")) {
    if (-not (Test-Path env:$k)) {
      throw "$k env var 必填(放 .apc-env.ps1 里,或导入 GitHub Actions secrets;无自签证书时 APC_CERT_FP 设为空字符串)"
    }
  }

  # 把 Python 代码写到独立文件再跑,避免 PowerShell → uv → python 的命令行参数转义陷阱
  # (v0.2.1 用 here-string + python -c 在 GitHub Actions 上 silently 跑过但不改文件)
  @'
import os, pathlib, sys
p = pathlib.Path("wxsp/apc_config.py")
content = p.read_text()
changed = []
for key in ("APC_ENDPOINT","APC_APP_ID","APC_APP_SECRET","APC_PUBLIC_KEY","APC_CERT_FP"):
    val = os.environ.get(key)
    if val is None:
        print(f"FATAL: env var {key} 未设置(GitHub secret 名字对不上?)", file=sys.stderr)
        sys.exit(1)
    pattern = f'"__{key}__"'
    if pattern not in content:
        print(f"FATAL: 在 apc_config.py 里找不到占位符 {pattern}", file=sys.stderr)
        sys.exit(1)
    content = content.replace(pattern, repr(val))
    changed.append(key)
p.write_text(content)
# 校验真改了(防止 silent skip):再读回来,确认占位符都不在了
verify = p.read_text()
for key in changed:
    if f'"__{key}__"' in verify:
        print(f"FATAL: {key} 占位符 replace 后仍残留", file=sys.stderr)
        sys.exit(1)
print("==> 凭据已注入 + 校验通过:", ",".join(changed))
'@ | Out-File -Encoding utf8 build/inject_apc.py
  Write-Host "==> 注入脚本内容预览(前 5 行):"
  Get-Content build/inject_apc.py -TotalCount 5 | ForEach-Object { Write-Host "    | $_" }
  uv run python build/inject_apc.py
  # PowerShell 的 $ErrorActionPreference=Stop 不管原生 .exe 的非零退出码,
  # 必须手动检查 $LASTEXITCODE,否则注入失败也会继续 PyInstaller 出占位符版本。
  if ($LASTEXITCODE -ne 0) {
    throw "APC 凭据注入失败(exit=$LASTEXITCODE),拒绝继续打包"
  }
  Write-Host "==> 注入后 apc_config.py 预览(脱敏前 20 字符):"
  Get-Content wxsp/apc_config.py | ForEach-Object {
    if ($_.Length -gt 25) { Write-Host "    | $($_.Substring(0, 25))..." }
    else { Write-Host "    | $_" }
  }

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
