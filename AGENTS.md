# AGENTS.md — wxsp 多平台自动发布工具

> 权威设计文档是 [CLAUDE.md](CLAUDE.md)(800+ 行,含完整架构/约束/操作手册)。本文件是给 agent 的速读版,只挑了容易踩坑的硬约束;细节查 CLAUDE.md 对应章节。

## 项目简介

多平台短视频自动发布工具:**飞书 Bitable 是唯一任务源**,每天 09:00 cron 触发,Web UI(127.0.0.1:8765)做运维控制台。支持 6 个平台:`tencent_channel`(视频号)/`douyin`/`kuaishou`/`xiaohongshu`/`taobao_guanghe`/`pinduoduo`。macOS + Windows 单机部署,Python 3.10+。

## 常用命令

```bash
uv sync                                    # 装依赖(含 dev)
uv run pytest                              # 全量测试
uv run pytest -m "not integration"         # 单元测试(不点真页面;CI 跑这个)
uv run pytest tests/test_douyin_platform.py # 跑单个平台/模块
uv run ruff check . && uv run ruff format . # lint + 格式
uv run mypy wxsp                           # 类型检查(strict)
uv run wxsp doctor                         # 健康检查(配置/DB/cookie/NAS/飞书)
uv run wxsp run --task-id 42 --dry-run     # 单条 dry-run(验证发布步骤)
uv run wxsp web                            # 起 Web UI
```

- pre-commit(ruff + ruff-format + mypy)已装 hook,**禁止 `--no-verify` 绕过**。
- `@pytest.mark.integration` 标记的测试需要真浏览器 + 真账号,CI 跳过、手动跑。

## 架构分层(改代码前必看)

依赖方向:**`base`(类型)← `runner`(编排)← 各平台 adapter ← `publisher`(路由)**。

| 层 | 文件 | 职责 | 硬约束 |
|---|---|---|---|
| 身份元数据 | `wxsp/platform_meta.py` `REGISTRY` | 平台中文名/标题字数/login 检测/向导字段/指纹档位 | **纯数据,禁止 import 任何 wxsp 模块**(防循环依赖)。config/notify/browser/validator/setup 都读它。 |
| 共享类型 | `wxsp/platforms/base.py` | `PlatformPublisher` Protocol / `TaskBundle` / `PublishContext` / `PlatformSpec` | 只放类型,无业务逻辑。 |
| 共享编排 | `wxsp/platforms/runner.py` `run_publish()` | claim 抢锁 → 启浏览器 → 步骤回调 → 状态机 → 通知 → 飞书回写 | 所有"plumbing"只此一处,**平台 adapter 不要复制状态机**(历史 tencent/taobao 各抄一份已产生漂移)。 |
| 平台 adapter | `wxsp/platforms/X.py` + `X_selectors.py` | 浏览器交互:`_pre_publish` / `_post_publish` / `login` | **只做浏览器交互**。DB 写、通知、飞书回写都由 runner 统一处理。 |
| 路由 | `wxsp/publisher.py` `_PUBLISHERS` | 按 `task.platform` 分发到 adapter | 加平台在此注册一行 `"X": XPublisher()`。 |

### 新增平台(5 步操作手册,详见 CLAUDE.md §新增一个平台)

1. `platforms/X_selectors.py`:URL + 登录判定 + 各步元素 + `RISK_CONTROL_KEYWORDS`/`SUCCESS_INDICATORS`。优先语义化选择器(`text=`/`role=`/`placeholder=`)。
2. `platforms/X.py`:`_pre_publish`(开页→上传→填表→风控探测,**止于点发布前**)、`_post_publish`(点发布→等成功)、`X_SPEC = PlatformSpec(...)`、`class XPublisher`(`publish_one` 转调 `run_publish`、`login` 开浏览器等扫码且**不接 Settings**)。
3. `publisher._PUBLISHERS` 注册一行。
4. `platform_meta.REGISTRY` 加一条 `PlatformMeta`。
5. `wxsp setup` 生成 `config_X.yaml`。

> config / notify / browser / validator / setup / cli **都不用改**——全从 REGISTRY 读。

