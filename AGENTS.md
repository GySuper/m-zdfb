# AGENTS.md — wxsp 多平台自动发布工具

> [CLAUDE.md](CLAUDE.md) 是详细设计与操作手册,本文件是给 agent 的速读版。涉及平台列表、命令和运行时行为时,以当前源码与测试为准。

## 项目简介

多平台短视频自动发布工具:**飞书 Bitable 是唯一任务源**,各平台按自身配置注册每日 cron(默认 09:00),Web UI 默认监听 `127.0.0.1:8765` 做运维控制台。支持 6 个平台:`tencent_channel`(视频号)/`douyin`/`kuaishou`/`xiaohongshu`/`taobao_guanghe`/`pinduoduo`。macOS + Windows 单机部署,Python 3.10+。

## 常用命令

```bash
uv sync                                    # 装依赖(含 dev)
uv run pytest                              # 全量测试
uv run pytest -m "not integration"         # 单元测试(不点真页面;CI 跑这个)
uv run pytest tests/test_douyin_platform.py # 跑单个平台/模块
uv run ruff check . && uv run ruff format . # lint + 格式
uv run mypy wxsp                           # 类型检查(strict)
uv run wxsp doctor                         # 健康检查(配置/DB/cookie/NAS/飞书)
uv run wxsp run --task-id 42 --dry-run     # 单条 dry-run(验证发布步骤,不会点发布)
uv run wxsp run --daemon                  # 启动 Web UI + 各平台 cron(打包模式)
uv run wxsp web                            # 起 Web UI
```

- pre-commit(ruff + ruff-format + mypy)已装 hook,**禁止 `--no-verify` 绕过**。
- `@pytest.mark.integration` 标记的测试需要真浏览器 + 真账号,CI 跳过、手动跑。
- `wxsp status` 和 `wxsp logs` 当前仍是 CLI 占位命令;任务/日志查看与操作走 Web UI。

## 架构分层(改代码前必看)

依赖方向:**`base`(类型)← `runner`(编排)← 各平台 adapter ← `publisher`(路由)**。

| 层 | 文件 | 职责 | 硬约束 |
|---|---|---|---|
| 身份元数据 | `wxsp/platform_meta.py` `REGISTRY` | 平台中文名/标题与标签限制/login 检测/向导字段/指纹与浏览器档位 | **纯数据,禁止 import 任何 wxsp 模块**(防循环依赖)。config/notify/browser/validator/setup 都读它。 |
| 共享类型 | `wxsp/platforms/base.py` | `PlatformPublisher` Protocol / `TaskBundle` / `PublishContext` / `PlatformSpec` | 只放类型,无业务逻辑。 |
| 共享编排 | `wxsp/platforms/runner.py` `run_publish()` | claim 抢锁 → 启浏览器 → 步骤回调 → 状态机 → 通知 → 飞书回写 | 所有"plumbing"只此一处,**平台 adapter 不要复制状态机**(历史 tencent/taobao 各抄一份已产生漂移)。 |
| 平台 adapter | `wxsp/platforms/X.py` + `X_selectors.py` | 浏览器交互:`_pre_publish` / `_post_publish` / `login` | **只做浏览器交互**。DB 写、通知、飞书回写都由 runner 统一处理。 |
| 路由 | `wxsp/publisher.py` `_PUBLISHERS` | 按 `task.platform` 分发到 adapter;`_PUBLISH_LOCK` 保证进程内单 worker | 加平台在此注册一行 `"X": XPublisher()`。 |

### 新增平台(5 步操作手册,详见 CLAUDE.md §新增一个平台)

1. `platforms/X_selectors.py`:URL + 登录判定 + 各步元素 + `RISK_CONTROL_KEYWORDS`/`SUCCESS_INDICATORS`。优先语义化选择器(`text=`/`role=`/`placeholder=`)。
2. `platforms/X.py`:`_pre_publish`(开页→上传→填表→风控探测,**止于点发布前**)、`_post_publish`(点发布→等成功)、`X_SPEC = PlatformSpec(...)`、`class XPublisher`(`publish_one` 转调 `run_publish`、`login` 开浏览器等扫码且**不接 Settings**)。
3. `publisher._PUBLISHERS` 注册一行。
4. `platform_meta.REGISTRY` 加一条 `PlatformMeta`。
5. `wxsp web` 在没有平台配置时进入 `/setup` 五页向导;完成后写 `config_X.yaml`,并为其余已登记平台补空壳。

> 当前没有 `wxsp setup` CLI 命令;首次配置由 `wxsp web` → `/setup` 接管。config / notify / browser / validator / setup / cli **都从 REGISTRY 读**,新增平台不需要再维护重复平台表。

### 加平台时容易漏的多处注册

新错误类型必须三处全到,否则告警显示"未知错误":`errors.py`(继承 `PublisherError`)→ `errors._KIND_BY_TYPE` → `notify._ERROR_TYPE_CN`。
新步骤名 `ctx.last_step` 必须加进 `notify._STEP_CN`,否则告警步骤名漏成英文。

## 硬约束(违反会出事)

