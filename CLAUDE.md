# CLAUDE.md — 多平台自动发布工具 (wxsp)

> 本文件给 Claude Code 阅读,用于指导项目从零开发。**请逐节通读后再开始编码**,
> 特别留意 "核心约束"、"复用与参考"、"平台架构"。

**当前支持平台**:视频号(tencent_channel)、淘宝光合(taobao_guanghe)

---

## 编码行为准则(每次写代码前必读)

本项目所有编码、重构、Code Review 工作,**Claude Code 必须先调用 `andrej-karpathy-skills:karpathy-guidelines` skill**,按其指引执行。该 skill 是顶层行为约束,优先级高于本文件其他章节的技术细节(冲突时以 skill 为准)。

核心要点速览(完整内容以 skill 实时载入为准):

1. **想清楚再写(Think Before Coding)**:不要假设、不要藏疑惑。多解释时先列出选项再让用户选,不要静默挑一个。有更简单的方案要主动说。
2. **简洁优先(Simplicity First)**:解决问题的最少代码。不写没要求的功能,不写一次性代码的抽象,不为不可能的场景做防御。200 行能压到 50 行就压。
3. **外科手术式改动(Surgical Changes)**:只动该动的地方,不顺手"优化"周边代码、注释、格式。保持原有风格。发现无关死代码先提出,不擅自删。
4. **目标驱动执行(Goal-Driven Execution)**:把任务翻译成可验证的成功标准(例:"加校验"→"先写非法输入的测试,再让测试通过")。多步任务先列计划 + 每步的验证方式,再开工。

> ⚠️ 当 skill 中的指引与本文件的某条具体建议冲突时,以 skill 为准;若仍不确定,停下来问用户。

---

## 项目目标

为运营方提供一个**稳定、可观测、可恢复**的多平台自动化发布工具,本地运行,带 Web UI。每个平台独立配置、独立调度、独立通知,互不干扰。

**规模需求**:
- 多平台多账号(数量可扩展,配置驱动),每账号独立 Chrome profile + 设备指纹
- 每天约 80 条视频(每账号约 20 条,可配置)
- 视频文件存储在 NAS,通过本地挂载路径访问
- 任务元数据**单一来源:飞书多维表格(Bitable)**,每平台独立飞书表;Web UI 作为运维控制台只做查看/重试/扫码/告警/配置,**不**用于创建任务
- 全自动调度发布;人工只负责准备视频、维护飞书表、处理异常
- **跨平台:macOS + Windows 都要支持**(单机部署)

**非目标**:
- 不做内容生成(标题/描述/标签由用户提供)
- 不做数据采集和分析
- 不做用户系统、登录系统(本地单用户)

---

## 复用与参考(开始编码前请先读这两个项目)

在动手前,Claude Code 必须先克隆并阅读这两个仓库的关键文件:

```bash
git clone https://github.com/dreammis/social-auto-upload  ../_ref/social-auto-upload
git clone https://github.com/jackwener/OpenCLI            ../_ref/OpenCLI
```

### 必读文件

**social-auto-upload(主要复用源)**:
- `uploader/tencent/main.py` — 视频号发布流程的实现,我们的 `publisher/tencent_channel.py` 要基于这个改写
- `examples/get_tencent_cookie.py` — 扫码登录逻辑,几乎可以直接采用
- `uploader/tencent/` 下的其他文件 — 看看他们怎么处理上传进度、定时发布、合集

**OpenCLI(辅助参考)**:
- `src/`(任意一个网站 adapter)— 看 stealth 注入的 JS 代码
- 它的 doctor 健康检查命令的输出风格

### 复用规则

| 复用项 | 怎么处理 |
|--------|----------|
| 视频号发布的 Playwright 选择器和流程 | 借鉴 social-auto-upload,在我们的架构里**重写**(它是脚本式 + 原版 Playwright,我们要模块化 + patchright),保留它的"踩坑成果"(选择器选择、等待策略) |
| 扫码登录二维码捕获 | 可以**几乎照抄** social-auto-upload 的实现 |
| 反检测 init script | patchright 内建 CDP 修补 + `wxsp/fingerprint.py` 的 **per-account 指纹注入**(UA / WebGL / Canvas / Audio / Client Hints 等,绕过视频号"同设备多账号"风控)。`wxsp/stealth_js.py` 是上一代静态 init script,保留供回滚 |
| Exit code 规范 | 借鉴 OpenCLI 的 sysexits.h 命名 |

**不要**直接 copy 整个 social-auto-upload 项目:它的代码风格、目录结构、Flask backend、Vue frontend 都和我们不同,生搬硬套会带来历史负担。

---

## 核心约束(必须严格遵守)

### 1. 视频号风控敏感
- 单账号最小发布间隔 ≥ 30 分钟(可配置)
- **严禁** headless 模式跑视频号,默认 `headless=false`
- 每次操作之间要有 1-3 秒随机停顿(模拟人工)
- 出现 "请稍后"、"系统繁忙"、"操作过于频繁"、"账号异常" 等文案 → 立即停止该账号当天所有任务,标记 `risk_control` 错误,推送告警

### 2. Cookie 寿命短(实际 3-7 天)
- 每次发布前要做 Cookie 健康检查(打开账号主页验证登录态)
- Cookie idle 太久(距上次 `cookie_last_active_at` > `monitoring.cookie_warn_days`,默认 1.5 天)即使本次能登录也标 `warn` 状态 + 推 `cookie_warning` 告警(提醒主动续命,避免下次真过期才发现)
- Cookie 失效(本次登录就失败)→ 标 `expired`,**不重试**,等待 `wxsp login` 扫码
- 健康指标用 `cookie_last_active_at`(上次成功打开账号主页时间),不是"导入时间"

### 2.5 同 IP 多账号并发是风控特征
- 默认 `max_concurrent_accounts = 1`,**单 worker 串行执行**
- 同一时刻同一台机器只跑一个视频号账号的浏览器

### 3. 视频号定时发布平台限制
- 定时发布最早 = 当前时间 + 30 分钟(以页面校验为准,不要硬编码)
- 定时发布最晚 ≤ 14 天后
- 实现时不要 hard-code,做成可校验可配置

### 4. 失败必须可恢复
- 所有任务状态进 SQLite,断点续传
- 错误必须分类(详见 "错误分类与重试策略")
- 每次失败要保存截图到 `logs/screenshots/<task_id>_<step>.png`
- 永远不要静默吞掉异常

