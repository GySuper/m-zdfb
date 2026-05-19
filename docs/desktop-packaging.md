# Python 桌面程序双端打包指南(给 AI 看)

> 目标读者:接到"把 Python 项目打成 mac .dmg + win setup.exe,自动发版,带凭据注入"任务的 AI agent。
> 这是 wxsp 项目踩了 5 个版本才稳定下来的方案,直接照抄 + 按本文档检查即可。
> 不讲为什么选 PyInstaller(选了就是了),只讲怎么落地 + 哪些坑必须避。

---

## 0. 适用范围 + 总览

**适用**:
- 桌面 Python 程序(CLI / Web UI / 自动化工具),Python 3.10+
- 单文件分发到不会装 Python 的运营 / 客户机器
- 需要在打包时注入秘密(license server / API key / 证书)
- 想用 GitHub Actions 自动出 dmg + setup.exe + Release

**不适用**:
- 需要 universal binary(mac arm64 + x86_64 同一个 .app):本方案出哪台机器架构的就是哪个
- 需要做代码混淆 / 真正的反逆向:这套只到 .pyc 字节码,够拦运营 / 一般技术人员
- 需要 GUI 框架(PyQt / Electron):本方案只覆盖 console + Web UI

**技术栈**:

| 层 | 选型 | 备注 |
|---|---|---|
| 打包 | PyInstaller `--onedir --console` | onefile 启动慢 + AV 误报多,onedir 稳 |
| mac 安装包 | `create-dmg`(brew) | 拖图标的标准 dmg |
| win 安装包 | Inno Setup 6(choco) | x64 only,中文界面要单独装 ISL |
| 自动化 | GitHub Actions tag-triggered | `on: push: tags: ['v*']` |
| 凭据注入 | 源码占位符 + build 时 sed/replace + trap revert | **不**走环境变量(打包产物没 env) |

**输出**:
- `dist/wxsp-{ver}.dmg`(~250 MB,含 chromium)
- `dist/wxsp-{ver}-setup.exe`(~200 MB,含 chromium)

---

## 1. 项目骨架 + 关键文件

```
your_project/
├── your_project/                    # 包名 = 命令名,小写
│   ├── __init__.py
│   ├── cli.py                       # typer/click 入口 = pyproject.scripts 入口
│   ├── apc_config.py                # 凭据占位符文件(打包时被替换,源码永远是占位符)
│   └── ...
├── scripts/
│   ├── build_macos.sh               # bash,从 .apc-env source 或 GH secrets
│   ├── build_windows.ps1            # pwsh,从 .apc-env.ps1 或 GH secrets
│   └── setup.iss                    # Inno Setup 配置
├── .github/workflows/
│   └── build.yml                    # mac + win + release 三 job 并行
├── .apc-env                         # 本地 mac 凭据(gitignore!)
├── .apc-env.ps1                     # 本地 win 凭据(gitignore!)
├── .gitignore                       # 必须含 .apc-env*  /dist  /build  *.spec
└── pyproject.toml
```

**`.gitignore` 必须包含**:
```
.apc-env*
/dist
/build
*.spec
```

`*.spec` 是 PyInstaller 第一次跑生成的脚本,**每次 build 都重新生成,不要 commit**(否则会用旧 spec 跳过依赖分析,改了 import 看不到效果)。

---

## 2. launcher.py(PyInstaller 入口,**必须有 freeze_support**)

PyInstaller 不能直接打 `your_project/cli.py`,因为 typer/click 的 `app()` 调用需要顶层 module 调度。写一个独立的 launcher:

```python
"""PyInstaller 入口。"""
import multiprocessing
import sys

if __name__ == "__main__":
    # ★ 必须在所有业务 import 之前 ★
    # frozen + Windows 上 multiprocessing worker 子进程会 re-spawn frozen .exe,
    # 没这行 loguru enqueue / playwright 内部 worker 会 silent 失败,把整个日志/浏览器拖瘫。
    # mac 是 fork 模式,这行无害但写上对齐两端。
    multiprocessing.freeze_support()

    from your_project.cli import app  # 必须在 freeze_support 之后 import
    if len(sys.argv) == 1:
        sys.argv.append("web")  # 双击启动时默认子命令(GUI 体验)
    app()
```

build 脚本里把这段写入 `build/launcher.py`(每次重生成,见 §4 §5)。

---

## 3. PyInstaller 命令模板

