# wxsp Windows 打包(M11)。Nuitka standalone → Inno Setup → setup.exe
$ErrorActionPreference = "Stop"

$Version = if ($env:WXSP_VERSION) { $env:WXSP_VERSION } else { "0.1.0" }
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

Write-Host "==> 装 Nuitka(若缺)"
uv add --dev nuitka 2>&1 | Out-Null

Write-Host "==> 清理旧产物"
Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue

Write-Host "==> Nuitka 编译"
# --lto=no / --jobs=2 / --show-scons: 同 macOS 脚本,见注释
uv run python -m nuitka `
  --standalone `
  --lto=no `
  --jobs=2 `
  --show-scons `
  --windows-console-mode=disable `
  --include-package=wxsp `
  --include-package-data=wxsp `
  --include-data-dir=wxsp/templates=wxsp/templates `
  --include-data-files=deploy/wxsp.plist.tmpl=deploy/wxsp.plist.tmpl `
  --include-data-files=deploy/wxsp-task.xml.tmpl=deploy/wxsp-task.xml.tmpl `
  --output-dir=dist `
  --assume-yes-for-downloads `
  --remove-output `
  wxsp/__main__.py

# Nuitka 默认产物名 __main__.dist
$DistDir = "dist/__main__.dist"
if (-not (Test-Path $DistDir)) {
  throw "Nuitka 未生成 $DistDir"
}
Rename-Item $DistDir "wxsp.dist"
$DistDir = "dist/wxsp.dist"
Rename-Item "$DistDir/__main__.exe" "wxsp.exe"

Write-Host "==> 内嵌 patchright chromium"
$ChromiumSrc = uv run python -c "
import patchright, os
d = os.path.join(os.path.dirname(patchright.__file__), 'driver')
c = [x for x in os.listdir(d) if x.startswith('chromium')]
assert c, 'no chromium'
print(os.path.join(d, c[0]))
"
Write-Host "    chromium 源: $ChromiumSrc"
$ChromiumDst = "$DistDir/chromium"
New-Item -ItemType Directory -Force -Path $ChromiumDst | Out-Null
Copy-Item -Recurse -Force "$ChromiumSrc/*" $ChromiumDst

Write-Host "==> 运行 Inno Setup"
$InnoPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $InnoPath)) {
  throw "未找到 Inno Setup,装它: choco install innosetup -y"
}
& $InnoPath /Qp "/DAppVersion=$Version" "/DSourceDir=$ProjectRoot\dist\wxsp.dist" scripts/setup.iss

Write-Host "==> 完成"
Get-ChildItem "dist/*setup*.exe" | Format-Table Name, Length