### 加平台时容易漏的多处注册

新错误类型必须三处全到,否则告警显示"未知错误":`errors.py`(继承 `PublisherError`)→ `errors._KIND_BY_TYPE` → `notify._ERROR_TYPE_CN`。
新步骤名 `ctx.last_step` 必须加进 `notify._STEP_CN`,否则告警步骤名漏成英文。

## 硬约束(违反会出事)

- **dry-run 红线**:点发布的动作只能写在 `post_publish`;`pre_publish` 之后是 dry-run gate,`--dry-run` 在那里截断。写错位置会让 dry-run 真发出去。
- **headless 禁用**:视频号系扫码和发布都 `headless=false`,弹真窗口;不要在无桌面环境跑。
- **指纹透传**:视频号风控按设备指纹判多账号;`publisher`/`login`/`doctor` 全都要传 `account_id` 才能拿到确定性指纹,漏传会被判定异常设备踢登录。
- **DB 状态变更**走 `db.transition_task(task_id, status, **fields)`,让幂等锁有唯一加锁点;业务模块拿到 ORM 对象后只读。
- **cookie 由 persistent context 自动持久化**,不单独维护 cookie.json(needs_fingerprint=False 的平台除外,用 cookies.json 显式持久化)。

## 编码约定

- 日志统一 `from loguru import logger`,**业务代码禁用 `print`**。
- 路径走 `pathlib.Path`,**禁止字符串拼 `/`**(跨平台保证)。
- 所有 IO/网络/页面操作必须有超时,**不要写无限等待**。
- 外部输入(yaml、飞书数据、API 请求)用 Pydantic 校验。
- API 路由薄:`api/routes_X.py` 调 `wxsp/X.py` 同名模块函数。
- commit message 用 Conventional Commits(`feat:`/`fix:`/`chore:` ...)。
- Windows 中文版 stdout 默认 cp936,CLI 入口 `_force_utf8_stdout()` 已处理;新增含 emoji 的输出不要绕过它。

## 安全 / 禁止

- ❌ 不存储账号密码,只存 cookie;❌ 不把视频或 cookie 上传第三方
- ❌ 飞书 `app_secret` / 企微 webhook 用环境变量(`FEISHU_APP_SECRET`/`WECOM_BOT_WEBHOOK`),**不进 yaml**
- ❌ `data/` 和 `logs/` 全 gitignore,不要提交
- ❌ 不硬编码账号/Token;❌ 不静默忽略 `risk_control` 类错误

## Web UI 约定(FastAPI + Jinja2 + HTMX)

- HTMX 同步操作失败时:路由返 `200` + 失败片段 + 响应头 `HX-Trigger: {"opError": {...}}`,由 `base.html` 监听器弹全局 modal;**不走 4xx/5xx**(避免 htmx 默认 `responseError` 路径漏 swap)。
- 同步阻塞型路由(sync / run-today)用 `threading.Lock` 串行,抢不到锁返"正在跑中"。
- SSE(`/api/logs/stream`、`/api/tasks/stream`)做日志流和任务状态推送。
- 平台切换:`?platform=` 查询参数过滤;`platform_context` 中间件对无参 GET 重定向到默认平台(`data/default_platform` 存全局默认)。

## 配置

- 每平台独立 `config_{platform}.yaml`;`load_settings(platform=...)` / `get_config_path(platform)`。
- Settings 模型扁平(无平台嵌套);模板见 `config.example.yaml`。
- 敏感字段用 `${ENV_VAR}` 引用。

## 关键文档(改敏感区前先读)

- [CLAUDE.md](CLAUDE.md) — 权威设计:核心约束、发布 21 步、错误分类与重试、告警规则
- [docs/superpowers/specs/2026-05-12-wxsp-design.md](docs/superpowers/specs/2026-05-12-wxsp-design.md) — 总体设计 + 12 个 milestone 验收标准
- [docs/superpowers/specs/](docs/superpowers/specs/) — 各平台接入设计文档(改某平台先读对应的)
- [docs/desktop-packaging.md](docs/desktop-packaging.md) — macOS/Windows 打包