```bash
pyinstaller \
  --onedir \
  --console \              # mac 可用 --windowed(.app);win 必须 --console 保留 stderr
  --name your_project \
  --collect-all your_project \
  --collect-all dep1 \     # 每个用 importlib.metadata / lazy import 的依赖都要 collect-all
  --collect-all dep2 \
  --noconfirm \
  build/launcher.py
```

**`--collect-all` 必须列出**:
- 你自己的包(为了把 data files 如 templates/ 一起带上)
- 凡是用 `lark_oapi` / `fastapi` / `uvicorn` / `jinja2` / `playwright`/`patchright` 这种内部有动态 import 的依赖。漏一个跑起来 ImportError,**没法在 dev 测出来**

**`--console` vs `--windowed`**:
- Windows:**永远 --console**。`--windowed` 会把 stderr 接到 NUL,loguru / traceback 全消失,调试地狱
- mac:可以 `--windowed` 出真 .app bundle(双击不弹 Terminal),失去 stderr 但 loguru 文件 sink 仍能 work

---

## 3.1 patchright / playwright chromium 内嵌

如果项目用 patchright/playwright,**必须**把 `chromium-<版本>` 文件夹复制到 PyInstaller 输出里:

```bash
# 关键:保留 chromium-<版本> 这一层目录(playwright 按这个名字查找浏览器)
CHROMIUM_SRC=$(python -c "
import os, glob
root = os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or os.path.expanduser('~/Library/Caches/ms-playwright')
print(sorted(glob.glob(f'{root}/chromium-*'))[-1])
")
CHROMIUM_VERSION_DIR=$(basename "$CHROMIUM_SRC")  # e.g. chromium-1187
DST="dist/your_project.app/Contents/Frameworks/chromium/$CHROMIUM_VERSION_DIR"
mkdir -p "$DST"
cp -R "$CHROMIUM_SRC/." "$DST/"     # 注意 src/. 而不是 src,BSD cp 行为差异
```

Windows 类似但用 PowerShell `Copy-Item -Recurse`,**不加 `\*` 通配**(让目录名保留)。

---

## 4. mac 打包脚本(scripts/build_macos.sh)

```bash
#!/usr/bin/env bash
set -euo pipefail

VERSION="${WXSP_VERSION:-0.1.0}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# 本地便利:有 .apc-env 就 source,CI 走 env vars
[ -f .apc-env ] && source .apc-env

# 校验凭据非空(bash `:?` 同时拒绝 unset 和空字符串)
: "${APC_ENDPOINT:?必填}"
: "${APC_APP_ID:?必填}"
: "${APC_APP_SECRET:?必填}"
: "${APC_PUBLIC_KEY:?必填}"
[ "${APC_CERT_FP+set}" = "set" ] || { echo "APC_CERT_FP 必填(可空)" >&2; exit 1; }

# 注入完无论成功失败都还原源码(成功 / 失败 / Ctrl-C 都触发 EXIT trap)
trap 'git checkout -- your_project/apc_config.py 2>/dev/null || true' EXIT

uv run python - <<'PYEOF'
import os, pathlib
p = pathlib.Path("your_project/apc_config.py")
# ★ 必须显式 encoding="utf-8":CI runner locale 不可控
content = p.read_text(encoding="utf-8")
changed = []
for key in ("APC_ENDPOINT", "APC_APP_ID", "APC_APP_SECRET", "APC_PUBLIC_KEY", "APC_CERT_FP"):
    val = os.environ[key]
    pattern = f'"__{key}__"'
    if pattern not in content:
        raise SystemExit(f"FATAL: 找不到占位符 {pattern}")
    content = content.replace(pattern, repr(val))   # repr 处理多行 PEM / 特殊字符
    changed.append(key)
p.write_text(content, encoding="utf-8")
# 双层校验:replace 后再读回确认占位符都不在了
verify = p.read_text(encoding="utf-8")
for key in changed:
    if f'"__{key}__"' in verify:
        raise SystemExit(f"FATAL: {key} replace 后仍残留")
print(f"==> 凭据已注入 + 校验通过: {','.join(changed)}")
PYEOF

# 写 launcher.py(见 §2)+ 跑 PyInstaller(见 §3)+ 拷 chromium(见 §3.1)
# ...

# create-dmg
create-dmg --volname "YourApp Installer" --window-size 600 400 \
  --app-drop-link 450 200 --icon "your_project.app" 150 200 \
  "dist/your_project-${VERSION}.dmg" "dist/your_project.app"
```

