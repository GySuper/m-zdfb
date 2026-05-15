# M11 安装器 + 首次设置向导 设计文档

> **⚠️ 部分废弃(2026-05-15)**:本文档中所有"开机自启 / autostart / launchctl / schtasks / `wxsp/autostart.py` / `deploy/wxsp.plist` / `deploy/wxsp-task.xml`"相关内容(含 §5、§8 表格 autostart 行、向导第 6 步的"开机自启"复选框)**均已从产品移除**。当前形态:daemon 由用户从 Web UI / CLI 手动启动,不再注册到 launchd / 任务计划程序。其余章节(Nuitka 打包、Web UI 向导前 5 步)仍有效。
>
> 本文档是整体设计 [`2026-05-12-wxsp-design.md`](2026-05-12-wxsp-design.md) 的 M11 milestone 细化版,**对上层设计的偏离会在 §8 列明**。M11 在 M10(部署 + 文档)之后,目标是把"开发者风格的 CLI 安装流程"换成"运营双击装 + Web UI 配置向导"的产品形态。

**Goal**:运营拿到一个 `.dmg`(mac) / `.exe`(win) 双击装,装完自动起 Web UI,在浏览器里走完一个 5 步向导填飞书 / NAS / 账号 / 告警配置,点完成就能开机自启跑。**运营不接触命令行**,**装出来的程序不暴露 Python 源码**(Nuitka 编译挡一般技术人员)。

**Tech Stack**:Nuitka(Python → C → 机器码) + `create-dmg`(mac 打包) + Inno Setup(win 打包) + GitHub Actions(CI 出包) + `platformdirs`(用户数据目录跨平台) + 现有 FastAPI/Jinja2 UI 栈。

---

## 1. M11 范围

### 1.1 交付物

| 文件 | 改动 |
|------|------|
| `wxsp/config.py` | `data_dir` / `logs_dir` 默认值改成 `platformdirs.user_data_dir("wxsp")` / `user_log_dir("wxsp")`;开发模式(`WXSP_DEV_MODE=1`)仍用 `./data` / `./logs` |
| `wxsp/api/app.py` | startup hook 检查 config.yaml 是否存在,不存在则进 setup 模式,所有非 setup 路由返回 302 → `/setup` |
| `wxsp/api/routes_setup.py` | **新文件**,向导后端:`GET /setup/step/{1-6}` + `POST /setup/step/{1-6}` + `POST /setup/test-feishu` + `POST /setup/complete`(写 config.yaml + 注册自启 + 跳 dashboard) |
| `wxsp/templates/setup/*.html` | **新目录**,6 个向导页 + 1 个 base 模板 |
| `wxsp/autostart.py` | **新文件**,跨平台开机自启注册/反注册/查询状态 |
| `wxsp/browser.py` | 检测打包模式(`__compiled__` 属性 by Nuitka),改用打包内 Chromium 路径 |
| `wxsp/cli.py` | daemon 启动后自动开浏览器到 `127.0.0.1:8765`(已存在的 `webui.open_browser_on_start` 配置项收紧到打包模式默认开) |
| `scripts/build_macos.sh` | **新文件**,Nuitka 编译 + chromium 内嵌 + create-dmg 打包 |
| `scripts/build_windows.ps1` | **新文件**,同上 win 版,Inno Setup 出 setup.exe |
| `scripts/setup.iss` | **新文件**,Inno Setup 脚本(Windows 安装器 UI 模板) |
| `.github/workflows/build.yml` | **新文件**,tag 触发 + workflow_dispatch 手动触发,出 macOS .dmg + Windows .exe → Release |
| `deploy/wxsp.plist.tmpl` | **新文件**(不是改名现有 plist):打包模式专用,`ProgramArguments` 结构由 `uv run wxsp run --daemon`(5 段)改成 `<wxsp_bin> run --daemon`(3 段);保留 `__INSTALL_DIR__` 占位符 |
| `deploy/wxsp-task.xml.tmpl` | **新文件**(不是改名现有 xml):打包模式专用,`<Arguments>` 由 `run wxsp run --daemon` 改成 `run --daemon` |
| 现有 `deploy/wxsp.plist` / `wxsp-task.xml` | **保留不动**(给开发模式手动注册自启用) |
| `pyproject.toml` | 加 `platformdirs>=4.0.0` |

