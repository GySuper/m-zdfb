# M3 飞书同步 + Validator 设计文档

> 本文档是整体设计 [`2026-05-12-wxsp-design.md`](2026-05-12-wxsp-design.md) 的 M3 milestone 细化版,**对上层设计的偏离会在 §7 列明**。

**Goal**:从飞书 Bitable 拉"状态=待入库"的行,逐行校验后入 DB,不合规行回写飞书。完成后 `wxsp sync` 命令可独立运行,为 M5/M6 的执行链提供合规任务源。

**Tech Stack**:`lark-oapi`(飞书官方 Python SDK)+ Pydantic Settings(已就绪)+ SQLModel(已就绪)+ pathlib + pytest。

---

## 1. M3 范围

### 1.1 交付物

| 文件 | 内容 |
|------|------|
| `wxsp/feishu.py` | `make_client / fetch_pending_rows / writeback_row` 三个无状态函数,内置 3 次指数退避 |
| `wxsp/validator.py` | `validate(row, *, config, now, nas_finder) -> ValidationResult` 纯函数 + 数据类 |
| `wxsp/nas.py` | `find_video / find_cover` 两个函数(M3 部分;M4 补 `stage_to_tmp / cleanup_tmp` + doctor 检查) |
| `wxsp/cli.py` 改动 | `wxsp sync` 命令实现(此前为占位) |
| `pyproject.toml` 改动 | 加 `lark-oapi` 依赖 |
| `config.example.yaml` 改动(可选) | 给 `feishu` 节加示例占位 |

### 1.2 测试文件

- `tests/test_feishu.py` — mock `lark.Client` + 手写 JSON fixture
- `tests/test_validator.py` — 纯函数单测,fake nas_finder
- `tests/test_nas.py` — `tmp_path` 造目录树
- `tests/test_cli_sync.py` — 编排集成测试(monkeypatch `feishu` module)

### 1.3 不动的

- `wxsp/config.py`(`FeishuConfig` / `FeishuFieldMap` 已就绪,M0 完成)
- `wxsp/models.py`(`Video` / `Task` / 状态机已就绪,M1 完成)
- `wxsp/db.py`(沿用 `get_session` + 直接 `session.add`,不加新方法)

### 1.4 不做的

- **账号 round-robin**:账号字段在飞书侧改为**必填**(见 §7.1)
- **`daily_limit` 强制**:用户决定 M3 不限制,保留字段备用(见 §7.2)
- **封面图片比例校验**:需要 PIL/Pillow,延后到 M4/M5
- **NAS 不可达探测**:`rglob` 静默返回空,validator 报"文件不存在";细分由 M4 doctor 兜底
- **飞书状态回写"已计划/发布中/已发布"**:M3 只回写"失败"(校验失败时);发布相关状态由 M5/M7 写
- **`stage_to_tmp / cleanup_tmp / NAS doctor`**:M4 交付

---

## 2. 飞书 API 接入(`wxsp/feishu.py`)

### 2.1 凭证

`config.feishu.app_id` / `app_secret` 由 Pydantic Settings 加载;`app_secret` 通过 `${FEISHU_APP_SECRET}` 在 yaml 引用 env 变量(`config.py` 第 123 行的 `_ENV_PATTERN` 已实现展开)。

`config.example.yaml` 给占位:

```yaml
feishu:
  enabled: true
  app_id: cli_xxxxxxxxxx
  app_secret: ${FEISHU_APP_SECRET}
  bitable:
    app_token: xxxxxxxxxxxx
    table_id: tblxxxxxxxx
```

### 2.2 函数签名

```python
@dataclass(frozen=True)
class BitableRow:
    record_id: str
    fields: dict[str, Any]            # 飞书原字段名 → 值(未翻译)

class FeishuApiError(Exception):
    """3 次重试均失败后向上抛。"""

def make_client(app_id: str, app_secret: str) -> lark.Client: ...

def fetch_pending_rows(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    status_field: str,                # field_map.status,默认 "状态"
    status_pending_value: str = "待入库",
) -> list[BitableRow]:
    """拉所有 status_field=status_pending_value 的行,自动翻页直到 has_more=False。
    内置 3 次指数退避 (2s/4s/8s);3 次都失败抛 FeishuApiError。"""

def writeback_row(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],           # 飞书原字段名(caller 用 field_map 翻译过)
) -> None:
    """同样的 3 次退避。"""
```

### 2.3 重试细节

- 退避序列:`2 ** attempt`(0/1/2 → 1s/2s/4s);用 `time.sleep` 同步阻塞(sync 命令本身就是阻塞流程)
- 重试触发条件:`lark.LarkException` 的所有子类
- 非重试错误(`ValueError`、`TypeError` 等)直接向上抛,不掩盖