**bash 特性提醒**:
- `set -euo pipefail` 让任何命令失败立刻退出,Python `raise SystemExit` 会让脚本红
- `<<'PYEOF'`(单引号包裹 heredoc)→ **不**展开变量,Python 收原样代码
- `trap '...' EXIT` 任何退出路径都触发

---

## 5. Windows 打包脚本(scripts/build_windows.ps1)

```powershell
$ErrorActionPreference = "Stop"

# ★ 关键 1:强制 Python UTF-8 IO
# Windows runner 默认 locale=cp1252,Python read_text/print 中文都会挂。
# 整个进程树设置一次,所有 Python 子进程(包括 PyInstaller)免疫。
$env:PYTHONUTF8 = "1"

$Version = if ($env:WXSP_VERSION) { $env:WXSP_VERSION } else { "0.1.0" }
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

# 本地便利
if (Test-Path "$ProjectRoot\.apc-env.ps1") { . "$ProjectRoot\.apc-env.ps1" }

Write-Host "==> 清理旧产物"
Remove-Item -Recurse -Force dist, build, your_project.spec -ErrorAction SilentlyContinue

try {
  # 校验凭据
  foreach ($k in @("APC_ENDPOINT","APC_APP_ID","APC_APP_SECRET","APC_PUBLIC_KEY","APC_CERT_FP")) {
    if (-not (Test-Path env:$k)) { throw "$k 必填" }
  }

  # ★ 关键 2:Python 代码必须写到独立 .py 文件,不用 `python -c @"..."@`
  # PowerShell here-string 经 cmd 命令行参数传到 python 时,引号/反斜杠会被错误转义,
  # Python 收到的代码不完整,replace 没匹配上,但不报错 → silent ship 占位符版本。
  @'
import os, pathlib, sys
# 兜底:即使 PYTHONUTF8 失效也强制 stdio 走 UTF-8
for s in (sys.stdout, sys.stderr):
    try: s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
p = pathlib.Path("your_project/apc_config.py")
content = p.read_text(encoding="utf-8")   # ★ 必须 encoding=utf-8
changed = []
for key in ("APC_ENDPOINT","APC_APP_ID","APC_APP_SECRET","APC_PUBLIC_KEY","APC_CERT_FP"):
    val = os.environ.get(key)
    if val is None:
        print(f"FATAL: env var {key} 未设置", file=sys.stderr); sys.exit(1)
    pattern = f'"__{key}__"'
    if pattern not in content:
        print(f"FATAL: 找不到占位符 {pattern}", file=sys.stderr); sys.exit(1)
    content = content.replace(pattern, repr(val))
    changed.append(key)
p.write_text(content, encoding="utf-8")
verify = p.read_text(encoding="utf-8")
for key in changed:
    if f'"__{key}__"' in verify:
        print(f"FATAL: {key} replace 后仍残留", file=sys.stderr); sys.exit(1)
print("OK: injected", ",".join(changed))
'@ | Out-File -Encoding utf8 build/inject_apc.py

  uv run python build/inject_apc.py
  # ★ 关键 3:必须手动检查 $LASTEXITCODE
  # $ErrorActionPreference=Stop 只对 cmdlet 生效,对原生 .exe(uv.exe/python.exe)无效。
  # Python sys.exit(1) → PowerShell 默认会继续往下跑 PyInstaller → ship 占位符版本。
  if ($LASTEXITCODE -ne 0) {
    throw "APC 凭据注入失败(exit=$LASTEXITCODE),拒绝继续打包"
  }

  # 写 launcher.py + 跑 PyInstaller + 拷 chromium(详见 §2-§3.1)
  # ...

  # Inno Setup
  $InnoPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
  & $InnoPath /Qp "/DAppVersion=$Version" "/DSourceDir=$ProjectRoot\dist\your_project" scripts/setup.iss
} finally {
  Write-Host "==> 恢复 apc_config.py 占位符"
  git checkout -- your_project/apc_config.py 2>$null | Out-Null
}
```

**PowerShell 特性提醒**:
- `@'...'@`(单引号 here-string)= 不展开变量,内容原样写入文件
- `try { } finally { }` 任何抛出都会跑 finally,但**原生 .exe 的非零退出码不算抛出**,必须 `if ($LASTEXITCODE -ne 0) { throw }`
- `Set-Location` 切目录;`Test-Path env:$k` 检查 env var 存在(注意:**空字符串也算存在**,用 Python 端 `os.environ.get + is None` 不能区分,要在 ps1 里加 `if ($env:$k -eq "")` 才能拒绝空)