- **dry-run 红线**:点发布的动作只能写在 `post_publish`;`pre_publish` 之后是 dry-run gate,`--dry-run` 在那里截断。写错位置会让 dry-run 真发出去。
- **headless 约束**:`publisher.headless` 默认 `false`;`runner` 明确拒绝视频号(`tencent_channel`)使用 headless,登录和发布必须可见。其他平台发布是否 headless 由配置决定,登录入口均显式使用可见浏览器。
- **指纹透传**:只有 `platform_meta.needs_fingerprint=True` 的平台(当前仅视频号)注入 per-account 指纹;`publisher`/`login`/`doctor`/`browser_context` 必须传 `account_id`,否则会退化到无指纹模式。
- **DB 幂等/状态**:`claim_task` 是 pending → running 的原子抢锁点;`run_publish()` 统一收尾状态、通知和飞书回写。平台 adapter 不复制状态机;可复用字段更新使用 `db.transition_task(...)`,重试/同步路由的专用回退逻辑除外。
- **Cookie 持久化**:每账号独立 `user_data_dir`;`needs_fingerprint=True`(当前视频号)依赖 persistent context profile,其余平台(包括小红书真实 Chrome/CDP)通过账号目录下 `cookies.json` 显式读写。
- **小红书浏览器模式**:`platform_meta.use_real_chrome=True`,发布/登录通过 CDP 启动本机真实 Chrome;目标机器必须安装 Google Chrome。

## 编码约定

- 日志统一 `from loguru import logger`,**业务代码禁用 `print`**。
- 路径走 `pathlib.Path`,**禁止字符串拼 `/`**(跨平台保证)。
- 所有 IO/网络/页面操作必须有超时,**不要写无限等待**。
- 外部输入(yaml、飞书数据、API 请求)用 Pydantic 校验。
- API 路由薄:`api/routes_X.py` 调 `wxsp/X.py` 同名模块函数。
- commit message 用 Conventional Commits(`feat:`/`fix:`/`chore:` ...)。
- Windows 中文版 stdout 默认 cp936,CLI 入口 `_force_utf8_stdout()` 已处理;新增含 emoji 的输出不要绕过它。

## 平台特定硬约束

- **拼多多入口弹窗**:登录验证后、上传前执行 `entry_popups`。只允许点击可见弹窗关闭控件或按 `Escape`,包含无 `role=dialog` 的独立 Beast 关闭 SVG;最多关闭 5 个,约 10 秒内无法安全收敛就失败,不要点击「确定/参与」等业务按钮。
- **拼多多标签**:每个标签严格按 `#标签` → 等待 1.5 秒 → 按 `Space`,再处理下一个;不要擅自改回 Enter/候选项点击。

## 安全 / 禁止

- ❌ 不存储账号密码,只存 cookie;❌ 不把视频或 cookie 上传第三方
- ❌ 飞书 `app_secret` / 企微 webhook 用环境变量(`FEISHU_APP_SECRET`/`WECOM_BOT_WEBHOOK`),**不进 yaml**
- ❌ `data/` 和 `logs/` 全 gitignore,不要提交
- ❌ 不硬编码账号/Token;❌ 不静默忽略 `risk_control` 类错误

## Web UI 约定(FastAPI + Jinja2 + HTMX)

- HTMX 同步操作失败时:路由返 `200` + 失败片段 + 响应头 `HX-Trigger: {"opError": {...}}`,由 `base.html` 监听器弹全局 modal;**不走 4xx/5xx**(避免 htmx 默认 `responseError` 路径漏 swap)。
- 同步阻塞型路由(sync / run-today)用 `threading.Lock` 串行,抢不到锁返"正在跑中"。
- SSE `/api/logs/stream` 做日志流;任务状态通过页面/HTMX 刷新,当前没有 `/api/tasks/stream`。
- 平台切换:`?platform=` 查询参数过滤;`platform_context` 中间件对无参 GET 重定向到默认平台(`data/default_platform` 存全局默认)。
- Web UI 仅允许监听回环地址,并对 POST/PUT/PATCH/DELETE 做同源校验;`TrustedHostMiddleware` 只放行本机 host(测试用 `testserver`)。

## 配置

- 每平台独立 `config_{platform}.yaml`;`load_settings(platform=...)` / `get_config_path(platform)`。
- Settings 模型扁平(无平台嵌套);模板见 `config.example.yaml`。
- 敏感字段用 `${ENV_VAR}` 引用。
- 开发模式配置位于项目根;打包模式使用用户数据目录。旧版 `config.yaml` 会自动拆分为平台配置并备份为 `.yaml.bak`。
- 默认 SQLite 位于用户数据目录的 `db.sqlite`(可用 `WXSP_DB_PATH` 覆盖),`init_db()` 幂等建表并补齐旧库字段。

## 打包与发布

- macOS:`scripts/build_macos.sh` 用 PyInstaller 生成 `.app`,嵌入 patchright Chromium 后产出 DMG;Windows:`scripts/build_windows.ps1` 生成 onedir bundle,嵌入 Chromium 后用 Inno Setup 产出安装器。
- `.github/workflows/build.yml` 在 `v*` tag 或手动触发时构建 macOS/Windows;tag 构建完成后自动创建 GitHub Release。脚本从 `APC_*` 环境变量注入打包凭据,退出时恢复源码占位符。
- 发布版本号来自 `wxsp/__init__.py` 与 tag 传入的 `WXSP_VERSION`;不要把 `dist/`、`build/`、`data/`、`logs/` 或 secrets 提交到仓库。

## 关键文档(改敏感区前先读)

- [CLAUDE.md](CLAUDE.md) — 详细设计参考:核心约束、发布步骤、错误分类与重试、告警规则(如与源码冲突,以源码/测试为准)
- [docs/superpowers/specs/2026-05-12-wxsp-design.md](docs/superpowers/specs/2026-05-12-wxsp-design.md) — 总体设计 + 12 个 milestone 验收标准
- [docs/superpowers/specs/](docs/superpowers/specs/) — 各平台接入设计文档(改某平台先读对应的)
- [docs/desktop-packaging.md](docs/desktop-packaging.md) — macOS/Windows 打包
