# M3 飞书同步 + Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal**:实现 `wxsp sync` 命令——从飞书 Bitable 拉"状态=待入库"的行,逐行校验后入 DB,不合规行回写错误,为 M5/M6 提供合规任务源。

**Architecture**:三层解耦:`wxsp/feishu.py` 提供无状态的 lark-oapi 包装(`make_client / fetch_pending_rows / writeback_row`,内置 3 次指数退避);`wxsp/validator.py` 提供纯函数 `validate(row, *, config, now, nas_finder, active_account_ids)`,字段独立校验并收集所有错误;`wxsp/nas.py` 提供 `find_video / find_cover` 文件检索(M3 部分,M4 补 stage/cleanup)。CLI handler `wxsp sync` 把三者串起来:预检 → 拉数据 → 逐行 validate → DB 写入或飞书回写错误 → 汇总。

**Tech Stack**:Python 3.10+,`lark-oapi`(飞书官方 SDK),SQLModel,Typer,pytest + `typer.testing.CliRunner`。

详细设计见 [`docs/superpowers/specs/2026-05-12-m3-feishu-sync.md`](../specs/2026-05-12-m3-feishu-sync.md)。

---

## M3 Scope and Constraints

**In scope**:
- `wxsp/feishu.py` — `make_client / fetch_pending_rows / writeback_row` + `BitableRow / FeishuApiError`
- `wxsp/validator.py` — `validate` + `FieldError / ValidationResult / NasFinder` protocol
- `wxsp/nas.py` — `find_video / find_cover`(M3 部分)
- `wxsp/cli.py` — 替换 `sync` 占位实现
- `pyproject.toml` — 加 `lark-oapi` 依赖

**Out of scope(后续 milestone)**:
- `nas.stage_to_tmp / cleanup_tmp` + NAS doctor 检查(M4)
- 封面图片比例校验(M4/M5,需 PIL)
- 飞书状态字段的"已计划/发布中/已发布"业务态回写(M5+M7)
- `daily_limit` 强制(用户决定不限制,字段保留)
- 账号 round-robin(账号字段改飞书侧必填)
- M5 的 `retry.py`(M3 重试就近写在 feishu.py)
- `wxsp run --today` / daemon 中自动调用 `sync_now`(M6)

**Hard constraints**:
- **不真打飞书 API**:测试全部用 mock `lark.Client`(M3 没有 live API key,也不应该需要)
- **TDD**:每个任务两次 commit—RED 测试先行 + GREEN 实现跟上
- **跨平台**:全部 `pathlib.Path`,禁止字符串拼 `/`
- **时区**:`publish_at` / `execute_date` 解析后落 naive `Asia/Shanghai`,与 `models.py` 时间约定一致
- **ruff + mypy 全绿**(pre-commit 强制)
- **Conventional Commits**

**M3 Acceptance(spec §8)**:
1. fixture 10 行(5 合规 5 不合规),跑 `wxsp sync` → DB 5 个 Video+Task,飞书 10 个回写(5 已计划 / 5 失败)
2. 重复跑 → 0 入库,5 个"已存在"回写
3. 全量 `pytest` 绿,ruff + mypy 绿
4. `git commit` (Conventional Commits) + 给用户演示

---

## File Structure

```
wxsp/
├── feishu.py            ← Task 3+4+5 (make_client/fetch/writeback + 退避)
├── nas.py               ← Task 2     (find_video/find_cover)
├── validator.py         ← Task 6+7+8+9 (types + 各类规则 + 多错收集)
├── cli.py               ← Task 10    (sync handler)
├── config.py            ← (M0 已就绪,不动)
├── models.py            ← (M1 已就绪,不动)
├── db.py                ← (M1 已就绪,不动)
└── ...

tests/
├── test_nas.py             ← Task 2  (NEW)
├── test_feishu.py          ← Task 3+4+5 (NEW)
├── test_validator.py       ← Task 6+7+8+9 (NEW)
├── test_cli_sync.py        ← Task 10 (NEW)

pyproject.toml              ← Task 1  (加 lark-oapi)
```

---

## 任务依赖图

```
Task 1 (lark-oapi dep)
   │
   ├── Task 2 (nas.find_*)
   │      │
   │      └── Task 8 (validator: 文件 rules) ──┐
   │                                           │
   ├── Task 3 (feishu types)                   │
   │      │                                    │
   │      ├── Task 4 (feishu.fetch_pending_rows) ──┐
   │      │                                        │
   │      └── Task 5 (feishu.writeback_row)        │
   │             │                                 │
   │             └─────────────────┐               │
   │                               │               │
   ├── Task 6 (validator types) ───┼── Task 7 (validator: 文本/选择 rules)
   │                               │               │
   │                               │   Task 8 (validator: 文件 rules)
   │                               │               │
   │                               │   Task 9 (validator: 时间/账号/多错)
   │                               │               │
   │                               └── Task 10 (wxsp sync CLI) ──→ Task 11 (M3 acceptance)
```

---

### Task 1: 加 `lark-oapi` 依赖

**Files**:
- Modify: `pyproject.toml`

**Why**:M3 全部模块都依赖 `lark-oapi`,先把依赖装上,后续任务无须再动 `uv.lock`。

- [ ] **Step 1: 加依赖**

修改 `pyproject.toml` 第 14 行后追加 `lark-oapi`:

```toml
dependencies = [
    "typer>=0.12.0",
    "pydantic>=2.6.0",
    "pydantic-settings>=2.2.0",
    "pyyaml>=6.0",
    "loguru>=0.7.2",
    "sqlmodel>=0.0.21",
    "patchright>=1.40.0",
    "lark-oapi>=1.4.0",
]
```

- [ ] **Step 2: 同步 lock + 验证可 import**

```bash
uv sync
uv run python -c "import lark_oapi; print(lark_oapi.__version__)"
```

