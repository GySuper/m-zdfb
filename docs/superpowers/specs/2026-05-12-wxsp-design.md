# wxsp 视频号自动发布工具 — 设计文档 (Brainstorm 修订版)

> **日期**: 2026-05-12
> **状态**: 已通过 brainstorm,待 writing-plans 转化为实施计划
> **关系**: 这份文档是 [CLAUDE.md](../../../CLAUDE.md) 的修订版。CLAUDE.md 是项目长期主指南,本文档是本次 brainstorm 的结论。**冲突以本文档为准**(后做的赢)。

---

## 1. 关键决策汇总

| 决策 | 选择 | 替换原 CLAUDE.md 中的 | 理由 |
|------|------|---------------------|------|
| 任务数据源 | 飞书 Bitable 单源 | 飞书 + Web UI 双源 | YAGNI,避免双源同步/字段映射/冲突处理 |
| Web UI 定位 | 运维控制台(查看/重试/扫码/告警/配置),**不创建任务** | 既创建又查看 | 职责清晰 |
| Web UI 技术栈 | FastAPI + Jinja2 + HTMX | Vue 3 + Vite + Pinia + Tailwind + Element Plus | 单进程、纯 Python、SSE 自然;本地单用户工具不需要 SaaS 级前端 |
| 反检测/浏览器 | **patchright** + 独立 `user_data_dir` | playwright-stealth + 自写 init script | patchright 是 Playwright fork,深度修补 CDP 指纹,API 完全兼容 |
| Cookie 管理 | 取消独立 `cookie.json`,只靠 `user_data_dir` 持久化 | cookie.json + user_data_dir 双轨制 | persistent context 已经管 cookie,双轨是 social-auto-upload 的历史包袱 |
| 调度模型 | **每天 09:00 cron + 单 worker 串行 + 手动触发兜底;无任何 polling** | APScheduler daemon + 多 polling job;每 task 精准 trigger | 飞书侧只给"执行日期"(天粒度),按天批量处理更自然;运营工作流"前一晚或当天上午前填表,09:00 开跑" |
| 飞书同步 | **按需调用(worker 入口前自动 + 手动按钮),不定时拉** | 每 60s polling | 用户反馈"取消自动同步";避免无谓 API 调用,需要时点按钮 |
| 飞书文件字段 | **只填文件名,系统在 NAS 递归检索** | 填路径(相对或绝对) | 运营不需要懂路径,丢文件到固定目录即可;同名取修改时间最新 |
| 飞书新增"执行日期"字段 | **必填,日期粒度** | 无此字段 | daemon 只跑"执行日期=今天"的任务,运营可以提前安排 |
| 飞书"计划发布时间" → "定时发布时间" | **必填,只走定时发布,不再支持立即发布** | 可空 | publisher 流程简化无分支 |
| 配置 | `config.yaml` + `${ENV_VAR}` 两层 | default.yaml + yaml + env + CLI 四层 | 本地单用户工具,CLI 覆盖几乎用不上 |
| 跨平台 | **macOS + Windows 都支持** | 隐含只 mac | 用户需求 |
| Notifier (第一版) | **企业微信** 一个,接口预留 | 飞书 + 企微 + 钉钉三个 | 飞书做协作平台,企微做告警,分工合理 |
| 目录结构 | 顶层扁平 `wxsp/*.py`,超 500 行再拆 | `wxsp/accounts/` `wxsp/ingestion/` 等深层目录 | 早期分包除了找文件的麻烦没好处 |
| ORM | SQLModel | SQLAlchemy 2.x | 模型即 schema 即 Pydantic 校验,4 张表的规模刚好;底层兼容 SQLAlchemy |
| 任务-账号关系 | 1:1 (一视频一任务) | 同 | 跨账号分发由飞书多行实现 |
| 同 IP 并发 | `max_concurrent_accounts = 1` 默认 | 默认 2 | 同 IP 多账号同时活跃是风控特征 |

---

## 2. 整体架构

