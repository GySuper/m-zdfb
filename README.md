# wxsp — 微信视频号自动发布工具

本地运行的视频号自动发布工具:**飞书 Bitable 为单一任务源**,每天 09:00 cron 触发跑当日任务,Web UI 做运维控制台(查看 / 重试 / 扫码 / 告警 / 配置)。

支持 macOS + Windows 单机部署,4 账号 × 20 条/天规模。

> 设计与决策细节见 [CLAUDE.md](CLAUDE.md)、[docs/superpowers/specs/2026-05-12-wxsp-design.md](docs/superpowers/specs/2026-05-12-wxsp-design.md)。

---

## 系统要求

- **Python 3.10+**(由 `uv` 管理)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — 包管理器
- **Chromium**(由 `patchright install chromium` 自动装,带反检测补丁)
- **NAS 挂载**(可选,把视频/封面文件放本地路径也行)
- **飞书企业账号** + 已建好 Bitable 多维表格(字段见 CLAUDE.md "飞书 Bitable 表结构")
- **企微机器人 webhook**(可选,告警渠道)

---

## 安装

```bash
# 1. 克隆
git clone <repo-url> wechat-sph-upload
cd wechat-sph-upload

# 2. 创建虚拟环境 + 安装依赖
uv sync

# 3. 装 Chromium(patchright 加固版)
uv run patchright install chromium

# 4. 配置
cp config.example.yaml config.yaml
# 编辑 config.yaml:填飞书 app_id / bitable / 账号 / NAS 路径

# 5. 设置环境变量(secret 不进 yaml)
export FEISHU_APP_SECRET='cli_xxx'      # mac/Linux
export WECOM_BOT_WEBHOOK='https://...'
# Windows PowerShell:  $env:FEISHU_APP_SECRET="cli_xxx"

# 6. 健康检查
uv run wxsp doctor
# 应输出: 配置 ✓ / DB ✓ / NAS ✓ / 飞书 API ✓
```

---

## 首次使用

```bash
# 1. 每个账号扫码登录(弹出浏览器,扫码后自动保存 cookie)
uv run wxsp login account_a
uv run wxsp login account_b
# ... 每个账号都要扫一次

# 2. 拉飞书数据(把"待入库"行同步进本地 DB)
uv run wxsp sync

# 3. 跑今天的任务
uv run wxsp run --today

# 或: 起 Web UI 看板 (http://127.0.0.1:8765)
uv run wxsp web

# 或: 起 daemon (每天 09:00 自动跑) ⭐ 生产用
uv run wxsp run --daemon
```

> ⚠️ **视频号必须 `headless=false`**,扫码和发布都会弹出浏览器窗口。不要在无桌面环境运行。

---

## 开机自启

### macOS (launchd)

1. **替换占位符**:打开 [deploy/wxsp.plist](deploy/wxsp.plist),把:
   - `__INSTALL_DIR__` → 项目绝对路径(运行 `pwd` 得到)
   - `__UV_BIN__` → uv 绝对路径(运行 `which uv` 得到,通常 `/opt/homebrew/bin/uv` 或 `/Users/you/.local/bin/uv`)

2. **安装 + 启动**:
   ```bash
   cp deploy/wxsp.plist ~/Library/LaunchAgents/com.wxsp.daemon.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wxsp.daemon.plist
   launchctl kickstart -k gui/$(id -u)/com.wxsp.daemon   # 立即起一次
   ```

3. **查看状态 / 日志**:
   ```bash
   launchctl print gui/$(id -u)/com.wxsp.daemon | head -20
   tail -f logs/launchd.err.log logs/wxsp.*.log
   ```

4. **卸载**:
   ```bash
   launchctl bootout gui/$(id -u)/com.wxsp.daemon
   rm ~/Library/LaunchAgents/com.wxsp.daemon.plist
   ```

> 用 **LaunchAgent** (`gui/`) 不要用 LaunchDaemon (`system/`):后者跑在 root + Session 0,没有桌面会话,视频号浏览器开不起来。

### Windows (任务计划程序)

1. **替换占位符**:打开 [deploy/wxsp-task.xml](deploy/wxsp-task.xml),把:
   - `__INSTALL_DIR__` → 项目绝对路径(如 `C:\Users\you\wechat-sph-upload`)
   - `__UV_BIN__` → uv 路径(运行 `where uv` 得到,通常 `C:\Users\you\.local\bin\uv.exe`)
   - `__USERNAME__` → 登录用户名(运行 `echo %USERNAME%` 得到)

2. **注册任务**:
   ```cmd
   schtasks /Create /TN "wxsp-daemon" /XML deploy\wxsp-task.xml
   ```

   注册时报"参数错误"通常是 XML 编码问题。Win10/11 多数情况下接受 UTF-8,如果失败转 UTF-16 LE 再试:
   ```powershell
   Get-Content deploy\wxsp-task.xml | Out-File -FilePath deploy\wxsp-task-utf16.xml -Encoding Unicode
   schtasks /Create /TN "wxsp-daemon" /XML deploy\wxsp-task-utf16.xml
   ```