### 1.2 测试文件

- `tests/test_paths.py` — **新文件**,测 `config.py` 在 `WXSP_DEV_MODE=1` 和 unset 两种情况下 `data_dir` 解析结果
- `tests/test_autostart.py` — **新文件**,渲染 plist/xml 模板的纯函数测试(不真调 launchctl/schtasks,monkeypatch subprocess)
- `tests/test_routes_setup.py` — **新文件**,wizard 每步 POST + 校验 + final commit 写出 yaml 的集成测试
- Nuitka 编译产物本身**不写自动化测试**,验收靠 §7 的手工冒烟

### 1.3 不动的

- `wxsp/publisher.py` / `selectors.py` / `feishu.py` / `nas.py` / `scheduler.py` — 业务核心不碰
- `wxsp/api/routes_config.py` — 已有"高级配置"页保留(向导走完后,常规改配置走这里)
- `wxsp/api/routes_accounts.py` — 扫码登录页保留,向导第 6 步会跳过去
- 现有 `deploy/wxsp.plist` / `wxsp-task.xml` — 完全保留(留给开发模式手动注册);打包模式用新文件 `*.plist.tmpl` / `*.xml.tmpl`,见 §1.1

### 1.4 不做的(YAGNI)

- **自动检查更新** — 运营要更新就重新装。要做就半天加个"检查更新"按钮调 GitHub release API,不是 M11 范围
- **多语言** — 向导只做中文,代码里也只放中文文案,后续要做英文版再抽 i18n
- **许可证 / 机器码绑定** — 用户明确"自己人用",不做
- **静默安装 / 命令行卸载** — 运营自己拖拽/卸载就行
- **加密用户数据目录里的 cookie** — patchright 必须能读到,加密代价大于收益。CLAUDE.md §"安全"里已经说"不要把 data/ 上传到第三方",物理保护交给操作系统用户权限
- **macOS .pkg 安装包** — 没 Apple 开发者账号,`.pkg` 没法用右键绕过 Gatekeeper;改用 `.dmg` + 拖拽进 Applications,首次打开右键→打开一次即可
- **签名 / 公证** — 用户拒绝付 $99/年 Apple Developer;Windows 不签名运营第一次会看到 SmartScreen 警告但能点"仍要运行"。**这些会在 README 部署章节写清楚**
- **CI 跑 Nuitka 编译产物的端到端测试** — 视频号要 headless=false + 真扫码,CI 上没法跑,人工冒烟
- **多账号配置导入/导出** — 向导只支持手填,后续要做"从 JSON 导入"再加
- **代码混淆补强(PyArmor 之类)** — 用户选了"拦运营 + 一般技术人员"档,Nuitka 足够。要加更高强度再开新 milestone

---

## 2. 用户旅程

### 2.1 首次安装(macOS)

```
1. 双击 wxsp-1.0.0.dmg → 弹挂载窗
2. 拖 wxsp.app → /Applications
3. 双击 wxsp.app
   ├─ Gatekeeper 拦截 → 右键 → 打开 → 确认(只这一次)
   ├─ app 启动 daemon (FastAPI:127.0.0.1:8765)
   ├─ daemon startup hook 检测 ~/Library/Application Support/wxsp/config.yaml 不存在
   ├─ 进入 setup 模式
   └─ 自动开浏览器到 http://127.0.0.1:8765/setup/step/1
4. 浏览器里走完 6 页向导(详见 §4)
5. 最后一步点"完成":
   ├─ 写 config.yaml 到 ~/Library/Application Support/wxsp/
   ├─ 勾选了"开机自启" → 写 LaunchAgent
   ├─ 热加载配置(pydantic-settings 重新读)
   ├─ daemon 退出 setup 模式
   └─ 浏览器跳到 /accounts(引导扫码)
6. 在 /accounts 页对 4 个账号挨个点"扫码登录"
7. 完成,日常用就是浏览器访问 :8765 看 Dashboard
```