```
┌─────────────┐   poll    ┌──────────────┐     ┌──────────┐
│ 飞书 Bitable │ ←──────→ │  ingestion   │ ──→ │  SQLite  │
└─────────────┘  writeback │ + validator  │     │ (单一    │
                          └──────────────┘     │  事实源) │
                                                └────┬─────┘
                                                     ↓
            ┌──────────────────────────────────────────────┐
            │  wxsp run --daemon                           │
            │                                              │
            │  飞书同步 (按需,不轮询):                      │
            │   函数 feishu.sync_now() 在以下时机被调用:    │
            │    - worker 入口前自动调一次                  │
            │    - Web UI "立即同步" 按钮 / `wxsp sync`     │
            │   拉飞书新行 → validator (含 NAS 检索)        │
            │   → 入库为 pending                           │
            │                                              │
            │  任务执行器 (单 worker,一次跑一个):           │
            │   触发源:                                     │
            │    1. 每天 09:00 cron fire                   │
            │    2. Web UI/CLI 手动 fire (今日剩余 / 单条)  │
            │   每次触发:                                   │
            │    ① 先调 feishu.sync_now()                  │
            │    ② 扫 execute_date=today AND status=pending│
            │    ③ 按 publish_at 升序入队,串行调 publisher │
            │   → 状态实时 DB + SSE → 结束发企微            │
            └──────────────┬───────────────────────────────┘
                           ↓
            ┌──────────────────────────────────────────────┐
            │   publisher (patchright + persistent ctx)    │
            │                                              │
            │  [1] 复制视频 NAS → tmp/                     │
            │  [2] 启动浏览器 (user_data_dir + stealth)    │
            │  [3-13] selectors.py 驱动发布步骤            │
            │  [14] ★ DRY_RUN GATE: dry-run 在此停下       │
            │  [15-17] 真发布 + 提取 remote_url            │
            │  [18-20] 清理 tmp + 写 DB + 回写飞书         │
            │                                              │
            │  失败时:截图到 logs/screenshots/             │
            └──────────────────────────────────────────────┘
                       ↓                       ↓
                  ┌─────────┐             ┌────────────┐
                  │企微告警 │             │ Web UI     │
                  │(任务级) │             │ (HTMX/SSE) │
                  └─────────┘             └────────────┘
```

**核心模型**:每天 09:00 cron 触发跑当日任务 + 单 worker 串行 + 手动触发兜底 + 任务结束才通知。

---

## 3. 模块切分

### 3.1 目录结构 (扁平)

```
wxsp/
├── __init__.py
├── cli.py              # Typer 命令入口
├── config.py           # Pydantic Settings, 加载 config.yaml + env var
├── db.py               # SQLModel engine + session + 状态转换辅助
├── models.py           # SQLModel 表 (Account, Video, Task, Event)
├── feishu.py           # Bitable 拉取 + 回写
├── validator.py        # 入库校验
├── scheduler.py        # 09:00 cron + 手动 fire (无 polling)
├── publisher.py        # 视频号发布核心
├── selectors.py        # 选择器集中管理(改版时唯一改动点)
├── browser.py          # patchright context 工厂 + stealth 注入
├── stealth_js.py       # init script 常量
├── errors.py           # 错误类型 + 重试策略
├── notify.py           # Notifier 协议 + WecomNotifier
├── doctor.py           # 健康检查
├── nas.py              # NAS 文件检索 (按文件名递归 rglob) + 复制到 tmp/
├── retry.py            # 重试装饰器 / 指数退避
├── api/
│   ├── app.py          # FastAPI 入口 + 静态文件 + 模板
│   ├── deps.py
│   ├── routes_dashboard.py
│   ├── routes_accounts.py
│   ├── routes_tasks.py
│   ├── routes_plans.py
│   ├── routes_config.py
│   └── routes_logs.py  # SSE
└── templates/          # Jinja2
    ├── base.html
    ├── dashboard.html
    ├── accounts.html
    ├── tasks.html
    ├── task_detail.html
    ├── plans.html
    ├── config.html
    └── logs.html

deploy/
├── wxsp.plist          # macOS launchd 模板
└── wxsp-task.xml       # Windows 任务计划程序模板

data/
├── db.sqlite
├── chrome-profiles/    # 每账号一个 user_data_dir
└── tmp/                # 视频 stage 目录,发完清理

logs/
├── wxsp.{date}.log
└── screenshots/{YYYYMM}/{task_id}_{step}.png
```

### 3.2 模块职责

| 模块 | 主要入口 | 依赖 | 不依赖 |
|------|---------|------|--------|
| `feishu.py` | `sync_now() -> SyncResult` (拉 + 校验 + 入库) / `writeback(task_id, status, url, err)` | `lark-oapi` + `config.feishu` + DB + validator | - |
| `validator.py` | `validate(row: VideoRow) -> Result[Video, ValidationError]` | 纯函数 | 一切 IO |
| `scheduler.py` | `daemon.start()` / `queue_today()` / `fire_task(task_id)` | DB + APScheduler | publisher, feishu |
| `publisher.py` | `publish(task, *, dry_run=False) -> PublishResult` | `browser.py`, `selectors.py`, `nas.py` | DB, 调度 |
| `browser.py` | `with browser_context(account) as page: ...` | `patchright`, `stealth_js` | 业务逻辑 |
| `selectors.py` | 常量字典 | - | - |
| `notify.py` | `notify(event)` | `WecomNotifier` | DB |
| `doctor.py` | `check_all() -> list[CheckResult]` | 所有 IO 模块 | - |
| `nas.py` | `find_video(filename)` / `find_cover(filename)` (递归搜,多匹配取 mtime 最新) / `stage_to_tmp` / `cleanup_tmp` | `pathlib` | - |