### 5. NAS 访问的特殊性
- 视频/封面路径完全可配置,支持 NAS 挂载路径(如 `/Volumes/NAS/videos/`);**每账号独立** `video_search_root` / `cover_search_root`,可用 `{nas_root}` 占位
- 飞书 `视频文件` / `封面文件` 字段**两种填法并行**:**裸文件名**(在账号 `video_search_root` 下递归搜)或**完整 NAS 路径**(UNC `\\host\share\...` / 盘符 `X:\...` / POSIX `/Volumes/...`)。判定走 `nas._is_absolute_path_input`
- 跨 OS 部署时由 `paths.path_aliases: {UNC前缀: POSIX前缀, ...}` 翻译 —— macOS 上把 key→value,Windows 上把 value→key;默认 `{}` = 禁用(老配置兼容)。doctor 顺带核对当前 OS 那侧的 alias 目标盘是否挂上
- 启动时 `doctor` 必须检查每个账号配置的路径是否可访问
- 读 NAS 文件时要捕获网络错误,作为 `nas_unreachable` 错误类型
- `stage_to_tmp` 优先 symlink(零拷贝);Windows 默认账户没 `SeCreateSymbolicLinkPrivilege` 时 fallback 到 `shutil.copy2`(M10 修)
- 不做本地缓存(假设千兆 LAN,直接读),但保留接口便于后期加缓存

---

## 技术栈

| 类别 | 选型 | 理由 |
|------|------|------|
| 后端语言 | Python 3.10+ | 可参考 social-auto-upload 代码 |
| 浏览器自动化 | **patchright** (Playwright fork) | 比原版深度修补 CDP 指纹泄漏,API 完全兼容;视频号风控敏感时表现更好 |
| CLI | Typer | 类型友好 |
| Web 框架 | FastAPI | async,自动 OpenAPI,SSE 友好 |
| 前端 | **Jinja2 + HTMX** | 单进程、纯 Python、SSE/局部刷新天然支持;运维控制台不需要 SaaS 级前端 |
| 配置 | YAML (PyYAML) + Pydantic Settings | 类型校验;**两层覆盖**(config.yaml + 环境变量) |
| 数据库 | SQLite + **SQLModel** | 模型即 schema 即 Pydantic 校验;底层兼容 SQLAlchemy,后期可平滑升级 |
| 日志 | loguru | 简单好用 |
| 反检测 | **patchright 内建** + `wxsp/fingerprint.py` per-account 指纹注入 | 每个账号一套确定性指纹(种子 = MD5(account_id)),绕过视频号"同设备多账号"风控 |
| 飞书 SDK | `lark-oapi`(官方 Python SDK) | 多维表格读写 |
| 任务调度 | APScheduler | **只用一个 cron job(每天 09:00)**,无 polling |
| 包管理 | uv | 跨平台一致;比 pip 快 |
| 代码质量 | ruff + mypy + pre-commit | 强制开启 |

---

## 平台架构(多平台 adapter 模式)

### PlatformPublisher 协议

`wxsp/platforms/base.py` 定义 `PlatformPublisher` 协议。每个平台实现 `publish_one()` + `login()`,只管浏览器交互,不碰 DB/通知/飞书回写。

```
wxsp/platforms/
├── __init__.py
├── base.py                  # PublishResult + PlatformPublisher 协议
├── tencent_channel.py       # 视频号发布
├── tencent_selectors.py     # 视频号选择器
├── taobao_guanghe.py        # 淘宝光合发布
└── taobao_selectors.py      # 淘宝光合选择器
```

`publisher.py` 是薄路由层,根据 `task.platform` 调对应的 `PlatformPublisher.publish_one()`。

### 配置文件:每平台独立

每个平台一个独立配置文件:`config_{platform}.yaml`(如 `config_tencent_channel.yaml` / `config_taobao_guanghe.yaml`)。Settings 模型是扁平的(app + paths + accounts + scheduler + publisher + feishu + monitoring + webui),没有平台嵌套。

`get_config_path(platform)` 返回对应文件路径。`load_settings(platform=...)` 加载。

### 数据隔离

- `Account.platform` / `Task.platform` / `Event.platform` 标识所属平台
- Dashboard / Tasks / Accounts / Plans 路由按 `?platform=` 查询参数过滤
- 中间件 `platform_context` 发现可用平台,对 GET 请求无 `?platform=` 时自动重定向到默认平台;对 POST 请求从 Referer 头解析当前平台
- `get_settings(request)` 依赖从 `request.state.current_platform` 读取平台,路由中写 `settings: Settings = Depends(get_settings)` 即可拿到当前平台配置
- 默认平台存储在 `data/default_platform`(共享文件,全局设置,非 per-platform 配置)

### Web UI 平台切换

Sidebar 左上角 "当前平台" 下拉动态列出所有平台,切换后所有 nav 链接带 `?platform=`,各页面数据完全隔离。

---

## 目录结构(扁平,单文件单职责;超 500 行再拆模块)