---

## 6. Inno Setup 模板(scripts/setup.iss)

```ini
#define AppName "your_project"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\your_project"
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
```

---

## 7. GitHub Actions workflow

```yaml
name: Build installers

on:
  push:
    tags: ['v*']
  workflow_dispatch:

jobs:
  build-macos:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --all-extras
      - run: uv run patchright install chromium    # 或 playwright
      - run: brew install create-dmg
      - name: Build .dmg
        env:
          WXSP_VERSION: ${{ github.ref_type == 'tag' && github.ref_name || '0.0.0-dev' }}
          APC_ENDPOINT:   ${{ secrets.APC_ENDPOINT }}
          APC_APP_ID:     ${{ secrets.APC_APP_ID }}
          APC_APP_SECRET: ${{ secrets.APC_APP_SECRET }}
          APC_PUBLIC_KEY: ${{ secrets.APC_PUBLIC_KEY }}
          APC_CERT_FP:    ${{ secrets.APC_CERT_FP }}
        run: bash scripts/build_macos.sh
      - uses: actions/upload-artifact@v4
        with: { name: macos-dmg, path: dist/*.dmg }

  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --all-extras
      - run: uv run patchright install chromium
      - run: choco install innosetup -y --no-progress
      - name: Build setup.exe
        env:
          WXSP_VERSION: ${{ github.ref_type == 'tag' && github.ref_name || '0.0.0-dev' }}
          APC_ENDPOINT:   ${{ secrets.APC_ENDPOINT }}
          # ... 同上 5 个 secret
        shell: pwsh
        run: |
          $OutputEncoding = [System.Text.UTF8Encoding]::new()
          [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
          .\scripts\build_windows.ps1
      - uses: actions/upload-artifact@v4
        with: { name: windows-exe, path: dist/*setup*.exe }

  release:
    needs: [build-macos, build-windows]
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    permissions: { contents: write }
    steps:
      - uses: actions/download-artifact@v4
        with: { path: artifacts }
      - name: Create GitHub Release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GH_REPO: ${{ github.repository }}
        run: |
          gh release create "${{ github.ref_name }}" \
            artifacts/macos-dmg/*.dmg \
            artifacts/windows-exe/*setup*.exe \
            --generate-notes
```

**触发**:`git tag v1.2.3 && git push origin v1.2.3` → 自动跑 build + release。**不打 tag 只 push main 不会出包**(workflow_dispatch 手动也行)。

**GitHub secrets 配置位置**:`Settings → Secrets and variables → Actions → New repository secret`。5 个 secret 名字必须**完全一致**(大小写、下划线)。

**额度提示**:private repo 跑 GitHub Actions 用免费额度(目前 macos 每分钟 10x、win 2x),build 一次 mac 6 min + win 8 min ≈ 76 min,免费额度 2000 min/月够用。public repo 不消耗额度。

---

## 8. 凭据注入完整模式(踩坑最多)

源码 `your_project/apc_config.py` 永远是占位符:
```python
APC_ENDPOINT = "__APC_ENDPOINT__"
APC_APP_ID = "__APC_APP_ID__"
APC_APP_SECRET = "__APC_APP_SECRET__"
APC_PUBLIC_KEY = "__APC_PUBLIC_KEY__"
APC_CERT_FP = "__APC_CERT_FP__"
```

业务代码 `from your_project.apc_config import APC_ENDPOINT, ...` 读这些常量。打包时 build 脚本把占位符 sed 成真值,PyInstaller 编译 .pyc → 真值嵌进字节码,源码立即 git checkout 还原。

**5 个必须做的事**(每一个我们都因为没做踩过坑):

1. **`read_text` / `write_text` 显式 `encoding="utf-8"`** — Windows runner locale=cp1252,默认编码会解 UTF-8 文件出 `UnicodeDecodeError`(我们 apc_config.py 有中文 docstring 命中过)
2. **Python 代码写独立 .py 文件,不用 `python -c "..."`** — PowerShell here-string → cmd → python 这条参数链转义不可靠,字符串被破坏后 replace 匹配不上但不报错
3. **PowerShell 必须手动检查 `$LASTEXITCODE`** — `$ErrorActionPreference=Stop` 不管原生 .exe,Python `sys.exit(1)` 默认被吞,继续 PyInstaller → ship 占位符版本
4. **双层校验**:replace 前 `if pattern not in content: FATAL`,replace 后 `if pattern in verify: FATAL` — 防御 silent skip,任一失败让 build 红
5. **`trap EXIT` / `try/finally` 还原源码** — 不然 build 失败时本地 working tree 留着真凭据,容易误 commit 上 GitHub