3. **手动启动一次验证**:
   ```cmd
   schtasks /Run /TN "wxsp-daemon"
   schtasks /Query /TN "wxsp-daemon"
   ```

4. **卸载**:
   ```cmd
   schtasks /Delete /TN "wxsp-daemon" /F
   ```

> 触发器是 **LogonTrigger**(用户登录后 30 秒触发),不是开机时(BootTrigger)。原因同 mac:视频号要桌面会话,Session 0 没 UI。

---

## 常用命令

```bash
# 账号
uv run wxsp login <account_id>        # 扫码登录 / 刷新 cookie
uv run wxsp accounts list             # 看账号 + cookie 状态
uv run wxsp accounts pause <id>       # 暂停账号(风控时手动止血)
uv run wxsp accounts resume <id>

# 飞书同步
uv run wxsp sync                      # 立即拉一次飞书(不跑任务)

# 执行
uv run wxsp run --daemon              # 起 cron daemon
uv run wxsp run --today               # 跑今天所有 pending
uv run wxsp run --task-id 42          # 跑单条
uv run wxsp run --task-id 42 --dry-run  # 跑到"发布"按钮前停下(验证用)

# 健康检查
uv run wxsp doctor                    # 配置 / DB / 账号 cookie / NAS / 飞书 API 全检一遍

# 清理(归档)
uv run wxsp cleanup                   # 按 monitoring.{log,screenshot}_retention_days 清过期文件

# 查看
uv run wxsp status                    # 今日发布状态汇总
uv run wxsp logs --follow             # tail -f 风格看日志
uv run wxsp web                       # 起 Web UI
```

完整命令清单见 `uv run wxsp --help`,Web UI 默认 http://127.0.0.1:8765/。

---

## 故障排查

| 现象 | 排查 |
|------|------|
| `wxsp doctor` 报 NAS 不可达 | `ls $(yq '.paths.nas_root' config.yaml)` 看挂载是否还在 |
| Cookie 失效循环 | `wxsp login <account>` 重新扫码,Web UI 也能扫 |
| 浏览器弹不出来(开机自启) | 检查是不是用了 LaunchDaemon(mac)/BootTrigger(Windows);必须是用户会话级 |
| 视频号"操作过于频繁" | `risk_control` 错误,账号会自动暂停 24h;企微会收到告警;别强行 retry |
| daemon 不跑 | 看 `logs/wxsp.YYYY-MM-DD.log`(M9 起所有 daemon/web 进程都写文件日志) |
| 历史积压一直涨 | Web UI Dashboard 顶上 "积压 N 条" 可点,跳到 `/tasks?backlog=1` 一键"重新入队到今天" |

更细的错误分类与重试策略见 CLAUDE.md "错误分类与重试策略"。

---

## 项目结构

```
wxsp/                     # 后端主包
├── cli.py                # Typer CLI 入口
├── config.py             # Pydantic Settings
├── db.py / models.py     # SQLModel(Account/Video/Task/Event)
├── feishu.py             # Bitable 同步 + 回写
├── validator.py          # 入库校验
├── scheduler.py          # 09:00 cron + 手动 fire
├── publisher.py          # 视频号发布核心 (patchright)
├── selectors.py          # 视频号改版唯一改动点
├── browser.py + stealth_js.py  # 反检测
├── notify.py             # 企微告警
├── doctor.py             # 健康检查
├── nas.py                # NAS 文件检索 + stage
├── archive.py            # 日志/截图清理 + loguru 文件 sink (M9)
└── api/ + templates/     # FastAPI + Jinja2 + HTMX Web UI

deploy/
├── wxsp.plist            # macOS launchd 模板
└── wxsp-task.xml         # Windows 任务计划程序模板

data/   chrome-profiles/ + db.sqlite + tmp/    (gitignore)
logs/   wxsp.YYYY-MM-DD.log + screenshots/     (gitignore)
```

---

## 开发

```bash
uv sync                          # 装依赖(含 dev)
uv run pre-commit install        # ruff + mypy + 测试 hook
uv run pytest                    # 跑全量测试
uv run pytest -m "not integration"  # 只跑单元测试(不点真页面)
```

提交规范用 **Conventional Commits**(`feat:` / `fix:` / `chore:` ...)。pre-commit 会拦下 ruff/mypy 不过的代码,**禁止 `--no-verify` 绕过**。

---

## 安全

- 不存储账号密码,只存 cookie(由 patchright 的 user_data_dir 持久化)
- 飞书 app_secret / 企微 webhook 用环境变量,**不要进 yaml**
- `data/` 和 `logs/` 全 gitignore,不要上传到任何第三方
- 视频号风控敏感:`max_concurrent_accounts=1`(同 IP 多账号并发 = 风险),单账号最小发布间隔 ≥ 30 分钟