```
wxsp/
├── wxsp/                              # 后端主包
│   ├── __init__.py
│   ├── cli.py                         # Typer CLI 入口
│   ├── config.py                      # Pydantic Settings (per-platform config_{platform}.yaml)
│   ├── db.py                          # SQLModel engine + session + 状态转换辅助 + 幂等锁
│   ├── models.py                      # SQLModel 表 (Account/Video/Task/Event),含 platform 字段
│   ├── feishu.py                      # Bitable 拉取(sync_now) + 回写
│   ├── validator.py                   # 入库校验(纯函数,按平台区分字段)
│   ├── scheduler.py                   # 每平台独立 cron + 手动 fire (APScheduler 包装)
│   ├── publisher.py                   # 薄路由层,根据 task.platform 路由到对应 platform
│   ├── platforms/                     # 平台 adapter 实现
│   │   ├── base.py                    #   PublishResult + PlatformPublisher 协议
│   │   ├── tencent_channel.py         #   视频号发布核心
│   │   ├── tencent_selectors.py       #   视频号选择器
│   │   ├── taobao_guanghe.py          #   淘宝光合发布核心
│   │   └── taobao_selectors.py        #   淘宝光合选择器
│   ├── browser.py                     # patchright context 工厂 + per-account 指纹注入
│   ├── fingerprint.py                 # per-account 设备指纹生成 + JSON 持久化 + JS init script
│   ├── stealth_js.py                  # 上一代静态 init script(已被 fingerprint 取代,保留回滚)
│   ├── errors.py                      # 错误类型 + 分类(含 taobao 特有:ProductNotFound/TopicNotFound)
│   ├── notify.py                      # Notifier 协议 + WecomNotifier(platform 感知)
│   ├── doctor.py                      # 健康检查
│   ├── nas.py                         # find_video / find_cover / stage_to_tmp / cleanup_tmp
│   ├── retry.py                       # 重试装饰器 / 指数退避
│   ├── api/                           # FastAPI 路由层
│   │   ├── app.py                     # FastAPI 入口 + platform_context 中间件
│   │   ├── deps.py
│   │   ├── routes_dashboard.py
│   │   ├── routes_accounts.py
│   │   ├── routes_tasks.py
│   │   ├── routes_plans.py
│   │   ├── routes_config.py
│   │   └── routes_logs.py             # SSE
│   └── templates/                     # Jinja2 模板
│       ├── base.html                  #   含动态平台切换器
│       ├── dashboard.html
│       ├── accounts.html
│       ├── tasks.html
│       ├── task_detail.html
│       ├── plans.html
│       ├── config.html
│       └── logs.html
├── data/
│   ├── db.sqlite
│   ├── default_platform               # 全局默认平台设置
│   ├── chrome-profiles/               # 每账号独立 user_data_dir(cookie 由此持久化)
│   │   ├── account_a/
│   │   ├── taobao_a1/
│   │   └── ...
│   ├── fingerprints/                  # per-account 指纹 JSON(种子 = MD5(account_id),丢失能从种子重建)
│   │   ├── account_a.json
│   │   └── ...
│   └── tmp/                           # 视频 stage 目录(NAS → 本地),发完清理
├── logs/
│   ├── wxsp.{YYYY-MM-DD}.log          # 按天滚动 + 压缩
│   └── screenshots/
│       └── {YYYYMM}/
│           └── {task_id}_{step}.png
├── docs/
│   └── superpowers/specs/             # 设计文档(brainstorm 产出)
├── tests/
├── config.yaml                        # 用户配置(gitignore)
├── config.example.yaml                # 配置模板
├── pyproject.toml
├── .pre-commit-config.yaml
└── README.md
```

---

## 配置系统(每平台独立文件)

### 配置加载

每个平台一个独立配置文件:`config_{platform}.yaml`(如 `config_tencent_channel.yaml` / `config_taobao_guanghe.yaml`)。`get_config_path(platform)` 返回对应路径,`load_settings(platform=...)` 加载。

向后兼容:旧的 `config.yaml` 在首次访问时会自动迁移拆分为平台文件。

环境变量用 `${ENV_VAR}` 语法在 yaml 里引用(如 `app_secret: ${FEISHU_APP_SECRET}`)。实现用 Pydantic Settings,所有字段强类型校验。

### config.yaml 完整示例

```yaml
# ============== 全局 ==============
app:
  data_dir: ./data
  logs_dir: ./logs
  timezone: Asia/Shanghai

# ============== 路径(NAS 友好,跨平台) ==============
# 只配 NAS 挂载根目录;每个账号的 video / cover 检索路径在 accounts 下各自配。
# Windows 示例: nas_root: "Z:/wxsp" 或 "\\\\server\\share\\wxsp"
paths:
  nas_root: /Volumes/NAS/wxsp
  # 跨 OS 路径前缀映射(可选)。运营常从 Windows 复制完整 UNC 路径粘进飞书"视频文件",
  # daemon 跑在 macOS 时需要把 UNC 前缀翻成本地 mount 才能读到。
  # macOS 上 key→value,Windows 上 value→key;同 OS 部署可留空 {} 等于禁用。
  path_aliases: {}
  # 示例:
  # path_aliases:
  #   "\\\\172.31.15.11\\dianshang": "/Volumes/dianshang"
  #   "\\\\nas.local\\wxsp":         "/Volumes/wxsp"

# ============== 账号 ==============
# 每账号都要配 video_search_root / cover_search_root,可用 {nas_root} 占位。
# user_data_dir 是独立 Chrome profile(cookie 由 persistent context 持久化,无 cookie.json)。
accounts:
  account_a:
    display_name: "美食号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_a
    video_search_root: "{nas_root}/videos/account_a"
    cover_search_root: "{nas_root}/covers/account_a"
  account_b:
    display_name: "健身号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_b
    video_search_root: "{nas_root}/videos/account_b"
    cover_search_root: "{nas_root}/covers/account_b"
  account_c:
    display_name: "旅游号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_c
    video_search_root: "{nas_root}/videos/account_c"
    cover_search_root: "{nas_root}/covers/account_c"
  account_d:
    display_name: "搞笑号"
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_d
    video_search_root: "{nas_root}/videos/account_d"
    cover_search_root: "{nas_root}/covers/account_d"

# ============== 调度 ==============
scheduler:
  daily_cron_hour: 9                  # 每天 09:00 自动触发跑当日任务
  daily_cron_minute: 0
  strategy: round-robin               # 保留字段,当前未使用(账号必填,不做自动分配)

# ============== 发布器 ==============
publisher:
  headless: false                     # 视频号必须 false
  upload_timeout_seconds: 600
  step_pause_seconds: [1, 3]          # 步骤间随机停顿范围
  screenshot_on_error: true
  max_concurrent_accounts: 1          # 单 worker 串行,避免同 IP 多账号并发风控

# ============== 飞书集成 ==============
feishu:
  enabled: true
  app_id: cli_xxxxxxxxxx
  app_secret: ${FEISHU_APP_SECRET}
  bitable:
    app_token: xxxxxxxxxxxx
    table_id: tblxxxxxxxx
  # 字段映射(运营自定义的中文字段名 → 内部字段)
  field_map:
    video_file: "视频文件"
    title: "标题"
    description: "描述"
    tags: "标签"
    cover: "封面文件"
    topic: "合集"
    original_claim: "原创"
    account: "账号"
    execute_date: "执行日期"          # 新增:必填,daemon 只跑 execute_date=today
    publish_at: "定时发布时间"        # 改名+必填:视频号页面填的发布时刻
    status: "状态"                    # 工具回写
    remote_url: "已发布链接"          # 工具回写
    error_message: "错误信息"         # 工具回写
  sync:
    # 不再有 pull_interval_seconds: 飞书 sync 改成按需调用(worker 入口前自动 + 手动按钮)
    write_back_enabled: true

# ============== 告警 + 归档(M9) ==============
monitoring:
  cookie_warn_days: 1.5               # 距上次 active_at 超过此天数仍能登录 → status=warn 触发 cookie_warning
  notifiers:
    wecom:                            # 第一版只做企微;其他实现接口预留
      enabled: true
      webhook: ${WECOM_BOT_WEBHOOK}
  notify_on:
    - cookie_expired
    - cookie_warning
    - risk_control
    - task_failed
    - element_not_found               # 可能改版,需关注
    - nas_unreachable
    - backlog_high                    # 历史积压(execute_date<today + pending/interrupted)超阈值
  log_retention_days: 30              # 日志按天滚动,过期清除(daemon 启动时 cleanup)
  screenshot_retention_days: 90       # 错误截图保留期
  backlog_warn_threshold: 20          # 历史积压超此数触发 backlog_high 告警

# ============== Web UI ==============
webui:
  host: 127.0.0.1
  port: 8765
  open_browser_on_start: true
```