### 2.4 `lark-oapi` 调用形态

`fetch_pending_rows` 内部用 Bitable 的 `list_app_table_record` API,带 `filter` 参数过滤"状态=待入库"。具体 payload 在实现期 prompt 给 implementer subagent;此处不固化,因为 lark-oapi 的 builder API 可能随版本变化。

`writeback_row` 用 `update_app_table_record`。

---

## 3. NAS 文件检索(`wxsp/nas.py`,M3 部分)

### 3.1 函数签名

```python
def find_video(filename: str, *, search_root: Path) -> Path:
    """递归找 filename;多匹配取 mtime 最新;0 匹配 raise FileNotFoundError。"""

def find_cover(filename: str, *, search_root: Path) -> Path:
    """同 find_video。"""
```

### 3.2 实现核心

```python
def _find(filename: str, search_root: Path) -> Path:
    matches = list(search_root.rglob(filename))
    if not matches:
        raise FileNotFoundError(filename)
    return max(matches, key=lambda p: p.stat().st_mtime)

def find_video(filename, *, search_root): return _find(filename, search_root)
def find_cover(filename, *, search_root): return _find(filename, search_root)
```

### 3.3 边界

- 不做扩展名/大小/比例校验(由 validator 负责)
- 不接 config(纯函数,显式传 `search_root`)
- 异常用标准 `FileNotFoundError`,不引入自定义异常
- 全程 `pathlib.Path`,不拼字符串 `/`

---

## 4. Validator(`wxsp/validator.py`)

### 4.1 数据类型

```python
class NasFinder(Protocol):
    def find_video(self, filename: str) -> Path: ...
    def find_cover(self, filename: str) -> Path: ...

@dataclass(frozen=True)
class FieldError:
    field: str                        # 飞书侧字段中文名(已用 field_map 翻译)
    message: str                      # 人话错误描述

@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    # 通过时填:
    video_path: Path | None = None
    cover_path: Path | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    topic: str | None = None
    original_claim: bool = False
    account_id: str | None = None
    execute_date: date | None = None
    publish_at: datetime | None = None
    # 失败时填:
    errors: list[FieldError] = field(default_factory=list)
```

### 4.2 签名

```python
def validate(
    row: BitableRow,
    *,
    config: Settings,
    now: datetime,
    nas_finder: NasFinder,
    active_account_ids: set[str],     # 调用方一次性查 DB 后传入,validator 自己不持 session
) -> ValidationResult:
```

`now` / `nas_finder` / `active_account_ids` 全部注入,让 `validate` 是纯函数,测试零依赖(尤其不需要 DB session)。`active_account_ids` 由 `wxsp sync` 在循环外一次 `select(Account.id).where(Account.is_active == True)` 查出来,循环内复用。

### 4.3 校验规则

所有字段**独立校验、错误全部收集**,不在第一个错就 return。

| 字段 | 规则 | 失败信息示例 |
|------|------|-----------|
| 标题 | 必填,16-30 Unicode 字符 | `标题: 12 字(要求 16-30 字)` |
| 标签 | 可空;≤ 5 个 | `标签: 7 个(最多 5 个)` |
| 视频文件 | 必填;`nas_finder.find_video` 找到;扩展名 ∈ {`.mp4`, `.mov`}(大小写不敏感);文件大小 ≤ 4 × 1024³ 字节(4 GiB) | `视频文件: 未在 '<video_search_root>' 下找到 'xxx.mp4'` / `视频文件: 不支持的扩展名 '.avi'(允许 .mp4/.mov)` / `视频文件: 4.8 GiB 超出 4 GiB 上限` |
| 封面文件 | 可空;填了 → `find_cover` 找到。**M3 不校验比例** | `封面文件: 未在 '<cover_search_root>' 下找到 'a.jpg'` |
| 合集 | 可空,不校验 | — |
| 原创 | bool,空 → False | — |
| 账号 | **必填**;在 DB `Account` 表存在且 `is_active=True`(忽略 `paused_until`——临时暂停由 worker 在执行时跳过,不在入库阶段拦) | `账号: 未指定` / `账号: 'xxx' 不存在` / `账号: 'xxx' 已停用` |
| 执行日期 | 必填,date 类型 | `执行日期: 未指定` |
| 定时发布时间 | 必填;`[now+30min, now+14d]` 区间;`date(publish_at) ≥ execute_date` | `定时发布时间: 2026-05-12 09:00 早于 now+30min` / `定时发布时间: 日期 2026-05-13 早于执行日期 2026-05-14` |

### 4.4 字段读取 + 类型转换