### 2.2 首次安装(Windows)

```
1. 双击 wxsp-1.0.0-setup.exe
2. SmartScreen 警告 → "更多信息" → "仍要运行"(只这一次)
3. Inno Setup 安装向导(英文/中文可选):
   ├─ 选安装目录 (默认 C:\Program Files\wxsp\)
   ├─ ☑ 开机自动启动 (勾选则装完后注册任务计划程序)
   ├─ ☑ 创建桌面快捷方式
   └─ 安装
4. 装完默认勾选"启动 wxsp"→ 等同 macOS 步骤 3 之后
```

### 2.3 重装 / 换机

```
mac:    备份 ~/Library/Application Support/wxsp/ 整个目录 → 新机器装好 app → 把目录复制过去 → 启动跳过向导直接 Dashboard
win:    备份 %APPDATA%\wxsp\ → 同上
```

### 2.4 卸载

```
mac:    /Applications/wxsp.app 拖进废纸篓 + 在「系统设置 → 通用 → 登录项」移除 wxsp
        (用户数据目录 ~/Library/Application Support/wxsp/ 不自动删,需手工清)
win:    控制面板 → 卸载程序 → wxsp(卸载器自动反注册任务计划程序)
        (用户数据目录 %APPDATA%\wxsp\ 不自动删,需手工清)
```

---

## 3. 用户数据目录跨平台映射

| 平台 | 路径 | 由谁决定 |
|------|------|---------|
| macOS | `~/Library/Application Support/wxsp/` | `platformdirs.user_data_dir("wxsp")` |
| Windows | `%APPDATA%\wxsp\`(= `C:\Users\<u>\AppData\Roaming\wxsp\`) | `platformdirs.user_data_dir("wxsp")` |
| Linux(开发者偶尔用) | `~/.local/share/wxsp/` | `platformdirs.user_data_dir("wxsp")` |

目录结构(跟现有 `./data` `./logs` 一致):

```
~/Library/Application Support/wxsp/
├── config.yaml                  # 向导写出
├── data/
│   ├── db.sqlite
│   ├── chrome-profiles/{account_a,...}/
│   └── tmp/
└── logs/
    ├── wxsp.YYYY-MM-DD.log
    └── screenshots/{YYYYMM}/