### 配置 UI 化

Web UI 的 "配置" 页能编辑 `config.yaml`(高级用户)或者用表单逐项编辑,保存后写回,**敏感字段(secret/webhook)做掩码显示**。

---

## 内容来源:飞书 Bitable(唯一)

SQLite 是唯一的运行时事实来源,所有任务从飞书拉取。Web UI **只做运维控制台**(查看/重试/扫码/告警/配置),**不**用于创建任务。

### 飞书 Bitable 表结构(用户在飞书侧创建)

字段名可在 `config.yaml/feishu.field_map` 里映射:

| 飞书字段名 | 类型 | 必填 | 工具读写 | 说明 |
|----------|------|-----|---------|------|
| 视频文件 | 单行文本 | **是** | 读 | 两种填法:**裸文件名**(如 `国庆短片01.mp4`) → 在**该任务所属账号的** `video_search_root` 下递归搜,同名取 mtime 最新;**完整 NAS 路径**(UNC `\\host\share\...` / 盘符 `X:\...` / POSIX `/Volumes/...`) → 过 `paths.path_aliases` 翻译后 `Path.exists()` 直接命中。详见 [核心约束 §5](#5-nas-访问的特殊性) |
| 标题 | 单行文本 | **是** | 读 | 16-30 字 |
| 描述 | 多行文本 | 否 | 读 | |
| 标签 | 多选 | 否 | 读 | ≤ 5 个 |
| 封面文件 | 单行文本 | 否 | 读 | 同"视频文件":裸文件名(在账号 `cover_search_root` 递归搜)或完整路径(过 `paths.path_aliases` 翻译) |
| 合集 | 单选 | 否 | 读 | 视频号合集名(需提前在平台建好) |
| 原创 | 复选框 | 否 | 读 | |
| 账号 | 单选 | **是** | 读 | **必填**:可填 **display_name(中文名)或 account_id**(validator 双向反查);留空 → 校验失败回写"未指定"(**不再** round-robin 自动分配)。账号 ID 工具侧 `secrets.token_hex(4)` 自动生成,运营无需关心;平台新增账号时会自动把 display_name 追加到本字段的飞书单选项 |
| **执行日期** | 日期 | **是** | 读 | **新增**。daemon 只跑 `execute_date = today` 的任务 |
| **定时发布时间** | 日期时间 | **是** | 读 | **改名 + 必填**(原"计划发布时间")。视频号页面上设置的发布时刻 |
| 状态 | 单选 | 否 | **写** | 工具回写:待入库 / 已计划 / 发布中 / 已发布 / 失败 |
| 已发布链接 | URL | 否 | **写** | 工具回写 |
| 错误信息 | 多行文本 | 否 | **写** | 工具回写(校验失败的具体原因 / 发布失败的错误类型) |

**约束**:`执行日期 ≤ date(定时发布时间)`(可以前一天跑、第二天发,反之不行)

### 飞书同步流程(按需,无定时 polling)

`feishu.sync_now()` 是普通函数,**不是后台定时任务**。被以下场景调用:

```bash
# 1. 手动: 立即拉飞书(不跑任务)
wxsp sync

# 2. 启动 daemon 后,以下场景自动调用 sync_now():
#    - 每天 09:00 cron 触发跑当日任务时,先 sync 再跑
#    - Web UI/CLI 手动触发任务时,先 sync 再跑
#    - Web UI "立即同步" 按钮
wxsp run --daemon
```

`sync_now()` 做的事:
1. `lark-oapi` 拉飞书"状态=待入库"的所有行(分页)
2. 每行 → `validator.validate()`(含 `nas.find_video/find_cover()`:裸文件名走 rglob,完整路径走 alias 翻译 + exists)
3. 4 核心字段(执行日期 / 定时发布时间 / 标题 / 视频文件)任一为空 → **跳过且不回写**(`SyncResult.skipped_incomplete`)。视为"业务还没填完的草稿",留到下次拉
4. 通过 → 写本地 `Video` + 创建 `Task(status=pending, execute_date, publish_at)`
5. 不通过(非草稿、确实校验失败) → 飞书回写"失败 + 错因"(具体到哪个字段)

注意:**视频文件不在飞书附件里**,飞书只存"文件名或完整路径";实际文件由 `nas.find_video()` 解析:裸文件名 → 在**目标账号的** `video_search_root` 下递归 `rglob`;完整路径 → 过 `paths.path_aliases` 翻译 + `Path.exists()`(不读 search_root)。

---

## 账号分配 + 调度

### 账号分配(账号必填,无自动分配)

飞书行的 `账号` 字段**必填**:validator 按 account_id / display_name 反查到具体账号;留空 → 校验失败回写"未指定"(**不做** round-robin 自动分配)。

1. 飞书行 `账号` 字段非空 → 反查固定到指定账号
2. 留空 → validator 判失败 + 飞书回写"未指定"(运营在飞书侧补填账号)
3. 若某账号当日任务数 > `daily_limit` → validator 拒绝入库 + 飞书回写错误

> 历史设计曾计划"空账号 round-robin 自动分配",现已改为**账号必填**(运营在飞书侧指定)。`scheduler.strategy` 配置项为保留字段,当前未使用。

### 时间调度(无时间窗口分配)

时间维度由飞书侧的"定时发布时间"字段直接决定 —— **运营自己控制何时发布**,工具不再做"把任务均匀铺到时间窗口"的逻辑。

worker 处理顺序:每天 09:00 cron 触发时,扫 `execute_date=today AND status=pending`,按 **`publish_at` 升序** 进队串行跑。

### 触发模型(无 polling)

| 触发源 | 作用 |
|-------|------|
| **每天 09:00 cron**(唯一自动入口) | ① `feishu.sync_now()` → ② 扫 today's pending → ③ 串行跑 |
| **Web UI / CLI `wxsp run --today`**(手动补单) | 同上,手动版本 |
| **Web UI / CLI `wxsp run --task-id N`**(加急单条) | ① sync → ② 跑指定 task |
| **Web UI "立即同步" / CLI `wxsp sync`** | 只 sync 飞书,不跑任务 |

**单 worker 串行**:同一时刻只跑一个任务。worker busy 时新触发的任务进队列等待。

---

## SQLite 表结构(SQLModel,4 张)

```python
class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: str
    user_data_dir: str                            # 每账号独立 Chrome profile
    daily_limit: int
    is_active: bool = True
    paused_until: datetime | None = None          # 风控触发时填,scheduler 自动跳过
    cookie_status: str = "unknown"                # ok | warn | expired | unknown
    cookie_last_checked_at: datetime | None
    cookie_last_active_at: datetime | None        # 上次主页打开成功时间(健康指标)

class Video(SQLModel, table=True):
    id: str = Field(primary_key=True)             # 飞书 record_id
    source: str = "feishu"
    file_path: str                                # 解析后的 NAS 绝对路径(由 nas.find_video 搜出)
    title: str
    description: str | None
    tags_json: str
    cover_path: str | None                        # 解析后的封面绝对路径(由 nas.find_cover 搜出)
    topic: str | None                             # 合集名
    original_claim: bool = False
    file_hash: str | None
    ingested_at: datetime

class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    video_id: str = Field(foreign_key="video.id", index=True)
    account_id: str = Field(foreign_key="account.id", index=True)
    execute_date: date = Field(index=True)        # 飞书"执行日期":worker 在这一天处理
    publish_at: datetime                          # 飞书"定时发布时间":视频号页面填的发布时刻
    status: str = Field(index=True)               # pending | running | success | failed | skipped | interrupted
    attempts: int = 0
    lease_token: str | None                       # 幂等锁:谁占用谁能跑
    lease_expires_at: datetime | None
    last_error_type: str | None
    last_error_msg: str | None
    started_at: datetime | None
    finished_at: datetime | None
    remote_video_id: str | None
    remote_url: str | None
    screenshots_json: str = "[]"

    __table_args__ = (
        UniqueConstraint("video_id", name="uq_one_task_per_video"),     # 杜绝重复入库
        Index("ix_status_execute_date", "status", "execute_date"),
    )

class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime
    level: str                                    # info | warn | error
    task_id: int | None = Field(foreign_key="task.id", index=True)
    account_id: str | None = Field(foreign_key="account.id", index=True)
    type: str                                     # cookie_expired | risk_control | element_not_found | ...
    message: str
    context_json: str = "{}"
```

### 幂等锁(claim_task)

`claim_task(task_id)` 是原子 SQL UPDATE,影响行数=1 才表示拿到执行权,杜绝"手动 retry + 调度并发触发同一 task 发两次"。配合 `unique(video_id)` 双保险。

`interrupted` 状态:daemon 启动时一次性扫 `status=running AND lease_expires_at<NOW()` 的 task 置为 interrupted,等运营从 Web UI 决定。**不**做"每 N 分钟回收僵尸"这种 polling。

---

## CLI 命令规范

```
# 账号
wxsp login <account_id>              扫码登录,刷新 Cookie
wxsp accounts list                   查看账号 + Cookie 状态
wxsp accounts pause <id> [--hours h] 暂停账号
wxsp accounts resume <id>            恢复账号

# 健康检查
wxsp doctor                          检查账号 / Cookie / NAS 可达 / 飞书 API;cookie idle 超 cookie_warn_days 标 warn 并推 cookie_warning

# 归档清理
wxsp cleanup                         按 monitoring.{log,screenshot}_retention_days 清过期日志/截图

# 飞书同步(按需)
wxsp sync                            立即拉飞书一次,不跑任务

# 执行
wxsp run --daemon                    启动 daemon(FastAPI + 09:00 cron)
wxsp run --today                     立即跑今天的 pending(先 sync 再扫再串行跑)
wxsp run --task-id <N> [--dry-run]   跑单条任务;--dry-run 不点最后的"发布"按钮

# 查看
wxsp status [--date d]               状态汇总(默认今天)
wxsp logs [--task-id t] [--follow]   查看日志

# Web
wxsp web [--port p]                  启动 Web UI(开浏览器),等价于 run --daemon + 自动打开
```

所有命令支持 `--format json` 用于脚本化。**`wxsp plan generate` 已移除**:plan 不再是独立动作,只是 tasks 表当日视图。

---

## Web UI 详细规划(FastAPI + Jinja2 + HTMX)

### 定位

**运维控制台**,不创建任务。所有任务创建在飞书侧完成。

### 页面

1. **Dashboard** — 今日总览(进度条、4 个账号卡片状态、当前正在跑、最近 10 个事件、历史积压计数)
2. **Accounts** — 账号列表,Cookie 状态,**扫码登录**(嵌入二维码,完成自动刷新),**立即同步**按钮,暂停/恢复
3. **Tasks** — 任务列表带筛选(执行日期/账号/状态),点击进详情;**手动触发跑今天**按钮
4. **TaskDetail** — 单任务全信息,每步耗时,截图缩略图,重试按钮(单条加急 fire)
5. **Plans** — 任意日期的任务清单(按 execute_date 查询;只读)
6. **Config** — 编辑 `config_{platform}.yaml`(表单 + 高级 YAML 模式),敏感字段掩码;**新增账号**(ID 工具自动生成 + display_name 重名预检 + 自动推送到飞书账号字段,账号自动归属当前平台无需手动选);**企微测试推送**按钮(展开 ENV + https-only 校验,实发一条 markdown)
7. **Logs** — SSE 实时日志流(按账号/任务/级别过滤)

### HTMX 操作交互约定(本仓库统一)

所有走 HTMX 同步执行的操作按钮(`/accounts/sync` / `/tasks/run-today` / etc.)遵守:

- **失败弹全局 modal**:路由返 `200` + 失败 HTML 片段 + 响应头 `HX-Trigger: {"opError": {"title": "...", "detail": "..."}}`。`base.html` 注册 `document.body.addEventListener('opError', ...)` 监听器,把 detail 灌进 `<dialog id="op-modal">` 并 `showModal()`。不走 4xx/5xx —— 避免 htmx 默认的 `htmx:responseError` 路径漏 swap
- **inline 状态片段**:同时把 flash 片段 swap 到 `#op-status`(成功/失败都用同一容器,绿/红/黄三色 class)
- **loading 指示**:按钮带 `hx-indicator="#xxx-spinner"`,htmx 飞行期间自动加 `htmx-request` class 显隐 spinner
- **并发去重**:同步阻塞型路由(sync / run-today)用 `threading.Lock` 串行,抢不到锁就返"正在跑中,请等"

### 实时通信

- **SSE (Server-Sent Events)** 用于日志流和任务状态推送
- FastAPI 路由 `/api/logs/stream` 和 `/api/tasks/stream` 提供 SSE
- HTMX 用 `hx-sse` 或前端 `EventSource` 订阅

### 启动方式

```bash
wxsp web                # 同时启动 FastAPI + 打开浏览器,默认 127.0.0.1:8765
# 等价于
uvicorn wxsp.api.app:app --port 8765
```

**单进程**:FastAPI 直接渲染 Jinja2 模板 + 静态文件,没有前后端分离。开发和生产部署完全一致(无 build 步骤)。HTMX 通过 CDN 引入(也可下到 `wxsp/templates/static/htmx.min.js` 自包含)。

---

## 视频号发布的关键技术点(publisher.py)

**发布页 URL**: `https://channels.weixin.qq.com/platform/post/create`

实现前**必须先读** `_ref/social-auto-upload/uploader/tencent/main.py`,理解其选择器选择和等待策略。**但用 patchright 替代原版 Playwright**(API 兼容)。

### 发布流程(严格按顺序,21 步)

每步独立函数,失败时:
- 截图到 `logs/screenshots/{YYYYMM}/{task_id}_{step}.png`
- 抛出特定错误类型(见错误分类节),让 `@retry_on` 装饰器决定重试
- 步骤间 1-3 秒随机停顿

```
[0]  claim_task(task.id)               拿不到锁 → raise AlreadyClaimed
[1]  stage_video_to_tmp()              NAS → 本地 tmp/{task_id}/(失败 → nas_unreachable)
[2]  launch_browser(account)           patchright + user_data_dir + stealth init
[3]  open_publish_page()                等待 DOM ready (最长 30s)
[4]  verify_logged_in()                "未登录"跳转 → cookie_expired (不重试)
[5]  upload_video(file)                 监听上传 + "处理完成"(超时 600s,失败 → upload_failed)
[6]  fill_title()
[7]  fill_description()
[8]  add_tags()
[9]  set_cover()                       跳过 if cover_path is None
[10] bind_topic()                       跳过 if topic is None
[11] toggle_original()                  跳过 if not original_claim
[12] disable_location()                 默认选"不显示位置"(运营约定,不接飞书字段)
[13] set_schedule(task.publish_at)     视频号页面填飞书的"定时发布时间";**无立即发布分支**
[14] risk_control_probe()              扫"请稍后/系统繁忙/操作过于频繁/账号异常"
                                       命中 → risk_control(paused_until=now+24h,告警,raise)
─────── ★ DRY_RUN GATE ★ ─────────────────
[15] if dry_run: 截图 + 关闭 + return DryRunPreview
─────────────────────────────────────────
[16] click_publish()
[17] wait_for_success_indicator()
[18] extract_remote_video_id_and_url()
[19] close_browser()
[20] cleanup_tmp(task.id)
[21] commit_task_success() + feishu.writeback(status, url, err)
```

### 选择器编写约定

- 优先用语义化选择器:`text=`、`role=`、`placeholder=`
- 避免脆弱的 CSS class
- 所有选择器集中在 `wxsp/selectors.py`,**唯一会因视频号改版而改的"易变文件"**

### 反检测(patchright + per-account 指纹注入)

视频号风控按**设备指纹**判定多账号(实测同 IP 不同电脑能多账号共存,**同 IP 同电脑**会互踩 —— 后扫的赢、先扫的被踢)。`wxsp/fingerprint.py` 给每个账号生成确定性的假指纹,绕过这层风控。

**核心机制**:
- `browser_context(..., account_id=)` → `fingerprint.get_or_create_fingerprint(account_id, storage_dir)`
- 种子 = `MD5(account_id)`,**确定性**派生(UA / screen / WebGL / Canvas 噪声 / Audio 噪声 / 字体 / Client Hints / hardwareConcurrency / deviceMemory),首次写 `data/fingerprints/{account_id}.json` 持久化,文件丢失能从种子重建
- 应用方式:
  - 部分字段通过 Playwright context options(`user_agent` / `viewport` / `locale` / `timezone_id` / `screen`)
  - 其余通过 `context.add_init_script()` 注入 JS,覆写 `navigator.webdriver` / `WebGLRenderingContext.getParameter` / `HTMLCanvasElement.toDataURL` / `AudioBuffer.getChannelData` / `navigator.userAgentData` / `RTCPeerConnection`(屏蔽 ICE candidate 防 WebRTC 漏 IP)

**关键不变量**:
- **同账号永远拿同一套指纹**。指纹漂移 → 视频号判定"同账号换设备"踢登录,后果跟没指纹一样
- **publisher / login / doctor 必须都传 account_id**。一边带指纹一边不带,视频号同样判定异常设备 → `doctor.CookieChecker` 签名是 `(account_id, user_data_dir) -> bool`,强制透传
- 指纹生成失败 → `browser_context` **软回退到无指纹模式**(`no_viewport=True`),不阻塞发布;只记 warn 日志

**升级路径**:第一次启用指纹后,旧 profile 里的 cookie 在视频号眼里关联的是"无指纹"那台设备 → **运营要重新扫码登录每个账号**。

每个账号使用独立的 `user_data_dir`(配置里指定),避免互相污染。**cookie 由 persistent context 自动管理,不再单独维护 cookie.json**。

`wxsp/stealth_js.py` 是上一代静态 init script(来自 OpenCLI 借鉴),已被 per-account 指纹完全取代,代码保留备用。

---

## 错误分类与重试策略

> ⚠️ **现状**:`retry.py` 的 `retry_on` 装饰器已实现但**尚未接线到发布步骤**。下表 transient 错误(`network` / `upload_failed` / `nas_unreachable` / `element_not_found`)的"自动重试次数"为**目标设计,待实现**;当前这些错误跑一次失败即按下表的 halt 语义处理(不自动重试)。**已生效**的是 `cookie_expired` / `risk_control` 的"不重试 + 账号级 halt"与 `element_not_found` / `nas_unreachable` 的"全局 halt"。

| 类型 | 含义 | 重试策略(`*` = 规划中,尚未接线) |
|------|------|----------|
| `network` | 网络/超时 | 指数退避,最多 3 次 `*` |
| `cookie_expired` | 登录失效 | 不重试,告警,**软失败:task 回退 pending + 不回写飞书"失败"** —— 运营扫码登录后下轮 queue_today 自动重跑(该账号其余 5 个 task 也不会被错误标 failed)。`cookie_status` 仍写 `expired`,scheduler pre-flight 下一轮跳过(**账号级 halt**) |
| `risk_control` | 平台风控 | 不重试,账号暂停 24 小时(**账号级 halt**) |
| `video_invalid` | 文件损坏/格式不支持 | 不重试 |
| `element_not_found` | 元素找不到(可能改版) | 1 次重试 `*`,截图,告警(**全局 halt** 已生效) |
| `upload_failed` | 上传中断 | 2 次重试 `*` |
| `nas_unreachable` | NAS 不可达 | 5 次指数退避 `*`(NAS 可能短暂掉线)(**全局 halt** 已生效) |
| `feishu_api_error` | 飞书 API 错误 | 3 次重试(回写路径 `writeback_row` 已实现独立退避) |
| `unknown` | 兜底 | 1 次重试 `*`,然后 failed |

> 飞书回写本身(`feishu.writeback_row`)有独立退避序列 `(1s/3s/10s,共 4 次)` —— 历史 `(1s/2s/3 次)` 在飞书抖动时被实测打穿,最后一条 task 跑完 status="已计划" 不更新。退避总时长 ~14s 远小于一次 publish 的 1-5min,不会拖慢主流程。

### Halt 机制(防告警刷屏)

`run_today_pending` 单 worker 串行跑队列,某条 task 命中下面任一情况后,后续相同范畴的 task **不再尝试**(避免反复开浏览器 + 重复推同一条告警刷屏):

| 范畴 | 触发错误 | 行为 |
|---|---|---|
| **账号级 halt** | `cookie_expired` / `risk_control` | 该账号本轮剩余 task 全 skip(写 `RunSummary.per_account[aid].halt_reason`),其他账号继续 |
| **全局 halt** | `element_not_found` / `nas_unreachable` | 整轮中止,所有账号剩余 task 全 skip(写 `RunSummary.global_halt_reason`) |

跑完后推一条 `run_summary` 通知,头部高亮 halt 原因 + 按账号 breakdown(成功/失败/跳过 + 失败明细)。`element_not_found` 选 global 是因为视频号改版会让所有账号同样失败;`nas_unreachable` 同理。

---

## 告警系统(wxsp/notify.py)

### Notifier 接口

```python
class Notifier(Protocol):
    def send(self, event: NotifyEvent) -> bool: ...

class NotifyEvent:
    type: str            # cookie_expired | risk_control | element_not_found | task_failed | nas_unreachable | run_summary | backlog_high | cookie_warning
    level: str           # info | warn | error
    title: str
    content: str
    context: dict
    task_id: int | None = None
    account_id: str | None = None
    account_display_name: str | None = None   # 渲染时优先用,运营看中文友好名
```

**第一版只实现 `WecomNotifier`**(企微机器人,Markdown 消息;飞书做协作平台,企微做告警,职责分工)。`FeishuNotifier` / `DingtalkNotifier` 留作接口预留,需要时几十行代码即可加上。

**通知文案约定(全中文,无英文穿透)**:
- 所有 markdown 标题前缀 `[视频号]`(后续多平台扩展时按 settings 取)
- `level` 渲染为中文标签:`info → [信息]` / `warn → [警告]` / `error → [错误]`
- `error_type` 通过 `error_type_cn()` 映射(如 `cookie_expired → 登录态失效`),未知 key 兜底"未知错误"
- 发布步骤(`step`)通过 `step_cn()` 映射(如 `upload → 上传视频`)。`element_not_found` 的 title 会拼上 step,例如"元素未找到 · 设置封面 —— 视频号可能改版"
- 账号优先显示 `display_name`,没有时回退到 `account_id`

### 通知规则

- ✅ 任务**失败**(重试用完后) → 企微告警(同账号同类失败已被 halt 机制去重,见上节)
- ✅ **风控** → 高优告警 + 账号自动暂停 24h
- ✅ **登录失效** → 提醒扫码;`cookie_warning`(距上次 active 超阈值但本次还能登)也推一条
- ✅ **元素找不到**(可能改版) → 告警(标题拼步骤名,运营一眼看到改版位置)
- ✅ **历史积压超阈值**(`backlog_high`) → 告警;**24h 冷却**(进程级 `threading.Lock` + Lock 内 commit 保证查写原子化,防 daemon 反复重启 / cron 多次触发刷屏)
- ✅ **日汇总**(`run_summary`) → 每轮 `run_today_pending` 跑完推一条:成功 N / 失败 M / 暂停跳过 K,按账号 breakdown + 失败明细。整轮 halt 时头部高亮原因。失败 > 0 时 level=warn,否则 info
- ⚠️ 单条任务**成功** → 默认**不发**(80 条/天会刷屏);汇总在 run_summary 里出
- ❌ 不做"每日日报"(被 run_summary 覆盖,YAGNI)

### 路由

`notify.dispatch(event)` 根据配置里启用的渠道**并行**发送给所有 notifier,任何一个失败不影响其他。

---

## 开发约定

- 所有 IO/网络/页面操作必须有超时,**不要写无限等待**
- 不要在业务代码里 `print`,统一 `from loguru import logger`
- 每个发布步骤独立函数,便于单测和重试
- Pydantic 校验所有外部输入(yaml、飞书数据、API 请求)
- DB 操作集中在 `wxsp/db.py`,业务模块拿到 ORM 对象后只读;状态变更走 `db.transition_task(task_id, status, **fields)`(让幂等锁有唯一加锁点)
- API 路由薄,业务调用同名模块函数(`api/routes_X.py` 调 `wxsp/X.py`)
- 所有路径走 `pathlib.Path`,**禁止字符串拼 `/`**(跨平台保证)
- **commit message**:Conventional Commits

### 测试

- 单元测试覆盖纯函数(validator / nas.find_video / errors.classify),pre-commit 跑
- 集成测试(DB):幂等锁、状态机
- 集成测试(浏览器):用真 patchright + 真页面 + `--dry-run`,标记 `@pytest.mark.integration`,CI 跳过
- 冒烟测试:测试号 + 预制视频跑 dry-run 全流程,发版前手动跑
- **绝不 mock 浏览器**:选择器超时是最常见问题,mock 等于没测

---

## 安全 / 不允许做的事

- ❌ 不要绕过平台风控强行高频操作
- ❌ 不要存储账号密码,只存 Cookie
- ❌ 不要把视频或 Cookie 上传到任何第三方
- ❌ 不要在生产用 headless 跑视频号
- ❌ 不要静默忽略 risk_control 类错误
- ❌ 不要硬编码任何账号/Token 到代码里
- ❌ 不要把飞书 app_secret、webhook 提交到 git(使用 `${ENV_VAR}` 引用)

---

## 起步任务清单(Milestones)

12 个 milestone,每个有明确的可验证成功标准。详细验收标准见 [docs/superpowers/specs/2026-05-12-wxsp-design.md](docs/superpowers/specs/2026-05-12-wxsp-design.md) §7。

```
M0 脚手架 ──→ M1 数据层 ──┬──→ M2 浏览器+登录 ──┐
                          ├──→ M3 飞书+validator ┼──→ M5 发布核心 ──→ M6 调度 daemon ──→ M7 通知+回写
                          └──→ M4 NAS 处理 ──────┘                                               │
                                                                                                ↓
                                                                                          M8 Web UI
                                                                                                │
                                                                                                ↓
                                                                                      M9 监控+归档
                                                                                                │
                                                                                                ↓
                                                                                       M10 部署+文档
                                                                                                │
                                                                                                ↓
                                                                                   M11 安装器+向导
```

| # | 主题 | 关键交付 |
|---|------|---------|
| M0 | 脚手架 | pyproject.toml(uv) + ruff/mypy/pre-commit + 扁平目录 + typer CLI 骨架 + config.py + config.example.yaml |
| M1 | 数据层 | SQLModel 4 表 + db.py(engine/session/transition_task/claim_task 原子锁) + `wxsp accounts add/list/pause/resume` |
| M2 | 浏览器 + 登录 | browser.py(patchright + user_data_dir + init script) + `wxsp login` + doctor 检查登录态 |
| M3 | 飞书同步 + 校验 | feishu.py(`sync_now`) + validator.py(含 NAS 文件检索) + `wxsp sync` + 不合规行回写错误 |
| M4 | NAS 处理 | nas.py(find_video/find_cover/stage_to_tmp/cleanup_tmp) + doctor 检查 NAS + nas_unreachable 重试 |
| M5 | 发布核心 | publisher.py 步骤 [0-21] + selectors.py + errors.py + retry.py + `wxsp run --task-id N [--dry-run]` |
| M6 | 调度 daemon | scheduler.py(09:00 cron + 手动 fire,无 polling)+ `wxsp run --daemon` + `wxsp run --today` + 启动扫 running→interrupted |
| M7 | 通知 + 回写 | notify.py + WecomNotifier + 飞书回写(status/url/error) + 5 类告警事件接入 |
| M8 | Web UI | FastAPI + Jinja2 + HTMX:Dashboard / Accounts / Tasks / TaskDetail / Logs(SSE) / Config + 扫码二维码嵌入 + 手动触发按钮 |
| M9 | 监控 + 归档 | Dashboard 显示积压 + 重新入队按钮 + 日志/截图清理 |
| M10 | 部署 + 文档 | README |
| M11 | 安装器 + 设置向导 | Nuitka 编译 + .dmg/.exe 出包 + Web UI **5 页向导**(第 1 步选平台→按平台生成对应 field_map→账号管理放 /config,不进向导) |

**合计 ~22.5 工作日**。**MVP 截止点 = M7 完成**(端到端跑通:飞书 → 发布 → 通知);M8-M11 是体验/运维优化,生产前不能跳。

**每个 milestone 完成后**:
1. 跑当前 milestone 的验收标准(详见 design doc §7),**逐项打勾**
2. 跑全量 `pytest`(单元 + 已有集成测试)
3. `git commit` (Conventional Commits) + push
4. **给用户演示** + 拿到 "OK,下一个" 才进下一个 milestone
5. 不一次性写完,karpathy 第 4 条 "loop until verified"

---

## 常见问题(供 Claude Code 自检)

- **Q: 一个账号被风控了,其他账号还能继续吗?** 必须能。`paused_until` 只影响该账号
- **Q: 中途 Ctrl-C 怎么办?** `running` 任务下次 daemon 启动时一次性扫描,lease 过期的标记 `interrupted`,Web UI 提示用户决定
- **Q: 同一视频要发到多个账号怎么办?** schema 是 video 1:1 task(`unique(video_id)`)。如需 1:N 在飞书表中创建多行(每行一个账号),会产生不同的 record_id 即不同的 Video
- **Q: 09:00 cron 触发时机器没开?** 开机后 daemon 启动会扫一次今天 pending(因为 `cron`本来错过了);也可以手动 `wxsp run --today` 触发
- **Q: 09:00 之后才在飞书里加的任务怎么办?** 不会自动跑(09:00 是唯一自动入口)。需要手动点 Web UI "跑今天剩余" 或 `wxsp run --today`
- **Q: 今天没跑完的任务怎么办?** 不自动 rollover。Dashboard 会显示历史积压数量,运营从 Web UI 决定:改飞书"执行日期"为今天 / 点"重新入队到今天" / 取消
- **Q: 飞书表里的字段名我想用不同的中文怎么办?** 改 `config.yaml/feishu.field_map`
- **Q: NAS 暂时连不上怎么办?** `nas_unreachable` 错误类型,5 次指数退避;持续不可达则告警
- **Q: 视频文件名重复怎么办?** `nas.find_video()` 在 `video_search_root` 递归找,多匹配取 **mtime 最新**;0 匹配 → validator 拒绝入库 + 飞书回写"文件不存在"
- **Q: Web UI 能创建任务吗?** 不能。Web UI 是运维控制台,任务创建只走飞书表
- **Q: 同一台机器上 4 个账号能不能同时登录?** 能,靠 `wxsp/fingerprint.py` 的 per-account 指纹。视频号按设备指纹判定多账号,4 个账号 4 套不同指纹 → 视频号眼里像 4 台不同电脑,不会互踩
- **Q: 升级到带指纹的版本后,为什么所有账号都掉登录了?** 旧 profile 在视频号眼里关联的是"无指纹"那台设备,新版本启动会带新指纹 → 视频号判定换设备踢登录。**升级后每个账号都要重新扫码登录一次**,后续就稳定了
- **Q: `data/fingerprints/*.json` 能删吗?** 别。删了等于让账号生成新指纹,视频号会判定"同账号换设备"踢登录。文件丢了能从 `MD5(account_id)` 种子重建出同一套(确定性),所以**真要删也得连带 `data/chrome-profiles/{account_id}/` 一起删 + 重新扫码登录**

---

## 参考项目(已在 "复用与参考" 节说明)

- **social-auto-upload** (`dreammis/social-auto-upload`):`uploader/tencent/main.py` 是视频号发布的实现参考
- **OpenCLI** (`jackwener/OpenCLI`):反检测 JS、CLI 规范、doctor 模式

---

**最后**:严格按 milestone 推进,每完成一步先跑通再继续。视频号发布的核心难点不在算法,而在浏览器自动化的稳定性、异常处理、可观测性,要花足够时间在 publisher 模块和 doctor/告警上,做好截图、日志、重试、回写。