Expected:打印版本号,无错误。

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add lark-oapi dependency for feishu Bitable sync"
```

---

### Task 2: `nas.find_video / find_cover`

**Files**:
- Modify: `wxsp/nas.py`(目前只有 docstring 占位)
- Test: `tests/test_nas.py`(NEW)

**Why**:validator 的视频/封面 file rule 直接依赖这两个函数。先做无依赖的底层。

- [ ] **Step 1: 写失败测试 (RED)**

`tests/test_nas.py`:

```python
"""nas.find_video / find_cover tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wxsp.nas import find_cover, find_video


def test_find_video_single_match(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    (root / "a").mkdir(parents=True)
    target = root / "a" / "国庆01.mp4"
    target.write_bytes(b"x")

    result = find_video("国庆01.mp4", search_root=root)
    assert result == target


def test_find_video_multi_match_returns_newest_mtime(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    (root / "old").mkdir(parents=True)
    (root / "new").mkdir(parents=True)
    older = root / "old" / "dup.mp4"
    newer = root / "new" / "dup.mp4"
    older.write_bytes(b"x")
    newer.write_bytes(b"y")
    # 把 older 的 mtime 强制设到过去
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    result = find_video("dup.mp4", search_root=root)
    assert result == newer


def test_find_video_no_match_raises(tmp_path: Path) -> None:
    root = tmp_path / "videos"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        find_video("missing.mp4", search_root=root)


def test_find_cover_same_semantics(tmp_path: Path) -> None:
    root = tmp_path / "covers"
    (root / "deep" / "sub").mkdir(parents=True)
    target = root / "deep" / "sub" / "cover.jpg"
    target.write_bytes(b"x")

    result = find_cover("cover.jpg", search_root=root)
    assert result == target


def test_find_video_empty_root_raises(tmp_path: Path) -> None:
    # search_root 不存在 → rglob 抛 OSError 或返回空,统一表现为 FileNotFoundError
    root = tmp_path / "nonexistent"
    with pytest.raises(FileNotFoundError):
        find_video("any.mp4", search_root=root)
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_nas.py -v
```

Expected:全部 fail with `ImportError: cannot import name 'find_video' from 'wxsp.nas'`。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_nas.py
git commit -m "test: add failing tests for nas.find_video / find_cover"
```

- [ ] **Step 4: 最小实现 (GREEN)**

修改 `wxsp/nas.py`:

```python
"""NAS 文件检索 + stage_to_tmp + cleanup_tmp(M3 含 find_*,M4 补 stage/cleanup)。"""

from __future__ import annotations

from pathlib import Path


def _find(filename: str, search_root: Path) -> Path:
    """通用实现:在 search_root 下递归找 filename,多匹配取 mtime 最新。"""
    if not search_root.exists():
        raise FileNotFoundError(filename)
    matches = list(search_root.rglob(filename))
    if not matches:
        raise FileNotFoundError(filename)
    return max(matches, key=lambda p: p.stat().st_mtime)


def find_video(filename: str, *, search_root: Path) -> Path:
    """在 search_root 下递归找视频文件;多匹配取 mtime 最新;0 匹配 raise FileNotFoundError。"""
    return _find(filename, search_root)


def find_cover(filename: str, *, search_root: Path) -> Path:
    """在 search_root 下递归找封面文件;语义同 find_video。"""
    return _find(filename, search_root)
```

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_nas.py -v
uv run ruff check wxsp/nas.py tests/test_nas.py
uv run mypy wxsp/nas.py
```

Expected:5 个测试全 PASS;ruff/mypy 无 error。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/nas.py
git commit -m "feat(nas): add find_video / find_cover with mtime-newest tiebreak"
```

---

### Task 3: `feishu` 模块类型 — `BitableRow` + `FeishuApiError`

**Files**:
- Modify: `wxsp/feishu.py`
- Test: `tests/test_feishu.py`(NEW)

**Why**:后续两个函数的签名都要用到 `BitableRow`,先把类型立住,避免循环改。

- [ ] **Step 1: 写失败测试 (RED)**

`tests/test_feishu.py`:

```python
"""wxsp.feishu types and Bitable client wrappers."""

from __future__ import annotations

import pytest

from wxsp.feishu import BitableRow, FeishuApiError


def test_bitable_row_is_frozen_dataclass() -> None:
    row = BitableRow(record_id="rec123", fields={"标题": "abc"})
    assert row.record_id == "rec123"
    assert row.fields == {"标题": "abc"}
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        row.record_id = "other"  # type: ignore[misc]


def test_feishu_api_error_is_exception() -> None:
    err = FeishuApiError("bitable timeout")
    assert isinstance(err, Exception)
    assert str(err) == "bitable timeout"
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_feishu.py -v
```

Expected:fail with `ImportError: cannot import name 'BitableRow' from 'wxsp.feishu'`。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_feishu.py
git commit -m "test: add failing tests for feishu BitableRow / FeishuApiError types"
```

- [ ] **Step 4: 最小实现 (GREEN)**

修改 `wxsp/feishu.py`:

```python
"""飞书 Bitable 拉取与回写(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 无状态函数 API:make_client / fetch_pending_rows / writeback_row
  - 3 次指数退避(1s/2s/4s)就近写在函数体内,不引入 M5 的 retry.py
  - BitableRow 只存 record_id + 原始 fields dict;字段语义解析交给 validator
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BitableRow:
    """单行飞书 Bitable 记录的最小封装。fields 用飞书原字段中文名作 key。"""

    record_id: str
    fields: dict[str, Any]


class FeishuApiError(Exception):
    """飞书 API 在 3 次指数退避后仍失败时抛出。"""
```

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_feishu.py -v
uv run ruff check wxsp/feishu.py tests/test_feishu.py
uv run mypy wxsp/feishu.py
```

Expected:2 个测试 PASS,无 lint error。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/feishu.py
git commit -m "feat(feishu): add BitableRow dataclass + FeishuApiError"
```

---

### Task 4: `feishu.make_client` + `fetch_pending_rows`(分页 + 重试)

**Files**:
- Modify: `wxsp/feishu.py`
- Test: `tests/test_feishu.py`(extend)

**Why**:这是 sync 命令拉数据的核心。重点验证分页和指数退避——这两个是 lark-oapi 实际可能踩坑的点。

**lark-oapi 接口提示**(实现期可用 Context7 验证最新版本):
- Client:`lark.Client.builder().app_id(app_id).app_secret(app_secret).build()`
- Search records:`client.bitable.v1.app_table_record.search(SearchAppTableRecordRequest)`
- Request builder: `SearchAppTableRecordRequest.builder().app_token(...).table_id(...).page_size(...).page_token(...).request_body(SearchAppTableRecordRequestBody.builder().filter(...).build()).build()`
- Response:`response.data.items: list[AppTableRecord]`,`response.data.has_more: bool`,`response.data.page_token: str | None`,`response.success() -> bool`
- 错误时 `response.code != 0`,抛出方式:`if not response.success(): raise FeishuApiError(f"...{response.code}: {response.msg}")`

注:`AppTableRecord` 有 `record_id: str` 和 `fields: dict[str, Any]`。
注:filter 用 `FilterInfo.builder().conditions([Condition.builder().field_name("状态").operator("is").value([status_pending_value]).build()]).conjunction("and").build()`,搜不到具体参数名时用 Context7 查 `lark-oapi` 文档。

- [ ] **Step 1: 写失败测试 (RED)**

在 `tests/test_feishu.py` 追加:

```python
from typing import Any

from wxsp.feishu import fetch_pending_rows, make_client


class _FakeResponse:
    """模拟 lark.Client 返回的 response 对象。"""

    def __init__(
        self, items: list[dict[str, Any]], has_more: bool, page_token: str | None = None,
        code: int = 0, msg: str = "",
    ) -> None:
        self.data = _FakeData(items, has_more, page_token)
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self.code == 0


class _FakeData:
    def __init__(self, items: list[dict[str, Any]], has_more: bool, page_token: str | None) -> None:
        self.items = [_FakeRecord(r["record_id"], r["fields"]) for r in items]
        self.has_more = has_more
        self.page_token = page_token


class _FakeRecord:
    def __init__(self, record_id: str, fields: dict[str, Any]) -> None:
        self.record_id = record_id
        self.fields = fields


class _FakeClient:
    """模拟 lark.Client,捕获请求并按预设响应序列返回。"""

    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.search_calls: list[Any] = []
        # client.bitable.v1.app_table_record.search(request) 这个调用链
        self.bitable = self
        self.v1 = self
        self.app_table_record = self

    def search(self, request: Any) -> _FakeResponse:
        self.search_calls.append(request)
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_make_client_builds_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """make_client 应能返回一个 lark.Client 实例(我们只验证它不爆)。"""
    client = make_client("cli_app_id", "secret_value")
    assert client is not None
    # 不深入断言 builder 内部状态,只确保返回值有 bitable.v1.app_table_record.search 调用链
    assert hasattr(client, "bitable")


def test_fetch_pending_rows_single_page() -> None:
    fake = _FakeClient([
        _FakeResponse(
            items=[
                {"record_id": "rec1", "fields": {"标题": "a"}},
                {"record_id": "rec2", "fields": {"标题": "b"}},
            ],
            has_more=False,
        ),
    ])
    rows = fetch_pending_rows(
        fake,  # type: ignore[arg-type]
        app_token="tbl_token",
        table_id="tblxxx",
        status_field="状态",
    )
    assert len(rows) == 2
    assert rows[0].record_id == "rec1"
    assert rows[0].fields == {"标题": "a"}
    assert rows[1].record_id == "rec2"
    assert len(fake.search_calls) == 1


def test_fetch_pending_rows_paginates() -> None:
    fake = _FakeClient([
        _FakeResponse(
            items=[{"record_id": "rec1", "fields": {}}],
            has_more=True,
            page_token="cursor_2",
        ),
        _FakeResponse(
            items=[{"record_id": "rec2", "fields": {}}],
            has_more=False,
        ),
    ])
    rows = fetch_pending_rows(
        fake,  # type: ignore[arg-type]
        app_token="t", table_id="t", status_field="状态",
    )
    assert [r.record_id for r in rows] == ["rec1", "rec2"]
    assert len(fake.search_calls) == 2


def test_fetch_pending_rows_retries_on_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: sleeps.append(s))

    fake = _FakeClient([
        RuntimeError("transient 1"),
        RuntimeError("transient 2"),
        _FakeResponse(items=[{"record_id": "rec1", "fields": {}}], has_more=False),
    ])
    rows = fetch_pending_rows(
        fake,  # type: ignore[arg-type]
        app_token="t", table_id="t", status_field="状态",
    )
    assert len(rows) == 1
    # 两次失败后第三次成功 → 应该 sleep 过 2 次(指数退避 1s/2s)
    assert sleeps == [1.0, 2.0]


def test_fetch_pending_rows_raises_after_3_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: None)

    fake = _FakeClient([
        RuntimeError("fail 1"),
        RuntimeError("fail 2"),
        RuntimeError("fail 3"),
    ])
    with pytest.raises(FeishuApiError) as exc_info:
        fetch_pending_rows(
            fake,  # type: ignore[arg-type]
            app_token="t", table_id="t", status_field="状态",
        )
    assert "fail 3" in str(exc_info.value) or "3 次" in str(exc_info.value)


def test_fetch_pending_rows_raises_on_api_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: None)

    fake = _FakeClient([
        _FakeResponse(items=[], has_more=False, code=999, msg="forbidden"),
        _FakeResponse(items=[], has_more=False, code=999, msg="forbidden"),
        _FakeResponse(items=[], has_more=False, code=999, msg="forbidden"),
    ])
    with pytest.raises(FeishuApiError):
        fetch_pending_rows(
            fake,  # type: ignore[arg-type]
            app_token="t", table_id="t", status_field="状态",
        )
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_feishu.py -v
```

Expected:`make_client / fetch_pending_rows` 相关 fail with ImportError。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_feishu.py
git commit -m "test: add failing tests for feishu make_client + fetch_pending_rows"
```

- [ ] **Step 4: 最小实现 (GREEN)**

在 `wxsp/feishu.py` 追加(import 部分需补 `time` 和 lark 相关):