### 3.3 跨模块约束

- **DB 操作集中**:业务模块拿到 ORM 对象后只读,状态变更通过 `db.transition_task(task_id, status, **fields)`,让幂等锁有唯一加锁点
- **路径全用 `pathlib.Path`**,禁止字符串拼 `/`,跨平台保证
- **`api/` 路由薄**,业务调用同名模块函数
- **`selectors.py` 是唯一会因视频号改版而改动的"易变文件"**

---

## 4. 数据模型 + 状态机

### 4.1 SQLModel 表 (4 张)

```python
class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: str
    user_data_dir: str
    daily_limit: int
    is_active: bool = True
    paused_until: datetime | None = None
    cookie_status: str = "unknown"      # ok | warn | expired | unknown
    cookie_last_checked_at: datetime | None
    cookie_last_active_at: datetime | None

class Video(SQLModel, table=True):
    id: str = Field(primary_key=True)        # 飞书 record_id
    source: str = "feishu"
    file_path: str                           # 已解析的 NAS 绝对路径
    title: str
    description: str | None
    tags_json: str
    cover_path: str | None
    topic: str | None
    original_claim: bool = False
    file_hash: str | None
    ingested_at: datetime

class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    video_id: str = Field(foreign_key="video.id", index=True)
    account_id: str = Field(foreign_key="account.id", index=True)
    execute_date: date = Field(index=True)        # 飞书"执行日期",worker 在这一天处理
    publish_at: datetime                          # 飞书"定时发布时间",视频号页面填的发布时刻
    status: str = Field(index=True)
    attempts: int = 0
    lease_token: str | None
    lease_expires_at: datetime | None
    last_error_type: str | None
    last_error_msg: str | None
    started_at: datetime | None
    finished_at: datetime | None
    remote_video_id: str | None
    remote_url: str | None
    screenshots_json: str = "[]"

    __table_args__ = (
        UniqueConstraint("video_id", name="uq_one_task_per_video"),
        Index("ix_status_execute_date", "status", "execute_date"),
    )

class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime
    level: str
    task_id: int | None = Field(foreign_key="task.id", index=True)
    account_id: str | None = Field(foreign_key="account.id", index=True)
    type: str
    message: str
    context_json: str = "{}"
```

### 4.2 任务状态机

```
pending ──claim()──→ running ──success──→ success
   │                    │
   │                    ├──fail(retryable)──→ pending (attempts++)
   │                    ├──fail(fatal)──────→ failed
   │                    └──crash/timeout────→ interrupted
   │
   ├←──manual retry─────────────────────────────┤
   │                                            │
   └──scheduler skip(account paused)──→ skipped ┘
```

### 4.3 幂等锁实现 (原子)

```sql
UPDATE task
SET status='running',
    lease_token=:uuid,
    lease_expires_at=NOW() + interval '30 min',
    started_at=NOW(),
    attempts=attempts+1
WHERE id=:tid
  AND status='pending'
  AND execute_date <= CURRENT_DATE   -- 只能跑今天或之前的(允许人工把昨天的标 today 后手动 fire)
  AND NOT EXISTS (
    SELECT 1 FROM task WHERE id=:tid AND status='running'
                       AND lease_expires_at > NOW()
  );
```

影响行数 = 1 才表示拿到执行权;= 0 表示别人在跑或不可执行。`unique(video_id)` 保证一视频一任务,杜绝重复入库。

`interrupted` 状态:daemon 启动时一次性扫描 `running` 且 lease 过期的 task,标记 `interrupted` 等待人工决定。**不做"每 5 min 定时回收僵尸"这种 polling job**。

---

## 5. 核心流程

### 5.1 发布流程