```

### 3.1 开发模式 vs 打包模式

| 模式 | 检测条件 | data_dir / logs_dir 默认值 |
|------|---------|--------------------------|
| 打包模式 | `hasattr(sys.modules['__main__'], '__compiled__')` 为真(Nuitka 注入) | `platformdirs.user_*` |
| 开发模式 | 上式为假 **或** 环境变量 `WXSP_DEV_MODE=1` | `./data` / `./logs`(项目根) |

`config.yaml` 里仍允许显式覆盖(`app.data_dir: /custom/path`),默认值变化对老用户的开发模式无感。

---

## 4. 首次设置向导(6 页)

### 4.1 页面 1:欢迎 + 环境自检

- 显示版本号 / 数据目录路径(只读,告诉用户配置会落在哪)
- 自检:
  - Chromium 是否在打包目录就位(检 `app/Resources/chromium` 或 `wxsp\chromium\`)
  - 用户数据目录是否可写
  - 端口 8765 是否可用(应该是,因为 daemon 自己占着)
- 自检失败 → 红字提示 + 不让点"下一步"

### 4.2 页面 2:飞书配置

输入框:
- `app_id`(单行)
- `app_secret`(密码框)
- `bitable.app_token`(单行)
- `bitable.table_id`(单行)

按钮:
- **"测试连接"** → POST /setup/test-feishu → 用填的值实例化 `LarkClient` + 调 list_records(limit=1),成功显示 ✓ + 拉到的字段名列表(给用户对照 field_map),失败显示具体错误码
- **"下一步"** → 没点过测试连接就 disabled(强制验证)

字段映射(`field_map`)**不**在向导里露:用 `config.example.yaml` 的默认中文字段名。运营如果字段名不一样,后续在"高级配置"页改 yaml。

### 4.3 页面 3:NAS 路径

- `paths.nas_root`(单行)+ 旁边"打开文件选择器"按钮(用浏览器 `<input type="file" webkitdirectory>` ,折中 — 真原生选择器要 Electron/Tauri,现在不上)
- 输入后实时检测(POST /setup/probe-path) → 显示 ✓ 路径存在 / ✗ 路径不可达
- 路径可以是 `/Volumes/NAS/wxsp`(mac 挂载点) 或 `Z:/wxsp`(win 映射盘)或 UNC `\\server\share\wxsp`

### 4.4 页面 4:账号配置

每个账号一个卡片(默认显示 1 个,下方"+ 添加账号"按钮最多加到 8 个):

- `id`(单行,英文蛇形,如 `account_a`)
- `display_name`(单行,如"美食号")
- `daily_limit`(数字,默认 20)
- `video_search_root`(单行,默认 `{nas_root}/videos/{id}`)
- `cover_search_root`(单行,默认 `{nas_root}/covers/{id}`)
- 删除按钮(只在 ≥2 个账号时显示)

校验:
- 至少 1 个账号
- `id` 唯一 + 符合 `^[a-z][a-z0-9_]*$`
- `daily_limit` ≥ 1

### 4.5 页面 5:告警(可选)

- `monitoring.notifiers.wecom.webhook`(密码框,可留空)
- 留空则 `enabled: false`
- 按钮"测试推送"(POST /setup/test-wecom)→ 发一条测试消息 "wxsp 安装成功" 到 webhook

### 4.6 页面 6:完成

- 复述将要写入的关键配置(账号数 / NAS 路径 / 飞书 app_id 截断后 6 位)
- ☑ 开机自动启动(默认勾选)
- 按钮"完成,进入主界面":
  1. POST /setup/complete
  2. 后端:render config.yaml(用现有 `config.example.yaml` 作为模板,Jinja2 渲染填入用户输入)
  3. 写到 user_data_dir / config.yaml
  4. 勾选了 → 调 `autostart.enable_autostart()`
  5. `wxsp.config.get_settings.cache_clear()` 让下次读取重新加载
  6. 返回 302 → /accounts(并附 flash message"配置已保存,请扫码登录每个账号")

---

## 5. 开机自启实现(autostart.py)

### 5.1 接口

```python
def enable_autostart() -> None: ...
def disable_autostart() -> None: ...
def is_autostart_enabled() -> bool: ...
```

调用方:
- `routes_setup.py::complete` — 向导勾选时调 enable
- `routes_accounts.py` 或新增 `routes_settings.py` 提供 UI 开关(超出 M11 范围,可在 M12)
- CLI `wxsp autostart enable/disable/status`(便利接口,~20 行)

### 5.2 macOS 实现要点

- 渲染 `deploy/wxsp.plist.tmpl`(新结构,`ProgramArguments` 只有 3 个 string)填入:
  - `__INSTALL_DIR__` → 用户数据目录(`platformdirs.user_data_dir("wxsp")`,日志写在这里)
  - `__WXSP_BIN__` → app bundle 主程序绝对路径(`/Applications/wxsp.app/Contents/MacOS/wxsp`)
- 写到 `~/Library/LaunchAgents/com.wxsp.daemon.plist`
- `subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)])` 注册
- 检测已注册:`launchctl print gui/$(id -u)/com.wxsp.daemon` 退出码为 0
- 反注册:`launchctl bootout gui/{uid}/com.wxsp.daemon` + 删 plist

### 5.3 Windows 实现要点

- 渲染 `deploy/wxsp-task.xml.tmpl`(新结构,`<Arguments>` 只剩 `run --daemon`)填入:
  - `__INSTALL_DIR__` → 用户数据目录(`platformdirs.user_data_dir("wxsp")`)
  - `__WXSP_BIN__` → `C:\Program Files\wxsp\wxsp.exe`(填进 `<Command>` 节点)
  - `__USERNAME__` → `os.getlogin()`
- 临时写到 `%TEMP%\wxsp-task.xml`
- `subprocess.run(["schtasks", "/Create", "/TN", "wxsp-daemon", "/XML", tmp, "/F"], encoding="utf-16-le")`(M10 已踩坑,xml 用 UTF-16 LE 编码)
- 检测:`schtasks /Query /TN wxsp-daemon` 退出码为 0
- 反注册:`schtasks /Delete /TN wxsp-daemon /F`

---

## 6. 打包(Nuitka + CI)

### 6.1 Nuitka 命令(macOS,build_macos.sh 节选)

```bash
uv run python -m nuitka \
  --standalone \
  --macos-create-app-bundle \
  --macos-app-name=wxsp \
  --macos-app-icon=assets/icon.icns \
  --include-package=wxsp \
  --include-package-data=wxsp \
  --include-data-dir=wxsp/templates=wxsp/templates \
  --include-data-files=config.example.yaml=config.example.yaml \
  --output-dir=dist \
  wxsp/__main__.py