```python
import time
from typing import Any

import lark_oapi as lark
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    Condition,
    FilterInfo,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
)

_RETRY_DELAYS = (1.0, 2.0)  # 退避序列;最后一次失败不 sleep,直接抛


def make_client(app_id: str, app_secret: str) -> lark.Client:
    """构建 lark-oapi 客户端;无缓存,sync 启动时新建。"""
    return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()


def fetch_pending_rows(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str = "待入库",
) -> list[BitableRow]:
    """拉所有 status_field=status_pending_value 的行,自动翻页。

    内置 3 次指数退避(1s/2s):前两次抛 Exception → sleep → 重试;第三次抛 → FeishuApiError。
    response.code != 0 也按一次失败计。
    """
    rows: list[BitableRow] = []
    page_token: str | None = None
    while True:
        response = _search_with_retry(
            client,
            app_token=app_token,
            table_id=table_id,
            status_field=status_field,
            status_pending_value=status_pending_value,
            page_token=page_token,
        )
        rows.extend(_to_bitable_row(item) for item in (response.data.items or []))
        if not response.data.has_more:
            return rows
        page_token = response.data.page_token


def _search_with_retry(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str,
    page_token: str | None,
) -> Any:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            response = client.bitable.v1.app_table_record.search(
                _build_search_request(
                    app_token=app_token,
                    table_id=table_id,
                    status_field=status_field,
                    status_pending_value=status_pending_value,
                    page_token=page_token,
                )
            )
            if not response.success():
                raise FeishuApiError(f"飞书 API 错误 code={response.code} msg={response.msg}")
            return response
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(f"飞书 fetch 重试 3 次仍失败: {last_err}") from last_err


def _build_search_request(
    *,
    app_token: str,
    table_id: str,
    status_field: str,
    status_pending_value: str,
    page_token: str | None,
) -> SearchAppTableRecordRequest:
    body = (
        SearchAppTableRecordRequestBody.builder()
        .filter(
            FilterInfo.builder()
            .conjunction("and")
            .conditions(
                [
                    Condition.builder()
                    .field_name(status_field)
                    .operator("is")
                    .value([status_pending_value])
                    .build()
                ]
            )
            .build()
        )
        .build()
    )
    builder = (
        SearchAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .page_size(100)
        .request_body(body)
    )
    if page_token is not None:
        builder = builder.page_token(page_token)
    return builder.build()


def _to_bitable_row(item: AppTableRecord) -> BitableRow:
    return BitableRow(record_id=item.record_id, fields=dict(item.fields or {}))
```

注意:`lark_oapi.api.bitable.v1` 的具体类名(`SearchAppTableRecordRequest` 等)需 implementer 用 Context7 验证最新版本——名字若有差异(如 `SearchAppTableRecordRequestBody`/`AppTableRecord`),按实际包路径修正即可,不改对外签名。

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_feishu.py -v
uv run ruff check wxsp/feishu.py tests/test_feishu.py
uv run mypy wxsp/feishu.py
```

Expected:7 个测试 PASS(`test_make_client_builds_client` + 6 个 fetch_pending_rows)。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/feishu.py
git commit -m "feat(feishu): implement make_client + fetch_pending_rows with retry"
```

---

### Task 5: `feishu.writeback_row`(重试)

**Files**:
- Modify: `wxsp/feishu.py`
- Test: `tests/test_feishu.py`(extend)

**Why**:回写错误信息 / 状态到飞书;同样 3 次指数退避。结构上和 fetch 对称,提取共用重试逻辑也好。

**lark-oapi 接口提示**:
- `client.bitable.v1.app_table_record.update(UpdateAppTableRecordRequest)`
- Request:`UpdateAppTableRecordRequest.builder().app_token(t).table_id(t).record_id(r).request_body(AppTableRecord.builder().fields({...}).build()).build()`

- [ ] **Step 1: 写失败测试 (RED)**

在 `tests/test_feishu.py` 追加:

```python
from wxsp.feishu import writeback_row


class _FakeClientForUpdate:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.update_calls: list[Any] = []
        self.bitable = self
        self.v1 = self
        self.app_table_record = self

    def update(self, request: Any) -> _FakeResponse:
        self.update_calls.append(request)
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_writeback_row_single_success() -> None:
    fake = _FakeClientForUpdate([
        _FakeResponse(items=[], has_more=False),
    ])
    writeback_row(
        fake,  # type: ignore[arg-type]
        app_token="t", table_id="t", record_id="rec1",
        fields={"状态": "失败", "错误信息": "标题: 12 字"},
    )
    assert len(fake.update_calls) == 1


def test_writeback_row_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: None)

    fake = _FakeClientForUpdate([
        RuntimeError("transient"),
        _FakeResponse(items=[], has_more=False),
    ])
    writeback_row(
        fake,  # type: ignore[arg-type]
        app_token="t", table_id="t", record_id="rec1",
        fields={"状态": "已计划"},
    )
    assert len(fake.update_calls) == 2


def test_writeback_row_raises_after_3_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wxsp.feishu.time.sleep", lambda s: None)

    fake = _FakeClientForUpdate([RuntimeError("f1"), RuntimeError("f2"), RuntimeError("f3")])
    with pytest.raises(FeishuApiError):
        writeback_row(
            fake,  # type: ignore[arg-type]
            app_token="t", table_id="t", record_id="rec1",
            fields={"状态": "失败"},
        )
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_feishu.py::test_writeback_row_single_success -v
```

Expected:fail with ImportError 或 AttributeError。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_feishu.py
git commit -m "test: add failing tests for feishu writeback_row with retry"
```

- [ ] **Step 4: 最小实现 (GREEN)**

在 `wxsp/feishu.py` import 部分追加:

```python
from lark_oapi.api.bitable.v1 import (
    AppTableRecord,
    Condition,
    FilterInfo,
    SearchAppTableRecordRequest,
    SearchAppTableRecordRequestBody,
    UpdateAppTableRecordRequest,
)
```

追加函数:

```python
def writeback_row(
    client: lark.Client,
    *,
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
) -> None:
    """回写指定 record 的指定字段。fields 已用飞书原字段名作 key。

    内置 3 次指数退避(1s/2s);3 次都失败 → FeishuApiError。
    """
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            response = client.bitable.v1.app_table_record.update(
                _build_update_request(
                    app_token=app_token, table_id=table_id, record_id=record_id, fields=fields,
                )
            )
            if not response.success():
                raise FeishuApiError(
                    f"飞书 update 错误 code={response.code} msg={response.msg}"
                )
            return
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(_RETRY_DELAYS[attempt])
    assert last_err is not None
    raise FeishuApiError(f"飞书 writeback 重试 3 次仍失败: {last_err}") from last_err


def _build_update_request(
    *, app_token: str, table_id: str, record_id: str, fields: dict[str, Any],
) -> UpdateAppTableRecordRequest:
    body = AppTableRecord.builder().fields(fields).build()
    return (
        UpdateAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .record_id(record_id)
        .request_body(body)
        .build()
    )
```

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_feishu.py -v
uv run ruff check wxsp/feishu.py tests/test_feishu.py
uv run mypy wxsp/feishu.py
```

Expected:全部测试 PASS。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/feishu.py
git commit -m "feat(feishu): implement writeback_row with retry"
```

---

### Task 6: validator 类型 — `FieldError / ValidationResult / NasFinder`

**Files**:
- Modify: `wxsp/validator.py`
- Test: `tests/test_validator.py`(NEW)

**Why**:类型先立,后续 rule 任务直接产生 `FieldError` 即可。

- [ ] **Step 1: 写失败测试 (RED)**

`tests/test_validator.py`:

```python
"""wxsp.validator types and rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from wxsp.validator import FieldError, NasFinder, ValidationResult


def test_field_error_is_frozen() -> None:
    err = FieldError(field="标题", message="12 字(要求 16-30 字)")
    assert err.field == "标题"
    assert err.message == "12 字(要求 16-30 字)"
    with pytest.raises(Exception):
        err.field = "x"  # type: ignore[misc]


def test_validation_result_ok_shape() -> None:
    result = ValidationResult(ok=True, title="abc" * 6)
    assert result.ok is True
    assert result.errors == []  # 默认空 list


def test_validation_result_fail_shape() -> None:
    result = ValidationResult(
        ok=False, errors=[FieldError(field="标题", message="12 字")],
    )
    assert result.ok is False
    assert len(result.errors) == 1


def test_nas_finder_is_protocol() -> None:
    """NasFinder 是 Protocol,任何提供 find_video/find_cover 的对象都满足。"""

    class _Stub:
        def find_video(self, name: str) -> Path:
            return Path("/dev/null")
        def find_cover(self, name: str) -> Path:
            return Path("/dev/null")

    finder: NasFinder = _Stub()
    assert finder.find_video("x").as_posix() == "/dev/null"
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_validator.py -v
```

Expected:fail with `ImportError: cannot import name 'FieldError' from 'wxsp.validator'`。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_validator.py
git commit -m "test: add failing tests for validator types (FieldError/ValidationResult/NasFinder)"
```

- [ ] **Step 4: 最小实现 (GREEN)**

修改 `wxsp/validator.py`:

```python
"""入库校验(纯函数)(M3)。