```
publish(task, *, dry_run=False) -> PublishResult
│
├─ [0] claim_task(task.id) ─────→ 拿不到锁 → raise AlreadyClaimed
├─ [1] stage_video_to_tmp() ────→ NAS→本地 tmp/{task_id}/
│   └─ 失败 → nas_unreachable (5 次指数退避)
├─ [2] launch_browser(account) ─→ patchright + user_data_dir + stealth init
├─ [3] open_publish_page() ─────→ 等待 DOM ready, 最长 30s
├─ [4] verify_logged_in() ──────→ "未登录"跳转 → cookie_expired (不重试)
├─ [5] upload_video(file) ──────→ 监听上传 + "处理完成", 超时 600s
├─ [6] fill_title()
├─ [7] fill_description()
├─ [8] add_tags()
├─ [9] set_cover()         ◇ 跳过 if cover_path is None
├─ [10] bind_topic()       ◇ 跳过 if topic is None
├─ [11] toggle_original()  ◇ 跳过 if not original_claim
├─ [12] set_schedule(publish_at) → 视频号页面填 task.publish_at;无立即发布分支
├─ [13] risk_control_probe()   → 扫描"请稍后/系统繁忙/操作过于频繁/账号异常"
│       └─ 命中 → risk_control (paused_until=now+24h, 告警)
│
├─ [14] ★ DRY_RUN GATE ★ ──────→ if dry_run: 截图 + 关闭 + return
│
├─ [15] click_publish()
├─ [16] wait_for_success_indicator()
├─ [17] extract_remote_video_id_and_url()
├─ [18] close_browser()
├─ [19] cleanup_tmp(task.id)
└─ [20] commit_task_success() + writeback_feishu()
```

**每步约定**:
- 入口:显式等待目标元素出现,不用 sleep
- 出口:失败时 `screenshot(task_id, step_name)`
- 跨步:1-3 秒随机停顿
- 选择器都在 `selectors.py`,业务文件不出现 raw CSS/XPath

### 5.2 调度模型

daemon 里**没有任何 polling**:飞书 sync 不轮询、tasks 表不轮询、cookie 不轮询。所有动作要么由 cron 在固定时刻 fire,要么由用户主动触发。

#### 飞书同步(按需调用,非定时 job)

`feishu.sync_now()` 是一个普通函数,**不是后台定时任务**。它被以下场景调用:

| 调用时机 | 谁触发 |
|---------|------|
| worker 入口前的 ① 步 | 09:00 cron 或手动 fire 任务的内部流程 |
| `wxsp sync` CLI 命令 | 用户手动 |
| Web UI "立即同步" 按钮 | 运营手动 |

每次调用做的事:
1. `lark-oapi` 拉飞书"状态=待入库"的所有行(分页)
2. 每行 → `validator.validate()`(含 `nas.find_video/find_cover()` 搜文件)
3. 通过 → 写 DB,Task 状态=`pending`
4. 不通过 → 飞书回写"失败 + 错因"

#### 任务执行器(单 worker,串行)

触发源(按优先级):

| 优先级 | 触发源 | 流程 |
|------|-------|------|
| 1 | **每天 09:00 cron trigger** | ① `feishu.sync_now()` → ② 扫 `execute_date=today AND status=pending` → ③ 按 `publish_at` 升序入队 → 串行跑 |
| 2 | **Web UI "跑今天剩余" / CLI `wxsp run --today`** | 同上,手动版本 |
| 3 | **Web UI 单任务按钮 / CLI `wxsp run --task-id N`** | ① `feishu.sync_now()` → ② 跑指定 task |

**单 worker 语义**:同一时刻只跑一个任务。worker busy 时新触发的任务进队列等待。`max_concurrent_accounts = 1` 默认。

**worker 处理时刻 ≠ 视频号发布时刻**:worker 跑完后视频号页面已设置好 `publish_at` 的定时,视频号平台自己在到点发出。前一天就把"明天 14:00 发"的任务跑完是正常工作流。

**daemon 启动时一次性扫描**:发现 `status=running` 但 lease 过期 → 标 `interrupted`,等用户决定。

#### 为什么不做事件驱动 / 不做定时拉飞书?

- **不做事件驱动**:运营工作流是"前一晚或当天上午前填表,上午开跑",09:00 cron 触发已经覆盖。事件驱动反而和"上午才开始跑"的预期冲突
- **不做定时拉飞书**:用户明确反馈"取消自动同步"。飞书 sync 改成"worker 入口前自动 + 手动按钮兜底",避免无谓的 API 调用

### 5.3 错误分类 + 重试