**额外建议**:
- `$env:PYTHONUTF8 = "1"`(Windows 端):激活 Python 3.7+ UTF-8 mode,整个进程树 stdio + 默认 file encoding 都走 UTF-8,免疫 cp1252 类问题
- `repr(val)` 转 Python 字面量:处理多行 PEM、空字符串、含引号的值都正确

---

## 9. 日志在 frozen 模式的坑

如果你用 `loguru`(或类似异步日志库),**默认行为在 PyInstaller frozen 模式下会 silent 失效**。两个必修:

```python
# archive.py 或 logging.py
def install_file_sink(*, logs_dir: Path, retention_days: int):
    sink_kwargs = {
        "rotation": "00:00",
        "retention": f"{retention_days} days",
        "compression": "zip",
        "encoding": "utf-8",
        "level": "INFO",
        # ★ frozen + Windows 下 multiprocessing worker 子进程启动失败,
        #   日志全卡 queue 不落盘,文件永远 0 字节。单 worker 项目设 False 最稳。
        "enqueue": False,
    }
    logger.add(str(logs_dir / "app.{time:YYYY-MM-DD}.log"), **sink_kwargs)
```

```python
# cli.py:web 命令开头
from loguru import logger as _logger
# frozen + Windows 下 loguru 默认 stderr sink 可能失效(sys.stderr 被 PyInstaller wrap)
# 显式再 add 一个 stderr sink 保底,确保有输出
_logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <5} | {message}")
```

**Web UI 实时日志流**(如果有):用 loguru sink 推到 asyncio.Queue + SSE。启动时往 history ring buffer 推一条 banner,确保用户打开页面**至少能看到一行**,不会误以为坏了。

**`--windowed` 模式**(mac .app 双击不弹 Terminal):`sys.stdout` / `sys.stderr` 是 `None`,loguru add(sys.stderr) 会抛 `TypeError`。要 `if sys.stderr is not None: logger.add(...)` 保护。

---

## 10. 验证:写一个 `doctor` 命令

打包后没法在客户机器上 debug,**必须**在 CLI 里加 doctor 子命令做自检。我们的标准模板:

```python
@app.command("doctor")
def doctor():
    """打印 logs_dir / 凭据前缀 / 网络探测结果,所有用 typer.echo 不依赖 loguru。"""
    settings = load_settings()
    typer.echo(f"logs_dir = {settings.app.logs_dir}")
    typer.echo(f"is_packaged = {is_packaged()}")

    # 关键:凭据已注入校验(打包模式下)
    if is_packaged():
        from your_project.apc_config import APC_ENDPOINT, APC_APP_ID
        if APC_ENDPOINT.startswith("__"):
            typer.echo(f"❌ APC 凭据未注入:endpoint={APC_ENDPOINT}")  # 占位符 = build bug
        else:
            typer.echo(f"endpoint={APC_ENDPOINT[:40]}...")
            typer.echo(f"app_id={APC_APP_ID[:12]}...")
            # 直接调底层网络请求,把异常 type + repr 暴露出来
            try:
                from apc_sdk._http import fetch_session
                # ... 构造 client + cfg
                fetch_session(client, cfg, device_id=None)
                typer.echo("✅ APC check OK")
            except Exception as e:
                typer.echo(f"❌ {type(e).__name__}: {e}")
                typer.echo(f"   repr: {e!r}")
```

**为什么用 typer.echo 而不是 loguru**:doctor 是兜底诊断,如果 loguru 自己就坏了,doctor 还得 work。`typer.echo` 走 `click.echo`,内部处理了 Windows console UTF-8,**最不容易挂**。

---

## 11. 已知踩坑速查表