设计要点(详见 docs/superpowers/specs/2026-05-12-m3-feishu-sync.md):
  - 纯函数:validate(row, *, config, now, nas_finder, active_account_ids) -> ValidationResult
  - 字段独立校验,错误全部收集(不在第一个错就 return)
  - 时区:publish_at / execute_date 解析后落 naive Asia/Shanghai
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class FieldError:
    """单个字段的校验错误。field 填飞书原字段中文名(运营在飞书侧能直接对照)。"""

    field: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """validate() 的返回值。ok=True 时业务字段填充,errors 为空;ok=False 时反之。"""

    ok: bool
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
    errors: list[FieldError] = field(default_factory=list)


class NasFinder(Protocol):
    """validator 依赖的 NAS 检索接口。生产实现走 wxsp.nas;测试可直接造 stub。"""

    def find_video(self, filename: str) -> Path: ...
    def find_cover(self, filename: str) -> Path: ...
```

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_validator.py -v
uv run ruff check wxsp/validator.py tests/test_validator.py
uv run mypy wxsp/validator.py
```

Expected:4 个测试 PASS。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/validator.py
git commit -m "feat(validator): add FieldError / ValidationResult / NasFinder types"
```

---

### Task 7: validator 文本/选择/复选 rules + 多错收集 happy path

**Files**:
- Modify: `wxsp/validator.py`
- Test: `tests/test_validator.py`(extend)

**Why**:先把不依赖 NAS / 时间 / DB 的"纯字段规则"做完——标题、标签、合集、原创、账号 + 多错收集。
这部分占规则的一半。账号校验依赖 `active_account_ids: set[str]` 注入,不查 DB。

- [ ] **Step 1: 写失败测试 (RED)**

在 `tests/test_validator.py` 追加:

```python
from datetime import date, datetime

from wxsp.feishu import BitableRow
from wxsp.validator import validate

# config 我们构造一个最小的 stub,只填 validator 实际用到的字段
from wxsp.config import (
    AppConfig, FeishuConfig, FeishuBitableConfig, FeishuFieldMap, FeishuSyncConfig,
    MonitoringConfig, NotifiersConfig, PathsConfig, PublisherConfig, SchedulerConfig,
    Settings, WebUIConfig, WecomNotifierConfig,
)


def _make_settings(tmp_path: Path) -> Settings:
    """构造一个合法的 Settings,validator 实际只用到 feishu.field_map 和 paths.{video,cover}_search_root。"""
    return Settings(
        app=AppConfig(data_dir=tmp_path / "data", logs_dir=tmp_path / "logs", timezone="Asia/Shanghai"),
        paths=PathsConfig(
            nas_root=tmp_path / "nas",
            video_search_root=tmp_path / "nas" / "videos",
            cover_search_root=tmp_path / "nas" / "covers",
        ),
        accounts={},
        scheduler=SchedulerConfig(),
        publisher=PublisherConfig(),
        feishu=FeishuConfig(
            app_id="x", app_secret="y",
            bitable=FeishuBitableConfig(app_token="t", table_id="t"),
            field_map=FeishuFieldMap(),
            sync=FeishuSyncConfig(),
        ),
        monitoring=MonitoringConfig(
            notifiers=NotifiersConfig(wecom=WecomNotifierConfig(webhook="http://x")),
        ),
        webui=WebUIConfig(),
    )


class _StubNas:
    """默认 stub:find_video / find_cover 都抛 FileNotFoundError。每个测试按需 monkeypatch。"""

    def __init__(self) -> None:
        self.video_returns: dict[str, Path] = {}
        self.cover_returns: dict[str, Path] = {}

    def find_video(self, filename: str) -> Path:
        if filename in self.video_returns:
            return self.video_returns[filename]
        raise FileNotFoundError(filename)

    def find_cover(self, filename: str) -> Path:
        if filename in self.cover_returns:
            return self.cover_returns[filename]
        raise FileNotFoundError(filename)


def _make_happy_row(tmp_path: Path) -> BitableRow:
    """构造一个所有字段都填好的合规行;子测试按需修改某个字段。"""
    return BitableRow(
        record_id="rec_happy",
        fields={
            "标题": "这是一个测试标题视频内容",                              # 12 字 + 4 = 12 字!实际填 18 字
            "描述": "测试描述",
            "标签": [{"text": "标签1"}, {"text": "标签2"}],
            "封面文件": "",
            "合集": "测试合集",
            "原创": True,
            "账号": "account_a",
            "执行日期": _date_to_feishu_ms(date(2026, 5, 13)),
            "定时发布时间": _datetime_to_feishu_ms(datetime(2026, 5, 13, 14, 0)),
            "视频文件": "国庆01.mp4",
            "状态": "待入库",
        },
    )


def _date_to_feishu_ms(d: date) -> int:
    """date 转飞书返回的 ms timestamp(UTC 0 点)。"""
    from datetime import timezone
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _datetime_to_feishu_ms(dt: datetime) -> int:
    """naive Asia/Shanghai datetime → 飞书返回的 ms timestamp(UTC ms)。

    飞书侧用户填的是上海时间,API 返回 UTC ms。模拟时反向减 8 小时。
    """
    from datetime import timezone, timedelta
    # 把 naive 上海时间当作 UTC+8,转 UTC
    utc_dt = dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    return int(utc_dt.timestamp() * 1000)