# Nuitka 出来 dist/wxsp.app
# 把 patchright 的 chromium 整目录 copy 进去
PATCHRIGHT_BROWSERS=$(uv run python -c "import patchright; print(patchright.__path__[0])")/driver
cp -R "$PATCHRIGHT_BROWSERS/chromium-*" dist/wxsp.app/Contents/Resources/chromium/

# create-dmg 打 dmg
create-dmg \
  --volname "wxsp Installer" \
  --window-size 600 400 \
  --app-drop-link 450 200 \
  dist/wxsp-1.0.0.dmg \
  dist/wxsp.app
```

需要新建 `wxsp/__main__.py`:

```python
from wxsp.cli import app
if __name__ == "__main__":
    app()
```

### 6.2 Windows(build_windows.ps1 + setup.iss)

PowerShell 脚本同思路,Nuitka 加 `--windows-disable-console`(daemon 模式) 或留 console(便于调试日志)。出来 `dist\wxsp.exe` + 一堆依赖目录。

`setup.iss`(Inno Setup 脚本)负责把 `dist\` 整个目录打包成 `wxsp-1.0.0-setup.exe`,提供:
- 安装目录选择
- "开机自启" 复选框 → `[Run]` 段调 `wxsp.exe autostart enable`
- "桌面快捷方式" 复选框
- 卸载器自动调 `wxsp.exe autostart disable`

### 6.3 Chromium 路径解析(browser.py 改动)

```python
def _chromium_root() -> Path:
    if hasattr(sys.modules['__main__'], '__compiled__'):
        # 打包模式
        if sys.platform == "darwin":
            return Path(sys.executable).parent.parent / "Resources/chromium"
        else:
            return Path(sys.executable).parent / "chromium"
    # 开发模式 — 用 patchright 默认查找逻辑
    return None  # 返 None 时 patchright 用 PLAYWRIGHT_BROWSERS_PATH 或默认位置

def launch_browser(account):
    chromium_root = _chromium_root()
    if chromium_root:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(chromium_root)
    # ... 现有逻辑不变