validator 内部用 `config.feishu.field_map` 把内部字段名(`video_file`, `title`, ...)翻译成飞书原字段名,再从 `row.fields[原字段名]` 取值。`FieldError.field` 填**飞书原字段名**(运营在飞书侧能直接对照)。

**飞书字段值的类型规约**(基于 Bitable API 实测):

| 字段 | 飞书 API 返回类型 | validator 处理 |
|------|-----------------|--------------|
| 单行/多行文本 | `str` | 直接用,空字符串 = 未填 |
| 多选(标签) | `list[dict]`,每项 `{"text": ...}` | 取 `[x["text"] for x in value]` |
| 单选(账号/合集) | `dict {"text": ...}` 或字符串 | 兼容两种取 `text` |
| 复选框(原创) | `bool` | 直接用,None → False |
| 日期(执行日期) | `int` 毫秒级 Unix timestamp(UTC) | `datetime.fromtimestamp(ms/1000, tz=UTC).astimezone(ZoneInfo("Asia/Shanghai")).date()` |
| 日期时间(定时发布时间) | `int` 毫秒级 Unix timestamp(UTC) | `datetime.fromtimestamp(ms/1000, tz=UTC).astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)` → naive Asia/Shanghai(与 `models.py` 时间约定一致) |
| URL | `dict {"link": ..., "text": ...}` | 取 `link`(回写时反过来) |

**关键**:`publish_at` 经过时区换算后落地为 **naive Asia/Shanghai**(`tzinfo=None`),严格遵循 `models.py` 第 5-9 行的时间约定。

---

## 5. `wxsp sync` 编排

### 5.1 命令签名

```bash
wxsp sync [--dry-run]
```

`--dry-run`:走完 fetch + validate,**不写 DB、不回写飞书**,只打印计划做的事。

### 5.2 顺序

```
1. 预检
   - config.feishu.enabled = False → 打印"飞书未启用",exit 0
   - DB 文件不存在 → 提示 wxsp init,exit 64 (EX_USAGE)

2. 拉数据
   - client = make_client(app_id, app_secret)
   - rows = fetch_pending_rows(client, ...)
   - FeishuApiError 抛出 → 打印 + exit 70 (EX_SOFTWARE)

3. 逐行处理(顺序、无并发):
   nas_finder = ... (用 config.paths.{video,cover}_search_root)
   active_account_ids = {a.id for a in session.exec(select(Account).where(Account.is_active == True))}
   accepted, rejected, skipped_existing = [], [], []
   for row in rows:
       if session.get(Video, row.record_id):
           skipped_existing.append(row.record_id)
           continue
       result = validate(row, config=settings, now=now, nas_finder=nas_finder,
                         active_account_ids=active_account_ids)
       if result.ok:
           video = Video(id=row.record_id, ...)
           task = Task(video_id=row.record_id, account_id=result.account_id, ..., status=PENDING)
           try:
               session.add_all([video, task])
               session.commit()
           except IntegrityError:  # unique(video_id) 双保险
               session.rollback()
               skipped_existing.append(row.record_id)
           else:
               accepted.append((row.record_id, task.id))
       else:
           rejected.append((row.record_id, result.errors))

4. 回写飞书(--dry-run 跳过;config.feishu.sync.write_back_enabled=False 也跳过):
   - accepted: status="已计划"
   - rejected: status="失败" + error_message=格式化错误
   - skipped_existing: error_message="已有历史任务,请在 Web UI 重试" (不动 status)
   - writeback 单行失败 → 打印告警,继续下一行(不抛)

5. 打印汇总:
   飞书同步完成
     拉取: 10
     入库: 5
     拒绝: 4(已回写)
     已存在跳过: 1
```

### 5.3 错误信息格式

```
校验失败,请修复后将"状态"改回"待入库":
· 视频文件: 未在 '/Volumes/NAS/wxsp/videos' 下找到 '国庆01.mp4'
· 标题: 12 字(要求 16-30 字)
· 定时发布时间: 2026-05-13 09:00 早于 now+30min
```

实现:`"\n".join(f"· {e.field}: {e.message}" for e in errors)` 拼前面那一句。

### 5.4 Exit codes

| 场景 | exit code |
|------|-----------|
| 正常 | 0 |
| 飞书 API 持续失败(3 次都失败) | 70 (EX_SOFTWARE) |
| DB 文件不存在 / config 不合法 | 64 (EX_USAGE) |

---

## 6. 测试策略

### 6.1 单元测试