| 错误类型 | 重试 | 退避 | 副作用 |
|---------|------|-----|--------|
| `network` | 3 次 | 指数 (2/4/8s) | - |
| `nas_unreachable` | 5 次 | 指数 (5/10/20/40/80s) | 持续失败告警 |
| `upload_failed` | 2 次 | 30s | - |
| `element_not_found` | 1 次 | 0 | 截图 + 告警(可能改版) |
| `feishu_api_error` | 3 次 | 指数 | - |
| `cookie_expired` | **不重试** | - | 告警 + 等待 `wxsp login` |
| `risk_control` | **不重试** | - | 账号暂停 24h + 高优告警 |
| `video_invalid` | **不重试** | - | validator 阶段应已拦截 |
| `unknown` | 1 次 | 0 | 然后 failed |

实现:`@retry_on(error_types=..., max_attempts=..., backoff=...)` 装饰器,错误类型在 `errors.py`。

### 5.4 飞书 Bitable 字段(修订后,替代 CLAUDE.md 中的字段表)

运营在飞书侧维护一张表,字段如下(中文字段名可在 `config.yaml/feishu.field_map` 里改名映射):

| 飞书字段名 | 类型 | 必填 | 工具读写 | 说明 |
|----------|------|-----|---------|------|
| 视频文件 | 单行文本 | **是** | 读 | **仅文件名**(如 `国庆短片01.mp4`),系统在 `video_search_root` 递归搜 |
| 标题 | 单行文本 | **是** | 读 | 16-30 字 |
| 描述 | 多行文本 | 否 | 读 | |
| 标签 | 多选 | 否 | 读 | ≤ 5 个 |
| 封面文件 | 单行文本 | 否 | 读 | **仅文件名**,系统在 `cover_search_root` 递归搜;同名取 mtime 最新 |
| 合集 | 单选 | 否 | 读 | 视频号合集名(需提前在平台建好) |
| 原创 | 复选框 | 否 | 读 | |
| 账号 | 单选 | 否 | 读 | 空 → round-robin 分配;指定 → account_id |
| **执行日期** | 日期 | **是** | 读 | **新增**。daemon 只跑 `execute_date = today` 的任务 |
| **定时发布时间** | 日期时间 | **是** | 读 | **改名 + 必填**(原"计划发布时间")。视频号页面上设置的发布时刻 |
| 状态 | 单选 | 否 | **写** | 工具回写:待入库 / 已计划 / 发布中 / 已发布 / 失败 |
| 已发布链接 | URL | 否 | **写** | 工具回写 |
| 错误信息 | 多行文本 | 否 | **写** | 工具回写(校验失败的具体原因 / 发布失败的错误类型) |

**约束**:`执行日期 ≤ date(定时发布时间)`(可以前一天跑、第二天发,反之不行)

### 5.5 入库校验 (质量门前移)

`validator.py::validate(row)` 在飞书同步阶段执行,**不合规行回写错误信息到飞书**,不入 DB:

**字段校验**
- 标题:必填,16-30 字 (Unicode)
- 标签:≤ 5 个
- 视频文件名:必填,在 `video_search_root` 递归搜到 + 大小 ≤ 4GB + 扩展名 ∈ {.mp4, .mov};多匹配取 **mtime 最新**;搜不到 → 拒绝 + 回写"视频文件不存在"
- 封面文件名:可空;填了 → 同视频规则,在 `cover_search_root` 递归搜 + 比例 ∈ {1:1, 16:9, 3:4}
- **执行日期**:必填 (日期)
- **定时发布时间**:必填 (日期时间);在 `[now+30min, now+14d]` 区间内(以**入库时刻**为准);**日期部分 ≥ 执行日期**(可以前一天跑、第二天发,但不能"今天的执行日期 + 昨天的发布时间")
- 账号:空 → 按 round-robin 分配;指定 → 校验存在且 enabled

**通过** → 解析视频/封面绝对路径后写 `Video` + 创建 `Task(status='pending', execute_date=..., publish_at=...)`

**不通过** → 飞书回写"失败 + 错因"(具体到哪个字段哪个问题)

**NAS 文件检索实现** (`nas.py::find_video / find_cover`):
```python
def find_video(filename: str) -> Path:
    """在 config.paths.video_search_root 下递归找 filename。
    多匹配 → 取 mtime 最新;0 匹配 → raise FileNotFound。"""
    matches = list(video_search_root.rglob(filename))
    if not matches:
        raise FileNotFound(filename)
    return max(matches, key=lambda p: p.stat().st_mtime)
```

每次 sync 现扫,不维护索引(MVP)。NAS 文件膨胀到上万级再考虑加索引。

### 5.6 容量监控