```

### 6.4 GitHub Actions CI(.github/workflows/build.yml)

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
      - run: uv sync
      - name: Cache patchright chromium
        uses: actions/cache@v4
        with:
          path: ~/.cache/ms-playwright
          key: chromium-mac-${{ hashFiles('uv.lock') }}
      - run: uv run patchright install chromium
      - run: brew install create-dmg
      - run: bash scripts/build_macos.sh
      - uses: actions/upload-artifact@v4
        with: { name: macos, path: dist/*.dmg }

  build-windows:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - name: Cache patchright chromium
        uses: actions/cache@v4
        with:
          path: ~\AppData\Local\ms-playwright
          key: chromium-win-${{ hashFiles('uv.lock') }}
      - run: uv run patchright install chromium
      - run: choco install innosetup
      - run: powershell scripts/build_windows.ps1
      - uses: actions/upload-artifact@v4
        with: { name: windows, path: dist/*setup.exe }

  release:
    needs: [build-macos, build-windows]
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    permissions: { contents: write }
    steps:
      - uses: actions/download-artifact@v4
      - run: gh release create ${{ github.ref_name }} macos/*.dmg windows/*setup.exe --generate-notes
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### 6.5 包体积预估

| 平台 | 预估大小 | 主要来源 |
|------|---------|---------|
| .dmg | 400-500 MB | Chromium 280MB + Python runtime ~80MB + 依赖 ~50MB + Nuitka 编译产物 ~30MB |
| -setup.exe | 350-450 MB | 同上,Inno Setup 压缩稍紧 |

---

## 7. 验收标准(M11 完成判定)

人工冒烟,**两个平台都要过**。每条都打勾才算完。

### 7.1 打包流程

- [ ] `git tag v0.1.0-rc1 && git push --tags` 触发 CI,15 分钟内 Release 页有 2 个文件(.dmg + setup.exe)
- [ ] CI 第二次跑利用 chromium 缓存,时间下降到 ~10 分钟
- [ ] 手动 `workflow_dispatch` 也能跑(不打 tag)

### 7.2 macOS 安装

- [ ] 双击 .dmg 弹窗正常,拖 wxsp.app → /Applications 完成
- [ ] 首次右键打开,Gatekeeper 二次确认后 app 启动
- [ ] 浏览器自动打开到 :8765/setup/step/1
- [ ] 走完 6 步向导(造一个测试飞书表 + 测试 NAS 目录),"完成"按钮后浏览器跳 /accounts
- [ ] `~/Library/Application Support/wxsp/config.yaml` 已生成,内容合法(`wxsp doctor` 在终端模式下能直接读)
- [ ] 勾选"开机自启" → `launchctl print gui/$(id -u)/com.wxsp.daemon` 退出码 0
- [ ] 重启 mac → 登录后 30 秒内 daemon 自动起,浏览器自动开 Dashboard
- [ ] `~/Library/Application Support/wxsp/` 拖到第二台机器的同位置 → 第二台 app 启动后跳过向导,直接 Dashboard

### 7.3 Windows 安装

- [ ] SmartScreen 警告后能"仍要运行",Inno Setup 向导跑通
- [ ] 装在 `C:\Program Files\wxsp\` 默认目录,文件未被杀毒拦截
- [ ] 同 macOS 走完向导
- [ ] `%APPDATA%\wxsp\config.yaml` 已生成
- [ ] `schtasks /Query /TN wxsp-daemon` 退出码 0
- [ ] 重启 Windows + 登录 → 30 秒内 daemon 自动起
- [ ] 控制面板"卸载程序"能干净卸载(task scheduler 项移除)

### 7.4 端到端验证

- [ ] 向导走完,扫码登录 1 个账号,在飞书测试表里加一条"执行日期=今天"的任务,点 Web UI"立即同步 + 跑今天" → 浏览器弹出 → 发布成功(`--dry-run` 模式即可)
- [ ] 退出 app,把用户数据目录中的 `chrome-profiles/account_a/` 删掉模拟 cookie 失效 → 重启 app → Dashboard 显示该账号 cookie 状态 = expired
- [ ] 反编译验证:`strings /Applications/wxsp.app/Contents/MacOS/wxsp | grep -i "selectors\|publisher"` 应该捞不到清晰的业务字符串(Nuitka 编译后函数名 mangled);Python 字节码反编译工具(uncompyle6)对 Nuitka 产物无能为力

### 7.5 文档

- [ ] README "安装" 章节重写:面向运营,不再有 git clone / uv 步骤,改成"双击 .dmg / setup.exe"
- [ ] README 加"首次打开警告处理"小节(mac 右键打开 / win SmartScreen)
- [ ] CLAUDE.md "起步任务清单" 加 M11 一行,更新依赖关系图
- [ ] 保留一个"开发者从源码运行"小节(`uv sync && uv run wxsp web`),给二次开发用

---

## 8. 与上层设计的偏离

| 偏离点 | 上层设计 | M11 调整 | 理由 |
|--------|---------|---------|------|
| `data_dir` 默认值 | `./data` | 打包模式下 `platformdirs.user_data_dir("wxsp")`;开发模式不变 | app bundle 内目录只读,且多用户场景每用户独立数据 |
| 部署方式 | "deploy/wxsp.plist + 用户手改占位符" | autostart.py 自动渲染 + 自动注册,deploy/*.tmpl 仅作模板 | 运营不接触命令行的核心要求 |
| Secret 存储 | "用 `${ENV_VAR}` 引用,不进 yaml" | 向导填进 config.yaml 明文;config.yaml 在用户私有目录(700 权限) | 运营机本来就是单用户独占,环境变量对运营不友好;权衡后选简单。**git 层面的"不进 yaml"原则不变 — config.yaml 不在 repo 里** |
| CLAUDE.md §"起步任务清单" | M0-M10 共 11 个 milestone | 加 M11(在 M10 之后) | 安装器是新需求,不是已规划范围 |
| 包管理依赖 | `uv` | 运营机不再需要 uv / Python(Nuitka 内嵌);开发者依旧用 uv | 运营零开发环境 |

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| **Nuitka 编译 patchright 失败**(C 扩展兼容性) | M11 出不来 | 先在本地小样验证(只编 wxsp 核心 + patchright,跑 hello world 启动 chromium),通过再上 CI |
| **CI 跑 patchright install chromium 频繁失败**(网络) | 发版被卡 | 用 actions/cache 缓存 chromium 目录;失败重试 3 次 |
| **mac 上 .dmg 不签名,新 macOS 版本(Sequoia+)直接拒绝运行** | 运营装不了 | README 写清"右键打开"步骤;若彻底拒绝则 fallback 到先把 .app 解除隔离 `xattr -dr com.apple.quarantine /Applications/wxsp.app`(用户走一次终端命令);最终兜底是花 $99 公证 |
| **Windows Defender / 杀毒软件误报 Nuitka 编译产物**(常见) | 运营装的时候被隔离 | 已知问题,README 注明"信任 wxsp.exe";长期靠代码签名缓解,M11 不做 |
| **chromium 在 app bundle 内启动权限不足** | 浏览器拉不起来 | 测试用 `os.chmod(chromium_exec, 0o755)` 兜底;Nuitka 打包时保留可执行位 |
| **包体积接近 500MB 影响下载** | 运营首次安装慢 | 接受;实际上 Chromium 250MB 不可避免,跟 Electron 应用同量级 |
| **向导走到一半浏览器关掉** | 状态丢失,要重来 | 每步 POST 时把数据存到内存(`app.state.wizard_data` dict),重新打开 /setup 自动跳到最后填到的那页 |

---

## 10. 时间预估

| 子任务 | 工时 |
|--------|-----|
| platformdirs 接入 + 开发/打包模式检测 + 测试 | 0.5d |
| autostart.py + 模板渲染 + 平台分支测试 | 1d |
| routes_setup.py + 6 个模板页 + 校验 | 2d |
| browser.py chromium 路径适配 + 本地验证 | 0.5d |
| build_macos.sh + 本地编一次 .dmg 跑通 | 1d |
| build_windows.ps1 + setup.iss + 本地编一次 .exe 跑通 | 1d |
| CI workflow + 调试缓存 | 1d |
| 验收冒烟 + README 重写 + CLAUDE.md 更新 | 0.5d |
| **合计** | **7.5d** |

属于"中型" milestone,可拆成两段交付:**M11.1 = 打包跑通**(前 6 项),**M11.2 = 向导 + CI**(后 2 项)。