| # | 现象 | 真正原因 | 修法 |
|---|---|---|---|
| 1 | Windows build 看似成功,装上后凭据是 `__XXX__` 占位符 | PowerShell 不管原生 .exe 退出码,inject_apc.py 失败被吞 | `if ($LASTEXITCODE -ne 0) { throw }` |
| 2 | Windows inject_apc 抛 `UnicodeDecodeError: cp1252 ...` | runner locale 默认 cp1252,读 UTF-8 文件挂 | `read_text(encoding="utf-8")` |
| 3 | Windows print 中文抛 `UnicodeEncodeError: cp1252 ...` | Python stdout 默认 cp1252,中文 unicode 编码不了 | `$env:PYTHONUTF8 = "1"` + `sys.stdout.reconfigure(encoding="utf-8")` |
| 4 | Web UI 日志页面空白、文件 0 字节 | loguru `enqueue=True` 的 multiprocessing worker 在 frozen 下启动失败 | `enqueue=False` + launcher.py 加 `multiprocessing.freeze_support()` |
| 5 | mac 双击 .app 启动 ImportError | 某个用 `importlib_metadata` / lazy import 的依赖没被 `--collect-all` | 把那个依赖加进去,**dev 测不出来** |
| 6 | patchright/playwright 启动找不到浏览器 | chromium 文件夹层级丢了 | 内嵌时**保留 `chromium-<版本>` 这一层**(BSD cp 用 `src/.` 拷内容) |
| 7 | mac .app 双击没反应、看不到任何 stderr | `--windowed` 模式 stderr 接到 NUL | 改 `--console` 接受弹 Terminal,或加文件 sink + doctor 子命令 |
| 8 | GitHub Actions 改了 yml/secret 后还在用旧值 | tag 不会自动用最新 main | `git tag -d v1.2.3 && git push origin :v1.2.3` 删旧 tag 重打 |
| 9 | 装上后启动一闪而过 | console 模式 main 抛异常直接关窗口 | 让用户从 cmd 里 `app.exe > out.log 2>&1` 跑,看 log |
| 10 | inject 完了源码 working tree 还是真值,差点 commit 上 git | finally 没跑(脚本被 kill -9) | `trap '...' EXIT` 是 bash 兜底;PowerShell 用 `try/finally`;**+ `.gitignore` 加 `apc_config.py` 是过度防御,不要这么干**(占位符版本必须 commit) |

---

## 12. 发版流程速查

```bash
# 1. 本地准备
cp config.example.yaml config.yaml          # 第一次
cp .apc-env.example .apc-env                # 第一次,填真值
chmod +x scripts/build_macos.sh

# 2. 本地试打 mac dmg(可选,验证脚本对)
source .apc-env && bash scripts/build_macos.sh
open dist/wxsp-0.1.0.dmg                    # 拖装,跑 doctor 验证

# 3. 推 tag 触发 CI
git tag v0.2.5
git push origin main v0.2.5                  # 触发 build + release

# 4. CI 跑完(mac 6 min + win 8 min + release 2 min ≈ 16 min)
gh run watch                                 # 或网页看
gh release view v0.2.5                       # 看 assets

# 5. 失败回退
git tag -d v0.2.5                            # 删本地 tag
git push origin :v0.2.5                      # 删远程 tag
gh release delete v0.2.5 --yes               # 删 release
# 修完后重打同名 tag
```

---

## 13. 不要做的事

- ❌ `--windowed` + 没 doctor 子命令:出问题没任何信息可看
- ❌ 把 `apc_config.py` 加进 `.gitignore`:打包脚本依赖它在源码里存在
- ❌ commit `*.spec`:PyInstaller 第一次跑会生成,但下次 build 用旧 spec 跳过依赖分析
- ❌ 把凭据放进 `config.yaml` 让用户自己填:能 ship 真凭据的本质是字节码,放配置文件等于明文
- ❌ 用 `os.environ[KEY]` 读凭据(运行时):打包产物没 env,要从 `apc_config.py` 静态常量读
- ❌ 在 launcher.py 用 `multiprocessing.set_start_method("fork")`:Windows 上不支持
- ❌ `python -c "code"` 传复杂 Python 代码:转义陷阱,改写文件再 `python file.py`

---

## 附:wxsp 实际产物大小(参考)

| 项 | 大小 |
|---|---|
| `wxsp-0.2.5.dmg`(mac arm64) | 250 MB |
| `wxsp-0.2.5-setup.exe`(win x64) | 200 MB |
| `wxsp.app` 解压后 | ~600 MB |
| `wxsp/`(Inno 安装后) | ~700 MB |
| 其中 chromium 占 | ~500 MB |

如果不用 playwright/patchright,体积可降到 30-50 MB。