Dashboard 显眼显示:
- 今日计划总数 (`execute_date=today`) / 已完成 / 剩余 pending / 失败
- **历史积压**:`execute_date<today AND status IN ('pending','interrupted')` —— 那些昨天/前天没跑完的
- 估计追赶时间(按平均单条耗时估)

**积压告警**:任意时刻历史积压 > 20 条 → 企微告警"建议人工干预"。

**不做自动 rollover**:今天没跑完的任务不会自动滚到明天 —— 因为 daemon 第二天 09:00 cron 只扫"execute_date=今天",昨天的不会被自动重跑。运营在 Dashboard 看到积压后,可选择:
- 飞书侧改"执行日期"为今天 → 下次 sync 后 DB 更新,当天可手动 fire
- Web UI 点"重新入队到今天" → 直接把 task.execute_date 改 today,需手动 fire 才跑
- Web UI 标"取消"

显式的人工决定避免静默滚动堆积。

---

## 6. 测试 + 可观测性 + 部署

### 6.1 测试策略

| 层 | 测试什么 | 频率 |
|----|---------|-----|
| **单元** | `validator.validate()` / `nas.find_video/find_cover()` / `errors.classify()` / `scheduler.queue_today()` | 每次 commit (pre-commit) |
| **集成 (DB)** | DB transitions / 幂等锁 / 状态机 | 每次 commit |
| **集成 (浏览器)** | publisher 步骤,`@pytest.mark.integration` | 本地手动,CI 跳过 |
| **冒烟 (端到端)** | 测试号 + 预制视频 + dry-run 全流程 | 发版前 |

**不 mock 浏览器**:选择器超时是最常见的问题,mock 等于没测。集成测试用真 patchright + 真页面 + dry-run。

### 6.2 可观测性

四个面板:
- **Dashboard**:今日进度 / 当前正在跑(实时) / 最近事件
- **TaskDetail**:每步耗时 + 状态(SSE 实时) + 截图缩略图
- **Logs**:SSE 实时流,按账号/任务/级别 filter
- **企微告警**:任务级失败 / 风控 / cookie / 改版,卡片链接跳 Web UI

**通知规则**:
- ✅ 任务**失败**(重试用完后) → 企微告警
- ✅ **风控** → 高优告警 + 账号暂停 24h
- ✅ **登录失效** → 提醒扫码
- ✅ **元素找不到**(改版) → 告警
- ⚠️ 任务**成功** → 默认**不发**(80 条/天会刷屏),只留 event 记录

### 6.3 日志/截图归档

- 日志:`loguru` 按天滚动,保留 30 天,zip 压缩
- 截图:
  - 成功任务:不存
  - 失败任务:保留 90 天
  - 路径:`logs/screenshots/{YYYYMM}/{task_id}_{step}.png`
- 启动时 `cleanup_old_files()` 清过期文件

### 6.4 部署

**安装** (mac/Windows 一致):
```bash
uv venv && uv pip install -e .
uv run playwright install chromium
cp config.example.yaml config.yaml  # 编辑
uv run wxsp doctor
uv run wxsp login account_a         # 每账号扫码
uv run wxsp run --daemon
```

**开机自启**:
- macOS:`deploy/wxsp.plist` + `launchctl load`
- Windows:`deploy/wxsp-task.xml` + `schtasks /create`
- daemon 崩了会被 OS 拉起

**升级**:
- `uv pip install -U -e .`
- DB 加字段:SQLModel `create_all` 兜底
- breaking change:才引入 alembic (MVP 不引入)

---

## 7. Milestone 拆分

按 karpathy "Goal-Driven Execution",每个 milestone 有可验证的成功标准。