def test_validate_title_too_short(tmp_path: Path) -> None:
    row = _make_happy_row(tmp_path)
    fields = dict(row.fields)
    fields["标题"] = "短标题"  # 3 字
    row = BitableRow(record_id=row.record_id, fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "x.mp4"
    (tmp_path / "x.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "标题" and "16" in e.message for e in result.errors)


def test_validate_title_too_long(tmp_path: Path) -> None:
    row = _make_happy_row(tmp_path)
    fields = dict(row.fields)
    fields["标题"] = "字" * 31
    row = BitableRow(record_id=row.record_id, fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "x.mp4"
    (tmp_path / "x.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "标题" and "30" in e.message for e in result.errors)


def test_validate_title_boundary_16_passes(tmp_path: Path) -> None:
    row = _make_happy_row(tmp_path)
    fields = dict(row.fields)
    fields["标题"] = "字" * 16
    row = BitableRow(record_id=row.record_id, fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "x.mp4"
    (tmp_path / "x.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert all(e.field != "标题" for e in result.errors), result.errors


def test_validate_tags_too_many(tmp_path: Path) -> None:
    row = _make_happy_row(tmp_path)
    fields = dict(row.fields)
    fields["标题"] = "字" * 16
    fields["标签"] = [{"text": f"t{i}"} for i in range(6)]
    row = BitableRow(record_id=row.record_id, fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "x.mp4"
    (tmp_path / "x.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "标签" and "5" in e.message for e in result.errors)


def test_validate_account_empty(tmp_path: Path) -> None:
    row = _make_happy_row(tmp_path)
    fields = dict(row.fields)
    fields["标题"] = "字" * 16
    fields["账号"] = ""
    row = BitableRow(record_id=row.record_id, fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "x.mp4"
    (tmp_path / "x.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "账号" and "未指定" in e.message for e in result.errors)


def test_validate_account_not_in_active_set(tmp_path: Path) -> None:
    row = _make_happy_row(tmp_path)
    fields = dict(row.fields)
    fields["标题"] = "字" * 16
    fields["账号"] = "account_unknown"
    row = BitableRow(record_id=row.record_id, fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "x.mp4"
    (tmp_path / "x.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "账号" and "account_unknown" in e.message for e in result.errors)
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_validator.py -v
```

Expected:fail with `ImportError: cannot import name 'validate'`。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_validator.py
git commit -m "test: add failing tests for validator text/select/account rules"
```

- [ ] **Step 4: 最小实现 (GREEN)**

在 `wxsp/validator.py` 追加 import:

```python
from typing import Any

from wxsp.config import Settings
from wxsp.feishu import BitableRow
```

追加 `validate` 函数(包含文本/选择/账号 rules,文件/时间 rules 留给后续任务):

```python
_TITLE_MIN = 16
_TITLE_MAX = 30
_TAGS_MAX = 5


def validate(
    row: BitableRow,
    *,
    config: Settings,
    now: datetime,
    nas_finder: NasFinder,
    active_account_ids: set[str],
) -> ValidationResult:
    """逐字段校验,错误全部收集。返回 ValidationResult。

    所有规则独立运行,不会在第一个错就 return —— 运营要一次看到所有问题。
    """
    fm = config.feishu.field_map
    errors: list[FieldError] = []

    title = _check_title(row, fm.title, errors)
    tags = _check_tags(row, fm.tags, errors)
    description = _get_str(row.fields.get(fm.description))
    topic = _get_str(row.fields.get(fm.topic))
    original_claim = bool(row.fields.get(fm.original_claim) or False)
    account_id = _check_account(row, fm.account, active_account_ids, errors)

    if errors:
        return ValidationResult(ok=False, errors=errors)
    return ValidationResult(
        ok=True,
        title=title,
        description=description,
        tags=tags,
        topic=topic,
        original_claim=original_claim,
        account_id=account_id,
    )


def _check_title(row: BitableRow, field_name: str, errors: list[FieldError]) -> str | None:
    raw = row.fields.get(field_name)
    if not raw or not isinstance(raw, str):
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    n = len(raw)
    if n < _TITLE_MIN or n > _TITLE_MAX:
        errors.append(
            FieldError(field=field_name, message=f"{n} 字(要求 {_TITLE_MIN}-{_TITLE_MAX} 字)")
        )
        return None
    return raw


def _check_tags(row: BitableRow, field_name: str, errors: list[FieldError]) -> list[str]:
    raw = row.fields.get(field_name) or []
    # 多选字段:[{"text": ...}, ...] 或字符串列表
    tags: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                tags.append(text)
        elif isinstance(item, str):
            tags.append(item)
    if len(tags) > _TAGS_MAX:
        errors.append(FieldError(field=field_name, message=f"{len(tags)} 个(最多 {_TAGS_MAX} 个)"))
    return tags


def _check_account(
    row: BitableRow, field_name: str, active_account_ids: set[str], errors: list[FieldError],
) -> str | None:
    raw = row.fields.get(field_name)
    account_id = _coerce_select(raw)
    if not account_id:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if account_id not in active_account_ids:
        errors.append(FieldError(field=field_name, message=f"{account_id!r} 不存在或已停用"))
        return None
    return account_id


def _coerce_select(raw: Any) -> str | None:
    """单选字段:可能返回 dict {'text': ...} 或字符串。"""
    if isinstance(raw, dict):
        text = raw.get("text")
        return text if isinstance(text, str) else None
    if isinstance(raw, str):
        return raw or None
    return None


def _get_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw:
        return raw
    return None
```

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_validator.py -v
uv run ruff check wxsp/validator.py tests/test_validator.py
uv run mypy wxsp/validator.py
```

Expected:本 Task 加的 6 个测试 PASS(类型测试继续 PASS;文件 / 时间 rules / happy path 留到 Task 8/9)。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/validator.py
git commit -m "feat(validator): add title/tags/topic/original_claim/account rules"
```

---

### Task 8: validator 文件 rules — `video_file` / `cover`

**Files**:
- Modify: `wxsp/validator.py`
- Test: `tests/test_validator.py`(extend)

**Why**:加视频/封面 rule:必须 NAS 找到 + 扩展名(视频)+ 大小 ≤ 4 GiB。

- [ ] **Step 1: 写失败测试 (RED)**

在 `tests/test_validator.py` 追加:

```python
def _row_with(tmp_path: Path, **overrides: Any) -> BitableRow:
    """基于 happy row 改字段。"""
    row = _make_happy_row(tmp_path)
    fields = dict(row.fields)
    fields["标题"] = "字" * 16
    fields.update(overrides)
    return BitableRow(record_id=row.record_id, fields=fields)


def test_validate_video_file_not_found(tmp_path: Path) -> None:
    row = _row_with(tmp_path, **{"视频文件": "missing.mp4"})
    nas = _StubNas()  # 不预置 video_returns → 抛 FileNotFoundError
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "视频文件" and "未在" in e.message for e in result.errors)


def test_validate_video_file_wrong_extension(tmp_path: Path) -> None:
    bad = tmp_path / "bad.avi"
    bad.write_bytes(b"x")
    row = _row_with(tmp_path, **{"视频文件": "bad.avi"})
    nas = _StubNas()
    nas.video_returns["bad.avi"] = bad
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "视频文件" and ".avi" in e.message for e in result.errors)


def test_validate_video_extension_case_insensitive(tmp_path: Path) -> None:
    upper = tmp_path / "x.MP4"
    upper.write_bytes(b"x")
    row = _row_with(tmp_path, **{"视频文件": "x.MP4"})
    nas = _StubNas()
    nas.video_returns["x.MP4"] = upper
    # 其它字段必须也合规,这里时间还没实现,但 video 字段不应该报错
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    # 不关心整体 ok(时间 rules 还没做);只关心视频文件这一项不报错
    assert all(e.field != "视频文件" for e in result.errors), result.errors


def test_validate_video_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = tmp_path / "big.mp4"
    big.write_bytes(b"x")
    # 假装它 5 GiB
    class _FakeStat:
        st_size = 5 * 1024 ** 3
        st_mtime = 1.0
    monkeypatch.setattr(Path, "stat", lambda self: _FakeStat())

    row = _row_with(tmp_path, **{"视频文件": "big.mp4"})
    nas = _StubNas()
    nas.video_returns["big.mp4"] = big
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "视频文件" and "GiB" in e.message for e in result.errors)


def test_validate_cover_missing(tmp_path: Path) -> None:
    row = _row_with(tmp_path, **{"封面文件": "missing.jpg"})
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "国庆01.mp4"
    (tmp_path / "国庆01.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "封面文件" and "missing.jpg" in e.message for e in result.errors)


def test_validate_cover_empty_is_ok(tmp_path: Path) -> None:
    row = _row_with(tmp_path, **{"封面文件": ""})
    nas = _StubNas()
    video = tmp_path / "国庆01.mp4"
    video.write_bytes(b"x")
    nas.video_returns["国庆01.mp4"] = video
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    # 封面字段不应该报错
    assert all(e.field != "封面文件" for e in result.errors), result.errors
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_validator.py -v
```

Expected:6 个新增测试 fail(validator 没处理 video_file / cover 字段)。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_validator.py
git commit -m "test: add failing tests for validator video/cover file rules"
```

- [ ] **Step 4: 最小实现 (GREEN)**

在 `wxsp/validator.py` 顶部加常量:

```python
_VIDEO_EXTENSIONS = {".mp4", ".mov"}
_VIDEO_MAX_BYTES = 4 * 1024 ** 3  # 4 GiB
```

修改 `validate` 函数,在 `_check_account` 后插入文件检查:

```python
def validate(
    row: BitableRow,
    *,
    config: Settings,
    now: datetime,
    nas_finder: NasFinder,
    active_account_ids: set[str],
) -> ValidationResult:
    fm = config.feishu.field_map
    errors: list[FieldError] = []

    title = _check_title(row, fm.title, errors)
    tags = _check_tags(row, fm.tags, errors)
    description = _get_str(row.fields.get(fm.description))
    topic = _get_str(row.fields.get(fm.topic))
    original_claim = bool(row.fields.get(fm.original_claim) or False)
    account_id = _check_account(row, fm.account, active_account_ids, errors)
    video_path = _check_video(row, fm.video_file, nas_finder, errors)
    cover_path = _check_cover(row, fm.cover, nas_finder, errors)

    if errors:
        return ValidationResult(ok=False, errors=errors)
    return ValidationResult(
        ok=True,
        title=title, description=description, tags=tags, topic=topic,
        original_claim=original_claim, account_id=account_id,
        video_path=video_path, cover_path=cover_path,
    )
```

追加函数:

```python
def _check_video(
    row: BitableRow, field_name: str, nas_finder: NasFinder, errors: list[FieldError],
) -> Path | None:
    raw = _get_str(row.fields.get(field_name))
    if not raw:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    try:
        path = nas_finder.find_video(raw)
    except FileNotFoundError:
        errors.append(FieldError(field=field_name, message=f"未在 NAS 下找到 {raw!r}"))
        return None
    if path.suffix.lower() not in _VIDEO_EXTENSIONS:
        errors.append(
            FieldError(field=field_name, message=f"不支持的扩展名 {path.suffix!r}(允许 .mp4/.mov)")
        )
        return None
    size = path.stat().st_size
    if size > _VIDEO_MAX_BYTES:
        gib = size / 1024 ** 3
        errors.append(
            FieldError(field=field_name, message=f"{gib:.2f} GiB 超出 4 GiB 上限")
        )
        return None
    return path


def _check_cover(
    row: BitableRow, field_name: str, nas_finder: NasFinder, errors: list[FieldError],
) -> Path | None:
    raw = _get_str(row.fields.get(field_name))
    if not raw:
        return None  # 封面可空
    try:
        return nas_finder.find_cover(raw)
    except FileNotFoundError:
        errors.append(FieldError(field=field_name, message=f"未在 NAS 下找到 {raw!r}"))
        return None
```

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_validator.py -v
uv run ruff check wxsp/validator.py tests/test_validator.py
uv run mypy wxsp/validator.py
```

Expected:本 Task 加的 6 个测试 PASS。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/validator.py
git commit -m "feat(validator): add video_file / cover file rules with NAS lookup"
```

---

### Task 9: validator 时间 rules + happy path

**Files**:
- Modify: `wxsp/validator.py`
- Test: `tests/test_validator.py`(extend)

**Why**:补完最后两个 rule:执行日期(date)和定时发布时间(datetime + 区间)。然后 happy path 测试可以完整跑通。

时区规则(spec §4.4):飞书返回 ms UTC timestamp → 转 `Asia/Shanghai` → 落 naive。

- [ ] **Step 1: 写失败测试 (RED)**

在 `tests/test_validator.py` 追加:

```python
def test_validate_execute_date_missing(tmp_path: Path) -> None:
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["执行日期"] = None
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "国庆01.mp4"
    (tmp_path / "国庆01.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "执行日期" for e in result.errors)


def test_validate_publish_at_too_close(tmp_path: Path) -> None:
    # publish_at = now + 29 分钟 → 早于 now+30min
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["定时发布时间"] = _datetime_to_feishu_ms(datetime(2026, 5, 12, 9, 29))
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "国庆01.mp4"
    (tmp_path / "国庆01.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "定时发布时间" and "30min" in e.message for e in result.errors)


def test_validate_publish_at_too_far(tmp_path: Path) -> None:
    # publish_at = now + 14 天 + 1 分钟 → 超出 now+14d 上限
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["定时发布时间"] = _datetime_to_feishu_ms(datetime(2026, 5, 26, 9, 1))
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "国庆01.mp4"
    (tmp_path / "国庆01.mp4").write_bytes(b"x")
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "定时发布时间" and "14d" in e.message for e in result.errors)


def test_validate_publish_at_boundary_30min_passes(tmp_path: Path) -> None:
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["定时发布时间"] = _datetime_to_feishu_ms(datetime(2026, 5, 12, 9, 30))
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    video = tmp_path / "国庆01.mp4"
    video.write_bytes(b"x")
    nas.video_returns["国庆01.mp4"] = video
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert all(e.field != "定时发布时间" for e in result.errors), result.errors


def test_validate_publish_date_earlier_than_execute_date(tmp_path: Path) -> None:
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["执行日期"] = _date_to_feishu_ms(date(2026, 5, 14))
    fields["定时发布时间"] = _datetime_to_feishu_ms(datetime(2026, 5, 13, 14, 0))  # 早于 execute_date
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    video = tmp_path / "国庆01.mp4"
    video.write_bytes(b"x")
    nas.video_returns["国庆01.mp4"] = video
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    assert any(e.field == "定时发布时间" and "早于执行日期" in e.message for e in result.errors)


def test_validate_multi_error_collection(tmp_path: Path) -> None:
    """同时 3 个字段错 → errors 长度 == 3。"""
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "短"  # 1 字
    fields["视频文件"] = "missing.mp4"
    fields["账号"] = ""
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()  # 不预置 → 找不到
    result = validate(row, config=_make_settings(tmp_path), now=datetime(2026, 5, 12, 9, 0),
                      nas_finder=nas, active_account_ids={"account_a"})
    assert result.ok is False
    error_fields = {e.field for e in result.errors}
    assert {"标题", "视频文件", "账号"} <= error_fields


def test_validate_happy_path(tmp_path: Path) -> None:
    """所有字段都合规 → ok=True,全部 attribute 填充。"""
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "这是一个测试标题视频内容十八字符"  # 16 字
    row = BitableRow(record_id="rec_happy", fields=fields)

    settings = _make_settings(tmp_path)
    nas = _StubNas()
    video_path = tmp_path / "国庆01.mp4"
    video_path.write_bytes(b"x" * 100)
    nas.video_returns["国庆01.mp4"] = video_path

    result = validate(
        row, config=settings, now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas, active_account_ids={"account_a", "account_b"},
    )
    assert result.ok is True, result.errors
    assert result.title == "这是一个测试标题视频内容十八字符"
    assert result.tags == ["标签1", "标签2"]
    assert result.topic == "测试合集"
    assert result.original_claim is True
    assert result.account_id == "account_a"
    assert result.video_path == video_path
    assert result.cover_path is None
    assert result.execute_date == date(2026, 5, 13)
    assert result.publish_at == datetime(2026, 5, 13, 14, 0)  # naive 上海时间
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_validator.py -v
```

Expected:7 个新增测试(6 个时间 rule + 1 个 happy_path)fail —— validator 还没处理时间字段。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_validator.py
git commit -m "test: add failing tests for validator time rules + multi-error collection"
```

- [ ] **Step 4: 最小实现 (GREEN)**

在 `wxsp/validator.py` 顶部加 import:

```python
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
```

加常量:

```python
_PUBLISH_MIN_DELTA = timedelta(minutes=30)
_PUBLISH_MAX_DELTA = timedelta(days=14)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
```

修改 `validate`:

```python
def validate(
    row: BitableRow,
    *,
    config: Settings,
    now: datetime,
    nas_finder: NasFinder,
    active_account_ids: set[str],
) -> ValidationResult:
    fm = config.feishu.field_map
    errors: list[FieldError] = []

    title = _check_title(row, fm.title, errors)
    tags = _check_tags(row, fm.tags, errors)
    description = _get_str(row.fields.get(fm.description))
    topic = _get_str(row.fields.get(fm.topic))
    original_claim = bool(row.fields.get(fm.original_claim) or False)
    account_id = _check_account(row, fm.account, active_account_ids, errors)
    video_path = _check_video(row, fm.video_file, nas_finder, errors)
    cover_path = _check_cover(row, fm.cover, nas_finder, errors)
    execute_date = _check_execute_date(row, fm.execute_date, errors)
    publish_at = _check_publish_at(row, fm.publish_at, now, execute_date, errors)

    if errors:
        return ValidationResult(ok=False, errors=errors)
    return ValidationResult(
        ok=True,
        title=title, description=description, tags=tags, topic=topic,
        original_claim=original_claim, account_id=account_id,
        video_path=video_path, cover_path=cover_path,
        execute_date=execute_date, publish_at=publish_at,
    )
```

追加函数:

```python
def _check_execute_date(row: BitableRow, field_name: str, errors: list[FieldError]) -> date | None:
    raw = row.fields.get(field_name)
    if raw is None:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if not isinstance(raw, (int, float)):
        errors.append(FieldError(field=field_name, message=f"无法解析 {raw!r}"))
        return None
    dt_utc = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    return dt_utc.astimezone(_SHANGHAI).date()


def _check_publish_at(
    row: BitableRow,
    field_name: str,
    now: datetime,
    execute_date: date | None,
    errors: list[FieldError],
) -> datetime | None:
    raw = row.fields.get(field_name)
    if raw is None:
        errors.append(FieldError(field=field_name, message="未指定"))
        return None
    if not isinstance(raw, (int, float)):
        errors.append(FieldError(field=field_name, message=f"无法解析 {raw!r}"))
        return None
    dt_utc = datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    publish_at = dt_utc.astimezone(_SHANGHAI).replace(tzinfo=None)
    min_allowed = now + _PUBLISH_MIN_DELTA
    max_allowed = now + _PUBLISH_MAX_DELTA
    if publish_at < min_allowed:
        errors.append(
            FieldError(
                field=field_name,
                message=f"{publish_at.strftime('%Y-%m-%d %H:%M')} 早于 now+30min",
            )
        )
        return None
    if publish_at > max_allowed:
        errors.append(
            FieldError(
                field=field_name,
                message=f"{publish_at.strftime('%Y-%m-%d %H:%M')} 超出 now+14d 上限",
            )
        )
        return None
    if execute_date is not None and publish_at.date() < execute_date:
        errors.append(
            FieldError(
                field=field_name,
                message=f"日期 {publish_at.date()} 早于执行日期 {execute_date}",
            )
        )
        return None
    return publish_at
```

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_validator.py -v
uv run ruff check wxsp/validator.py tests/test_validator.py
uv run mypy wxsp/validator.py
```

Expected:**全部** validator 测试 PASS,包括 happy path。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/validator.py
git commit -m "feat(validator): add execute_date / publish_at rules + happy path"
```

---

### Task 10: `wxsp sync` CLI 命令

**Files**:
- Modify: `wxsp/cli.py`
- Test: `tests/test_cli_sync.py`(NEW)

**Why**:把前面所有模块串起来。测试用 monkeypatch 注入 FakeClient,不真打飞书。

设计契约(spec §5.2):
1. 预检:`config.feishu.enabled=False` → exit 0;DB 不存在 → exit 64
2. 拉数据:`FeishuApiError` → exit 70
3. 逐行:已存在 record_id → skip;valid → DB 写入 + accepted;invalid → rejected
4. 回写:accepted 改 "已计划";rejected 改 "失败" + error_message;skipped 加 error_message 不动 status
5. `--dry-run`:跳过 DB 写入和回写
6. `config.feishu.sync.write_back_enabled=False`:跳过回写

- [ ] **Step 1: 写失败测试 (RED)**

`tests/test_cli_sync.py`:

```python
"""CLI `wxsp sync` integration tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlmodel import Session, select
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.feishu import BitableRow
from wxsp.models import Account, Task, Video


def _datetime_to_feishu_ms(dt: datetime) -> int:
    utc_dt = dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    return int(utc_dt.timestamp() * 1000)


def _date_to_feishu_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture
def sync_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """搭一个完整的 sync 测试环境:config.yaml + DB + NAS 目录 + 一个 active account。"""
    nas_root = tmp_path / "nas"
    video_root = nas_root / "videos"
    cover_root = nas_root / "covers"
    video_root.mkdir(parents=True)
    cover_root.mkdir(parents=True)
    # 准备 5 个合规视频
    for i in range(5):
        (video_root / f"video_{i}.mp4").write_bytes(b"x" * 100)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "app": {"data_dir": str(tmp_path / "data"), "logs_dir": str(tmp_path / "logs"),
                        "timezone": "Asia/Shanghai"},
                "paths": {
                    "nas_root": str(nas_root),
                    "video_search_root": str(video_root),
                    "cover_search_root": str(cover_root),
                },
                "accounts": {
                    "account_a": {"display_name": "测试号", "daily_limit": 20,
                                  "user_data_dir": str(tmp_path / "p" / "a")},
                },
                "scheduler": {},
                "publisher": {},
                "feishu": {
                    "enabled": True,
                    "app_id": "cli_x",
                    "app_secret": "secret",
                    "bitable": {"app_token": "tok", "table_id": "tbl"},
                },
                "monitoring": {"notifiers": {"wecom": {"webhook": "http://x"}}},
                "webui": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    db_path = tmp_path / "data" / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(Account(
            id="account_a", display_name="测试号",
            user_data_dir=str(tmp_path / "p" / "a"),
        ))
        session.commit()

    return {"db_path": db_path, "video_root": video_root, "tmp_path": tmp_path}


def _happy_row(i: int, now: datetime) -> BitableRow:
    return BitableRow(
        record_id=f"rec_ok_{i}",
        fields={
            "标题": f"测试标题视频内容编号{i:02d}六字",  # 18 字以上
            "描述": "desc",
            "标签": [{"text": "t1"}],
            "封面文件": "",
            "合集": None,
            "原创": True,
            "账号": "account_a",
            "执行日期": _date_to_feishu_ms((now + timedelta(days=1)).date()),
            "定时发布时间": _datetime_to_feishu_ms(now + timedelta(days=1, hours=5)),
            "视频文件": f"video_{i}.mp4",
            "状态": "待入库",
        },
    )


def _bad_row(i: int, problem: str, now: datetime) -> BitableRow:
    """造一行不合规数据,problem 决定哪种错。"""
    base = dict(_happy_row(i, now).fields)
    if problem == "title":
        base["标题"] = "短"
    elif problem == "video_missing":
        base["视频文件"] = "no_such.mp4"
    elif problem == "publish_too_close":
        base["定时发布时间"] = _datetime_to_feishu_ms(now + timedelta(minutes=10))
    elif problem == "account_empty":
        base["账号"] = ""
    elif problem == "multi_error":
        base["标题"] = "短"
        base["账号"] = ""
    return BitableRow(record_id=f"rec_bad_{i}_{problem}", fields=base)


class _FakeClient:
    """模拟 feishu 模块的 lark.Client + fetch/writeback 行为。"""

    def __init__(self, rows: list[BitableRow]) -> None:
        self._rows = rows
        self.writebacks: list[tuple[str, dict[str, Any]]] = []  # (record_id, fields)


def test_sync_happy_pipeline(sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(5)] + [
        _bad_row(0, "title", now),
        _bad_row(1, "video_missing", now),
        _bad_row(2, "publish_too_close", now),
        _bad_row(3, "account_empty", now),
        _bad_row(4, "multi_error", now),
    ]

    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr(
        "wxsp.cli.fetch_pending_rows",
        lambda client, **kw: list(rows),
    )

    def fake_writeback(client: Any, *, record_id: str, fields: dict[str, Any], **kw: Any) -> None:
        fake.writebacks.append((record_id, fields))

    monkeypatch.setattr("wxsp.cli.writeback_row", fake_writeback)
    monkeypatch.setattr("wxsp.cli.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        videos = session.exec(select(Video)).all()
        tasks = session.exec(select(Task)).all()
    assert len(videos) == 5
    assert len(tasks) == 5
    # 飞书回写:5 个 accepted("已计划")+ 5 个 rejected("失败" + error_message)
    accepted_writebacks = [w for w in fake.writebacks if w[1].get("状态") == "已计划"]
    rejected_writebacks = [w for w in fake.writebacks if w[1].get("状态") == "失败"]
    assert len(accepted_writebacks) == 5
    assert len(rejected_writebacks) == 5
    # 每个 rejected 应该有 error_message
    for _, fields in rejected_writebacks:
        assert "错误信息" in fields
        assert fields["错误信息"]  # 非空


def test_sync_second_run_skips_existing(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(5)]

    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.cli.fetch_pending_rows", lambda client, **kw: list(rows))

    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.cli.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.cli.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    # 第一遍
    r1 = runner.invoke(app, ["sync"])
    assert r1.exit_code == 0
    accepted_first = [w for w in writebacks if w[1].get("状态") == "已计划"]
    assert len(accepted_first) == 5

    # 第二遍:DB 已有 5 条 → 全部 skip,回写"已有历史任务"
    writebacks.clear()
    r2 = runner.invoke(app, ["sync"])
    assert r2.exit_code == 0
    assert len(writebacks) == 5
    for _, fields in writebacks:
        assert "已有历史任务" in (fields.get("错误信息") or "")
        assert "状态" not in fields  # 不动 status

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
    assert len(tasks) == 5  # 没新增


def test_sync_disabled_exits_zero(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 把 config.yaml 改成 feishu.enabled=false
    cfg = sync_env["tmp_path"] / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["feishu"]["enabled"] = False
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "飞书未启用" in result.output


def test_sync_dry_run_writes_nothing(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: object())
    monkeypatch.setattr("wxsp.cli.fetch_pending_rows", lambda client, **kw: list(rows))
    called = {"writeback": 0}
    monkeypatch.setattr(
        "wxsp.cli.writeback_row",
        lambda *a, **kw: called.__setitem__("writeback", called["writeback"] + 1),
    )
    monkeypatch.setattr("wxsp.cli.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--dry-run"])
    assert result.exit_code == 0
    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        assert session.exec(select(Video)).all() == []
        assert session.exec(select(Task)).all() == []
    assert called["writeback"] == 0


def test_sync_feishu_api_error_exits_70(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wxsp.feishu import FeishuApiError

    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: object())

    def boom(client: Any, **kw: Any) -> list[BitableRow]:
        raise FeishuApiError("simulated")

    monkeypatch.setattr("wxsp.cli.fetch_pending_rows", boom)

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 70


class _FrozenDatetime:
    """让 cli 模块里的 datetime.now() 返回固定时间。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self, tz: Any = None) -> datetime:
        return self._now

    def __getattr__(self, name: str) -> Any:
        import datetime as _dt
        return getattr(_dt.datetime, name)
```

- [ ] **Step 2: 验证 RED**

```bash
uv run pytest tests/test_cli_sync.py -v
```

Expected:全部测试 fail with `ImportError: cannot import name 'make_client' from 'wxsp.cli'`(或类似)。

- [ ] **Step 3: RED commit**

```bash
git add tests/test_cli_sync.py
git commit -m "test: add failing tests for wxsp sync CLI"
```

- [ ] **Step 4: 最小实现 (GREEN)**

修改 `wxsp/cli.py` —— 替换 import 区 + `sync` 函数:

import 区追加(放在其它 wxsp imports 后):

```python
from wxsp.config import Settings, load_settings
from wxsp.feishu import (
    BitableRow,
    FeishuApiError,
    fetch_pending_rows,
    make_client,
    writeback_row,
)
from wxsp.models import Task, Video
from wxsp.nas import find_cover, find_video
from wxsp.validator import FieldError, NasFinder, validate
```

替换 `sync` 函数:

```python
class _NasFinderImpl:
    """生产 NasFinder:接 config 的 search root。"""

    def __init__(self, video_root: Path, cover_root: Path) -> None:
        self._video_root = video_root
        self._cover_root = cover_root

    def find_video(self, filename: str) -> Path:
        return find_video(filename, search_root=self._video_root)

    def find_cover(self, filename: str) -> Path:
        return find_cover(filename, search_root=self._cover_root)


@app.command("sync")
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="走完流程但不写 DB 不回写飞书"),
) -> None:
    """立即拉一次飞书 Bitable,执行入库 / 错误回写。"""
    settings = load_settings()
    if not settings.feishu.enabled:
        typer.echo("[wxsp] 飞书未启用,跳过 sync。")
        return

    typer.echo(
        f"[wxsp] 飞书同步开始: app_token={settings.feishu.bitable.app_token} "
        f"table_id={settings.feishu.bitable.table_id}"
    )

    client = make_client(settings.feishu.app_id, settings.feishu.app_secret)
    try:
        rows = fetch_pending_rows(
            client,
            app_token=settings.feishu.bitable.app_token,
            table_id=settings.feishu.bitable.table_id,
            status_field=settings.feishu.field_map.status,
        )
    except FeishuApiError as exc:
        typer.echo(f"[wxsp] 飞书 API 持续失败: {exc}")
        raise typer.Exit(code=70) from exc

    typer.echo(f"[wxsp] 拉取待入库行: {len(rows)} 条")

    nas_finder: NasFinder = _NasFinderImpl(
        video_root=settings.paths.video_search_root,
        cover_root=settings.paths.cover_search_root,
    )
    now = datetime.now()
    accepted: list[tuple[str, int]] = []
    rejected: list[tuple[str, list[FieldError]]] = []
    skipped_existing: list[str] = []

    with _open_session() as session:
        active_account_ids: set[str] = {
            a.id for a in session.exec(select(Account).where(Account.is_active.is_(True)))
        }
        for row in rows:
            if session.get(Video, row.record_id) is not None:
                skipped_existing.append(row.record_id)
                continue
            result = validate(
                row, config=settings, now=now, nas_finder=nas_finder,
                active_account_ids=active_account_ids,
            )
            if not result.ok:
                rejected.append((row.record_id, result.errors))
                continue
            if dry_run:
                accepted.append((row.record_id, -1))  # dry-run 不真写 DB
                continue
            video = Video(
                id=row.record_id, source="feishu",
                file_path=str(result.video_path),
                title=result.title or "",
                description=result.description,
                tags_json=_dumps_tags(result.tags),
                cover_path=str(result.cover_path) if result.cover_path else None,
                topic=result.topic,
                original_claim=result.original_claim,
                ingested_at=now,
            )
            task = Task(
                video_id=row.record_id,
                account_id=result.account_id or "",
                execute_date=result.execute_date,  # type: ignore[arg-type]
                publish_at=result.publish_at,  # type: ignore[arg-type]
                status="pending",
            )
            try:
                session.add(video)
                session.add(task)
                session.flush()
            except IntegrityError:
                session.rollback()
                skipped_existing.append(row.record_id)
                continue
            accepted.append((row.record_id, task.id or -1))

    # 回写飞书(--dry-run 跳过;config.feishu.sync.write_back_enabled=False 也跳过)
    if not dry_run and settings.feishu.sync.write_back_enabled:
        fm = settings.feishu.field_map
        for record_id, _task_id in accepted:
            _safe_writeback(client, settings, record_id, {fm.status: "已计划"})
        for record_id, errs in rejected:
            _safe_writeback(
                client, settings, record_id,
                {fm.status: "失败", fm.error_message: _format_errors(errs)},
            )
        for record_id in skipped_existing:
            _safe_writeback(
                client, settings, record_id,
                {fm.error_message: "已有历史任务,请在 Web UI 重试"},
            )

    typer.echo("[wxsp] 飞书同步完成")
    typer.echo(f"  拉取: {len(rows)}")
    typer.echo(f"  入库: {len(accepted)}{' (dry-run)' if dry_run else ''}")
    typer.echo(f"  拒绝: {len(rejected)}{' (已回写)' if not dry_run else ''}")
    typer.echo(f"  已存在跳过: {len(skipped_existing)}")


def _safe_writeback(
    client: Any, settings: Settings, record_id: str, fields: dict[str, Any],
) -> None:
    """writeback 单行失败不抛,打印告警继续。"""
    try:
        writeback_row(
            client,
            app_token=settings.feishu.bitable.app_token,
            table_id=settings.feishu.bitable.table_id,
            record_id=record_id,
            fields=fields,
        )
    except FeishuApiError as exc:
        typer.echo(f"[wxsp] 回写 {record_id} 失败(已跳过): {exc}")


def _format_errors(errs: list[FieldError]) -> str:
    bullet_lines = "\n".join(f"· {e.field}: {e.message}" for e in errs)
    return f"校验失败,请修复后将\"状态\"改回\"待入库\":\n{bullet_lines}"


def _dumps_tags(tags: list[str]) -> str:
    import json
    return json.dumps(tags, ensure_ascii=False)
```

import 区还需追加(顶部 `from typing import Any`):

```python
from typing import Any
```

注意:如果 `_open_session` 因 `WXSP_DB_PATH` 指向新位置而首次创建 DB,需要确保 `init_db` 已被调用——`_open_session` 内部已经做了。

- [ ] **Step 5: 验证 GREEN + 静态检查**

```bash
uv run pytest tests/test_cli_sync.py -v
uv run pytest -v   # 全量回归
uv run ruff check wxsp/cli.py tests/test_cli_sync.py
uv run mypy wxsp/cli.py
```

Expected:5 个 cli_sync 测试 PASS,全量回归 PASS。

- [ ] **Step 6: GREEN commit**

```bash
git add wxsp/cli.py
git commit -m "feat(cli): implement wxsp sync end-to-end (fetch + validate + DB + writeback)"
```

---

### Task 11: M3 验收 + 手工冒烟

**Files**:
- 不改源码
- 可能加 `config.example.yaml` 的 feishu 示例(若 M0 没补全)

**Why**:执行 spec §8 的全部验收项,确认 M3 端到端跑通,然后打 M3 acceptance commit。

- [ ] **Step 1: 检查 config.example.yaml 有无 feishu 节示例**

```bash
grep -n "feishu" config.example.yaml || echo "MISSING"
```

如果输出 MISSING 或不完整,补一个完整示例(本次 plan 不强制,M0 的责任)。

- [ ] **Step 2: 跑完整测试套件**

```bash
uv run pytest -v
```

Expected:全绿(含 M1/M2 的所有旧测试)。

- [ ] **Step 3: 跑 pre-commit 全检查**

```bash
uv run pre-commit run --all-files
```

Expected:trim/yaml/ruff/ruff-format/mypy 全 Pass。

- [ ] **Step 4: 手工冒烟(可选,有 live 飞书表才能跑)**

如果有 live 飞书 app 凭证 + 测试表(5 合规 + 5 不合规),手动跑:

```bash
export FEISHU_APP_SECRET=xxx
uv run wxsp sync --dry-run    # 先看效果,不污染飞书
uv run wxsp sync              # 真跑
uv run wxsp sync              # 再跑一次,验证"已存在跳过"
```

观察:
- 终端汇总数字符合预期
- 飞书侧"状态"字段:合规行变"已计划",不合规变"失败"
- 飞书"错误信息"字段:不合规行格式化错误清单可读
- 重复跑:0 入库 + 已存在跳过 = 之前入库数

无 live 飞书凭证时:跳过本步,集成测试已覆盖等价场景。

- [ ] **Step 5: M3 acceptance commit**

```bash
git commit --allow-empty -m "$(cat <<'EOF'
chore: mark M3 acceptance complete

M3 交付:
- wxsp/feishu.py(make_client + fetch_pending_rows + writeback_row + 3 次指数退避)
- wxsp/nas.py(find_video / find_cover,mtime-newest tiebreak)
- wxsp/validator.py(纯函数,字段独立校验全收集)
- wxsp/cli.py:wxsp sync 端到端(预检 / 拉数据 / 逐行 / 回写 / 汇总)

验收(spec §8):
- 单测/集成测全绿(test_{nas,feishu,validator,cli_sync}.py)
- 多错收集、文件 / 时间 / 账号 / 多选 rules 各 ≥ 1 case
- 重复 sync 不重复入库,改为回写"已存在"
- --dry-run 不写 DB 不回写
- 飞书 API 持续失败 → exit 70

偏离整体 design:见 spec §7。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

(以下是写完计划后自查的记录,确认 spec 各项需求都有任务覆盖。)

### Spec 覆盖

| Spec 章节 | 覆盖任务 |
|----------|--------|
| §1.1 交付物 — feishu.py | T3+T4+T5 |
| §1.1 交付物 — validator.py | T6+T7+T8+T9 |
| §1.1 交付物 — nas.py | T2 |
| §1.1 交付物 — cli sync | T10 |
| §1.1 交付物 — lark-oapi 依赖 | T1 |
| §2 飞书 API 接入 — 三个函数签名 | T3+T4+T5 |
| §2.3 重试策略 — 3 次 1s/2s 退避 | T4+T5(测试 + 实现) |
| §3 NAS find_* | T2 |
| §4 validator 类型 + 全部 rules | T6→T9 |
| §4.4 飞书字段类型转换(ms→naive Asia/Shanghai) | T9 |
| §5.2 sync 编排顺序 | T10 |
| §5.3 错误回写格式 | T10 `_format_errors` |
| §5.4 exit codes | T10 |
| §6.1 单测 + §6.2 集成测试 fixture | T2/T3/T4/T5/T6/T7/T8/T9/T10 各自 |
| §8 验收 | T11 |

### Type Consistency

- `BitableRow` 在 T3 定义,T4/T5/T7/T8/T9/T10 沿用同名同字段
- `ValidationResult` 在 T6 定义所有字段,T7/T8/T9 逐步填充其属性
- `NasFinder` Protocol 在 T6 定义,T7/T8/T9/T10 沿用
- 时区:T9 用 `Asia/Shanghai`;T10 测试 fixture 用 timedelta(hours=8) 模拟反向换算,符合 spec §4.4
- exit codes:0/64/70,T10 内部一致

### No Placeholders

- 每个步骤都有具体代码或具体命令
- `_build_search_request` 内部 lark-oapi builder 链是 best-effort,implementer 用 Context7 验证最新 API 名是允许的——这是版本敏感的细节,不算 TBD;**对外接口签名固定**
- 没有 "implement later" / "TODO" / "similar to Task N"