| 模块 | 覆盖 |
|------|------|
| `test_nas.py` | (a) 单匹配 (b) 多匹配取 mtime 最新(用 `os.utime` 设定) (c) 0 匹配 → `FileNotFoundError` (d) 跨子目录 |
| `test_validator.py` | 每条字段规则 ≥ 1 个 fail case + 1 个 happy case;边界:title=15/16/30/31 字、publish_at=now+29min/30min/14d/14d+1min;扩展名大小写(`.MP4`、`.Mov` 应通过);多个错误同时收集 |
| `test_feishu.py` | (a) 分页:第一次 has_more=True + page_token,第二次 has_more=False (b) 重试:前 2 次抛 LarkException 第 3 次成功 (c) 3 次都失败抛 FeishuApiError (d) writeback payload 结构 |

### 6.2 集成测试(M3 验收用)

`tests/test_cli_sync.py`:

**Fixture**:
- 10 行飞书 JSON,5 合规 + 5 不合规(每种校验失败覆盖一次)
- `tmp_path` 造 NAS 树,把合规行引用的 5 个视频文件放进去
- `tmp_path` 造空 DB(用 M1 的 `init_db`)
- monkeypatch `wxsp.feishu.make_client` 返回 `FakeClient`,FakeClient 内部消费 fixture JSON 实现 fetch / 记录 writeback

**断言**:
- 跑 `wxsp sync` → exit 0
- DB 里有 5 个 Video + 5 个 Task(status=pending)
- FakeClient 收到 10 次 writeback:5 次 status="已计划",5 次 status="失败" + error_message 非空
- 第二次跑 `wxsp sync` → 0 入库,FakeClient 收到 5 次 skipped_existing 回写(error_message 含"已有历史任务"),DB 不变

### 6.3 不做的

- **不**真打飞书 API(M3 不需要 live API key)
- **不** mock `validate`(纯函数,直接调真的)
- **不**做 perf 测试

---

## 7. M3 对整体设计的偏离

本节列出 M3 brainstorm 阶段决定的、与 [`2026-05-12-wxsp-design.md`](2026-05-12-wxsp-design.md) 不一致的地方。整体 design 后续会同步更新。

### 7.1 账号字段改飞书侧必填

整体 design §5.5 / CLAUDE.md "字段表"说账号空 → round-robin 分配。M3 决定**改为飞书侧必填**:

- 理由:用户明确"不允许账号为空"
- 影响:validator 拒绝账号为空的行;scheduler 不再需要 round-robin 实现
- 整体 design 待更新:§5.5 字段表、CLAUDE.md "账号分配" 节

### 7.2 `daily_limit` 不在 M3 强制

整体 design / CLAUDE.md 暗示 validator 应拒绝当日超 `daily_limit` 的行。M3 决定**不强制**:

- 理由:用户明确"当天有多少条发多少条,这个不做限制"
- 影响:`Account.daily_limit` 字段保留,但 validator 不校验
- 整体 design 待更新:§5.5 校验规则、CLAUDE.md "账号分配 + 调度" 节

### 7.3 NAS 函数提前到 M3

整体 design §7 把 `nas.py` 完整交付列在 M4。M3 决定:

- `find_video / find_cover` 在 M3 落地(validator 依赖)
- `stage_to_tmp / cleanup_tmp / NAS doctor 检查` 仍在 M4

### 7.4 飞书重新提交的处理

整体 design 未明确说"已存在 record_id 的处理"。M3 决定:

- DB 已有同 record_id 的 Video → 跳过,不动 DB
- 飞书回写 `error_message="已有历史任务(...),请在 Web UI 重试"`,**不动 status 字段**
- 每次 sync 看到同样的行就再回写一次同样提示(代价可忽略)

### 7.5 重试就近实现,不等 M5 retry.py

整体 design §5.3 把所有重试统一到 `retry.py`(M5)。M3 决定:

- feishu.py 的 3 次指数退避**就近写在 fetch/writeback 函数体里**(`time.sleep + try/except`)
- 不引入跨模块 retry 装饰器,避免 M3 依赖未交付的 M5 代码
- M5 写完 retry.py 时再统一收编

---

## 8. 验收标准

按整体 design §7 "每个 milestone 完成必做":

1. **fixture 测试**(对应整体 design 的 M3 验收):
   - 10 行 (5 合规 5 不合规),`wxsp sync` 后 DB 5 条 Video + 5 条 Task
   - 飞书 5 行(不合规)收到 `error_message` 回写,5 行(合规)状态改"已计划"
2. **重复 sync**:第二遍 0 入库 + 5 个 "已存在" 回写
3. **手工冒烟**(可选,需 live 飞书):用一张测试表跑 `wxsp sync`,人工确认飞书侧"错误信息"字段渲染正常
4. **全量 pytest 绿**
5. **ruff + mypy 全绿**(pre-commit)
6. `git commit` (Conventional Commits) + 给用户演示