### 关键路径

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
```

### Milestone 表

| # | 主题 | 主要交付 | **验收标准** | 预计 |
|---|------|---------|------------|-----|
| **M0** | 脚手架 | `pyproject.toml` (uv) + ruff/mypy/pre-commit + 扁平目录 + typer CLI 骨架 + config.py + `config.example.yaml` | `uv run wxsp --help` 列出所有命令;`uv run pre-commit run --all` 全绿 | 0.5d |
| **M1** | 数据层 | SQLModel 4 表 + `db.py` (engine/session/`transition_task()`/`claim_task()` 原子锁) + `wxsp accounts add/list/pause/resume` | 单元测试:两个并发 worker 抢同一 task 只有一个成功;`unique(video_id)` 约束生效 | 1d |
| **M2** | 浏览器 + 登录 | `browser.py` (patchright + user_data_dir + init script) + `wxsp login <account>` 扫码 + `doctor` 检查登录态 | 测试号扫码登录成功;重启 daemon 后无需重新扫码;`wxsp doctor` 输出每个账号 `cookie_status` | 1.5d |
| **M3** | 飞书同步 + 校验 | `feishu.py` + `validator.py` + `wxsp sync` + 不合规行回写错误 | fixture 表放 10 行 (5 合规 5 不合规),sync 后 DB 5 条 Video,飞书 5 行回写"失败+错因" | 1.5d |
| **M4** | NAS 处理 | `nas.py` (`resolve_path` / `stage_to_tmp` / `cleanup_tmp`) + `doctor` 检查 NAS + `nas_unreachable` 重试 | mac `/Volumes/NAS/...` 和 Windows `\\server\share\...` 都能 resolve;NAS 断开 5 次退避后告警 | 0.5d | **M4 验收完成 (2026-05-13)** |
| **M5** | 发布核心 | `publisher.py` 步骤 [0-20] + `selectors.py` + `errors.py` + `retry.py` + `wxsp run --task-id N [--dry-run]` | (1) dry-run 跑到 publish 按钮前停下截图;(2) 真发一条成功,DB 写入 `remote_url`;(3) 重复触发幂等锁只发一次 | 3-4d | **M5 代码完成 (2026-05-13) —— 真账号验收延后** |
| **M6** | 调度 daemon | `scheduler.py` (09:00 cron + 手动 fire,无 polling) + `wxsp run --daemon` + `wxsp run --today` + `wxsp sync` + worker 入口前自动调 `feishu.sync_now()` + 启动扫描 running→interrupted | 09:00 cron 触发时先 sync 飞书再扫 today pending 入队;手动 `wxsp run --today` 等效;`wxsp sync` 只拉飞书不跑任务;kill 后重启 running→interrupted | 1.5d | **M6 完成 (2026-05-13)** |
| **M7** | 通知 + 回写 | `notify.py` + `WecomNotifier` + 飞书回写 + 5 类告警事件 | 故意造 (a) cookie_expired (b) risk_control (c) element_not_found (d) task_failed,企微收到卡片,飞书状态正确回写 | 1d | **M7 完成 (2026-05-13)** —— 真账号企微卡片验收延后
| **M8** | Web UI | FastAPI + Jinja2 + HTMX:Dashboard / Accounts / Tasks / TaskDetail / Logs(SSE) / Config + 扫码二维码嵌入 + 手动触发按钮 | 打开 `127.0.0.1:8765`:看到 4 账号卡片;点 Task #N "重试"立刻触发;Logs 实时流动 | 3d | **M8 完成 (2026-05-13)** —— 扫码弹 patchright 窗口而非内嵌二维码(Web UI 不持浏览器),其余项目验收通过 |
| **M9** | 监控 + 归档 | Dashboard 显示积压 + 重新入队按钮 + 日志/截图清理 | 塞 50 条历史 pending,Dashboard 显示"历史积压 X 条";点"重新入队到今天"能改 execute_date;过保留期文件被清掉 | 1d | **M9 完成 (2026-05-14)** —— backlog 查询覆盖 pending+interrupted;`/tasks?backlog=1` 视图 + 行内"重新入队到今天"按钮;`wxsp/archive.py`(cleanup_old_files + install_file_sink + CleanupReport);`wxsp cleanup` CLI;daemon/web 启动时装文件 sink + 调清理 + 跑积压告警;`monitoring.{log,screenshot}_retention_days` 和 `backlog_warn_threshold` 进 config + UI
| **M10** | 部署 + 文档 | `deploy/wxsp.plist` + `deploy/wxsp-task.xml` + README | mac `launchctl load` 后开机起 daemon;Windows 任务计划程序同效 | 1d | **M10 完成 (2026-05-14)** —— `deploy/wxsp.plist`(macOS LaunchAgent,`gui/$UID` 用户会话,plutil 通过)+ `deploy/wxsp-task.xml`(Windows LogonTrigger,登录后 30s 触发,xmllint 通过)+ README 重写覆盖安装/扫码/启动/开机自启(mac+Win)/故障排查/项目结构/开发约定;还补了 M4 留的 Windows symlink fallback —— `nas.stage_to_tmp` 优先 symlink,OSError(WinError 1314 没 SeCreateSymbolicLinkPrivilege)时 fallback 到 `shutil.copy2`,connect+copy 都失败才抛 NasUnreachable;真机 `launchctl bootstrap` / `schtasks /Create` 验收延后(无 Windows 环境) |

**合计 ~15 工作日**(单人,不含并行)。**MVP 截止点 = M7 完成**(能端到端跑通飞书 → 发布 → 通知);M8-M10 是体验/运维优化,但生产前不能跳。

### 每个 milestone 完成必做

1. 跑当前 milestone 的验收标准,**逐项打勾**
2. 跑全量 `pytest`(单元 + 已有集成)
3. `git commit` (Conventional Commits) + push
4. **给用户演示** + 拿到 "OK,下一个" 才进下一个
5. 不一次性写完,karpathy 第 4 条 "loop until verified"

---

## 8. 风险/已知约束

来自 brainstorm 的 8 个风险点,全部已纳入设计:

| # | 风险 | 应对 | 体现在 |
|---|------|-----|-------|
| 1 | 80 条/天容量不一定跑得完 | Dashboard 显眼积压数 + 人工决定如何重新入队(不自动 rollover) | §5.6 + M9 |
| 2 | 任务幂等性:手动重试 + 调度并发 → 发两次 | `claim_task()` 原子锁 + `unique(video_id)` | §4.3 |
| 3 | NAS 网络抖动导致上传重传 | 发布前 `stage_to_tmp()` 复制到本地 | §5.1 步骤 [1] |
| 4 | 调试期不能每次真发布 | `--dry-run` 在步骤 [14] 停下 | §5.1 |
| 5 | Cookie 寿命实际 3-7 天 (非 7-15 天) | `cookie_warn_days` 默认 1.5;`cookie_last_active_at` 而非"导入时间" | §4.1 |
| 6 | 同 IP 多账号同时活跃 = 风控特征 | `max_concurrent_accounts = 1` 默认 | §5.2 |
| 7 | 入库即校验,不要等发布报错 | `validator.py` 在飞书同步阶段拦截 | §5.5 |
| 8 | 截图/日志膨胀 | 30d 日志 + 90d 失败截图 + 启动清理 | §6.3 |

### 已知约束

- **macOS + Windows 跨平台**:全用 `pathlib.Path`;开机自启各一份模板
- **视频号风控敏感**:`headless=false`、1-3s 随机停顿、风控文案触发立即暂停
- **Cookie 7-15 天寿命**(实际更短):自动监控 + 主动告警 + 等待人工扫码
- **视频号定时发布平台限制**:[now+30min, now+14d],validator 阶段校验
- **不绕风控、不存密码、不上传任何数据到第三方**

---

## 9. 与 CLAUDE.md 的差异说明

本文档是 CLAUDE.md 的修订版。CLAUDE.md 作为项目长期主指南保留,**实际开发以本文档为准**。主要差异:

| CLAUDE.md 原方案 | 本文档修订 |
|----------------|-----------|
| Vue 3 + Vite + Pinia + Tailwind + Element Plus | FastAPI + Jinja2 + HTMX |
| 飞书 + Web UI 双数据源 | 飞书单源,Web UI 仅做运维控制台 |
| playwright-stealth + 自写 init | patchright |
| cookie.json + user_data_dir 双轨 | 仅 user_data_dir |
| 三个 Notifier (飞书/企微/钉钉) | 企微一个,接口预留 |
| 配置四层 (default + yaml + env + CLI) | 两层 (yaml + env) |
| 深层目录 (`wxsp/accounts/`, `wxsp/ingestion/` 等) | 顶层扁平 |
| SQLAlchemy 2.x | SQLModel |
| `max_concurrent_accounts: 2` | `= 1` 默认 |
| daemon + 多个 polling job | 每天 09:00 cron + 手动触发兜底 + 单 worker 串行;**无任何 polling** |
| 飞书每 60s 自动 sync | 改为**按需 sync**:worker 入口前自动调一次 + Web UI/CLI 手动触发 |
| 飞书"视频文件"填路径 | **改填文件名**,系统在 NAS 递归检索,同名取 mtime 最新 |
| 飞书"封面文件"填路径 | **改填文件名**,同上 |
| (无) | **新增"执行日期"字段**(必填,日期粒度),daemon 只跑 execute_date=today |
| "计划发布时间"可空,支持立即/定时 | **改名"定时发布时间",必填**,publisher 不再支持立即发布分支 |
| Task 表有 `scheduled_at` | **改为 `execute_date` + `publish_at` 两字段** |
| 18 个 milestone | 11 个 (M0-M10) |
| 未明确跨平台 | mac + Windows 都支持 |
| 未提幂等锁 / dry-run / NAS stage / 容量监控 | 全部纳入 |

---

**下一步**:invoke `superpowers:writing-plans` skill,把本设计转化为可执行的实施计划。
