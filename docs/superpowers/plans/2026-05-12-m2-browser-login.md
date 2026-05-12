# M2 Browser + Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the patchright-based browser layer with per-account persistent contexts, plus `wxsp login <account_id>` (QR scan) and `wxsp doctor` (cookie health check) commands, leaving M3+ a working browser foundation.

**Architecture:** A thin `browser.py` exposes a `browser_context(user_data_dir)` context manager that launches a patchright persistent context with stealth init script injection. `doctor.py` exposes pure DB helpers (`record_cookie_check`, `refresh_cookie_status`) that accept an injected `cookie_checker` callable, so all cookie-status logic is unit-testable without a real browser. The CLI handlers wire these together: `login` calls `browser.check_cookie(...)` once with a long timeout (300s) for QR scan; `doctor` iterates DB accounts with a short timeout (15s).

**Tech Stack:** Python 3.10+, patchright (Playwright fork) sync API, SQLModel, Typer.

---

## M2 Scope and Constraints

**In scope:**
- `wxsp/stealth_js.py::INIT_SCRIPT` — fingerprint patches (`navigator.webdriver`, `chrome`, `plugins`, `languages`, `permissions.query`)
- `wxsp/browser.py` — `browser_context()` persistent context factory + `wait_for_logged_in()` + `check_cookie()`
- `wxsp/models.py` — add `COOKIE_STATUS_OK / WARN / EXPIRED / UNKNOWN` constants
- `wxsp/doctor.py` — `record_cookie_check()` + `refresh_cookie_status()` pure DB helpers
- `wxsp/cli.py` — replace `login` and `doctor` placeholder bodies with real implementations
- `pyproject.toml` — add `patchright>=1.40.0` dependency

**Out of scope (deferred):**
- Cookie expiry "warn" status — needs `config.monitoring.cookie_warn_days` comparison (M7 with notifier wiring)
- NAS reachability check in doctor (M4)
- Feishu API check in doctor (M3)
- Publisher's `verify_logged_in()` step (M5 — but it'll reuse `wait_for_logged_in()`)
- `selectors.py` — selectors live in `browser.py` for M2 since they're login-specific; M5 moves all publish-flow selectors into `selectors.py`

**Hard constraints from CLAUDE.md:**
- `headless=false` ALWAYS — 严禁 headless 跑视频号. `browser_context()` defaults `headless=False`; we never call with `True`.
- `Account.user_data_dir` is `str` in DB. Always wrap with `Path(user_data_dir)` before passing to filesystem APIs (documented in `models.py` module docstring already).
- Cookie persists via `user_data_dir` only — no separate `cookie.json`.
- macOS + Windows cross-platform — all paths via `pathlib.Path`, no string `/` concat.
- 不要硬编码任何账号/Token —— config.yaml or DB only.

**M2 Acceptance (from design doc §7 / CLAUDE.md):**
1. `wxsp login account_test` opens browser, user scans QR, DB `cookie_status='ok'` + `cookie_last_active_at` set.
2. Restart wxsp/the shell: `wxsp doctor` shows `account_test: ok` without re-scanning (persistent context auto-reuses cookies from `user_data_dir`).
3. `wxsp doctor` outputs per-account `cookie_status` (ok / expired / unknown) for every account in DB.

---

## File Structure

```
wxsp/
├── stealth_js.py      ← Task 2  (INIT_SCRIPT constant)
├── models.py          ← Task 2  (+COOKIE_STATUS_* constants)
├── doctor.py          ← Task 3+4 (record_cookie_check, refresh_cookie_status)
├── browser.py         ← Task 5  (browser_context, wait_for_logged_in, check_cookie)
├── cli.py             ← Task 6+7 (login, doctor handlers)
├── ...
tests/
├── test_stealth_js.py       ← Task 2 (NEW)
├── test_models.py           ← Task 2 (extend)
├── test_doctor.py           ← Task 3+4 (NEW)
├── test_browser.py          ← Task 5 (NEW)
├── test_cli_login.py        ← Task 6 (NEW)
├── test_cli_doctor.py       ← Task 7 (NEW)
└── ...
pyproject.toml         ← Task 1 (+patchright dep)
```

---

## Selectors Reference (from social-auto-upload `_ref/`)

Verified via the social-auto-upload `tencent_uploader/main.py`:

- **Home URL** (works as login landing too): `https://channels.weixin.qq.com`
- **"Logged in" markers** (any visible → logged in):
  - `div:has-text("发表视频")`
  - `button:has-text("发表")`
  - `button:has-text("发布视频")`
- **"Not logged in" markers** (these appear on the login page):
  - `div.login-qrcode-wrap`, `div.qrcode-wrap`, `img.qrcode`

For M2 we only positively detect "logged in" — if the success selector times out, we treat as "not logged in" (no need to distinguish "still loading" vs "showing QR"). M5 publisher can add finer detection if needed.

---

## Task 1: Add patchright dependency + install chromium binary

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` (regenerated)

This is a dependency change — no RED/GREEN cycle. Verification = `python -c "import patchright"` works and `wxsp` still installs cleanly.

- [ ] **Step 1: Add patchright to dependencies**

Edit `pyproject.toml`, in the `[project] dependencies` array add `"patchright>=1.40.0"` after `"sqlmodel>=0.0.21",`:

```toml
[project]
name = "wxsp"
version = "0.0.1"
description = "微信视频号自动发布工具"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "typer>=0.12.0",
    "pydantic>=2.6.0",
    "pydantic-settings>=2.2.0",
    "pyyaml>=6.0",
    "loguru>=0.7.2",
    "sqlmodel>=0.0.21",
    "patchright>=1.40.0",
]
```

- [ ] **Step 2: Sync deps**

Run: `uv sync`
Expected: resolves and installs `patchright` + transitive deps (`pyee`, `greenlet`, etc.) with no errors. The line `Installed ... packages` lists `patchright`.

- [ ] **Step 3: Verify patchright imports**

Run: `uv run python -c "import patchright; from patchright.sync_api import sync_playwright; print('patchright OK')"`
Expected: prints `patchright OK` with no traceback.

- [ ] **Step 4: Install Chromium browser binary**

Run: `uv run patchright install chromium`
Expected: downloads Chromium (a few hundred MB the first time), prints something like `Chromium <ver> downloaded to ...`. If patchright reuses an existing playwright cache the download may be near-instant.

Note: this is a one-time-per-machine setup, not committed to git. If the engineer's environment already has chromium, the command is a no-op.

- [ ] **Step 5: Re-run existing test suite to confirm nothing broke**

Run: `uv run pytest -q`
Expected: 45 tests pass (current M0+M1 baseline). No new failures from the dep bump.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add patchright dependency for browser automation"
```

---

## Task 2: stealth_js INIT_SCRIPT + Account cookie status constants

**Files:**
- Create: `tests/test_stealth_js.py`
- Modify: `tests/test_models.py` (extend with cookie status assertions)
- Modify: `wxsp/stealth_js.py` (currently 1-line docstring)
- Modify: `wxsp/models.py` (add 4 constants)

- [ ] **Step 1: Write failing tests for INIT_SCRIPT**

Create `tests/test_stealth_js.py`:

```python
"""Sanity checks on the stealth init script content."""

from __future__ import annotations

from wxsp.stealth_js import INIT_SCRIPT


def test_init_script_is_non_empty_string():
    assert isinstance(INIT_SCRIPT, str)
    assert len(INIT_SCRIPT) > 100  # not just a stub


def test_init_script_patches_webdriver_flag():
    # The single most important anti-bot signal: navigator.webdriver should be hidden.
    assert "navigator" in INIT_SCRIPT
    assert "webdriver" in INIT_SCRIPT


def test_init_script_patches_chrome_runtime():
    assert "window.chrome" in INIT_SCRIPT or "chrome.runtime" in INIT_SCRIPT


def test_init_script_patches_plugins_and_languages():
    assert "plugins" in INIT_SCRIPT
    assert "languages" in INIT_SCRIPT
    assert "zh-CN" in INIT_SCRIPT  # we target Chinese locale


def test_init_script_patches_permissions_query():
    assert "permissions" in INIT_SCRIPT
    assert "query" in INIT_SCRIPT
```

- [ ] **Step 2: Add cookie status assertions to `tests/test_models.py`**

Open `tests/test_models.py` and append at the end of the file (preserving existing tests):

```python
def test_cookie_status_constants_exist_with_expected_values():
    from wxsp.models import (
        COOKIE_STATUS_OK,
        COOKIE_STATUS_WARN,
        COOKIE_STATUS_EXPIRED,
        COOKIE_STATUS_UNKNOWN,
    )

    assert COOKIE_STATUS_OK == "ok"
    assert COOKIE_STATUS_WARN == "warn"
    assert COOKIE_STATUS_EXPIRED == "expired"
    assert COOKIE_STATUS_UNKNOWN == "unknown"


def test_account_default_cookie_status_is_unknown():
    from wxsp.models import Account, COOKIE_STATUS_UNKNOWN

    account = Account(id="a", display_name="A", user_data_dir="/tmp/a")
    assert account.cookie_status == COOKIE_STATUS_UNKNOWN
```

- [ ] **Step 3: Run tests — they should fail**

Run: `uv run pytest tests/test_stealth_js.py tests/test_models.py -v`
Expected:
- All `test_stealth_js.py` tests **FAIL** with `AttributeError` (INIT_SCRIPT not defined, only a module docstring).
- `test_cookie_status_constants_exist_with_expected_values` **FAILS** with `ImportError: cannot import name 'COOKIE_STATUS_OK'`.
- `test_account_default_cookie_status_is_unknown` **FAILS** with the same import error.
- All previous `test_models.py` tests still PASS.

- [ ] **Step 4: Commit RED**

```bash
git add tests/test_stealth_js.py tests/test_models.py
git commit -m "test: add failing tests for stealth init script and cookie status constants"
```

- [ ] **Step 5: Implement `wxsp/stealth_js.py`**

Replace the contents of `wxsp/stealth_js.py` with:

```python
"""反检测 init script 常量(M2)。

patchright 已深度修补 CDP 指纹泄漏;本 init script 是补强补丁,在每个 page 加载
前注入。复用自 OpenCLI 项目的反检测代码(MIT 兼容)。
"""

INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""
```

- [ ] **Step 6: Implement cookie status constants in `wxsp/models.py`**

Open `wxsp/models.py` and add after the existing `TASK_STATUS_*` block (around line 27, immediately before `class Account`):

```python
# Account.cookie_status 状态机:从 wxsp login / wxsp doctor 写入
COOKIE_STATUS_OK = "ok"
COOKIE_STATUS_WARN = "warn"  # 预留:M7 接 cookie_warn_days 阈值后启用
COOKIE_STATUS_EXPIRED = "expired"
COOKIE_STATUS_UNKNOWN = "unknown"
```

- [ ] **Step 7: Run tests — they should now pass**

Run: `uv run pytest tests/test_stealth_js.py tests/test_models.py -v`
Expected: ALL tests PASS (5 stealth_js tests + 2 new cookie-status tests + the 6 pre-existing models tests).

- [ ] **Step 8: Run full suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: 52 tests pass (45 baseline + 7 new).

- [ ] **Step 9: Commit GREEN**

```bash
git add wxsp/stealth_js.py wxsp/models.py
git commit -m "feat: add stealth INIT_SCRIPT and Account cookie status constants"
```

---

## Task 3: doctor.record_cookie_check

**Files:**
- Create: `tests/test_doctor.py`
- Modify: `wxsp/doctor.py` (currently 1-line docstring)

`record_cookie_check(session, account_id, *, is_logged_in, now)` is the single point that writes cookie state to DB. It's called by both `login` (after a successful scan) and `refresh_cookie_status` (one call per account). Pure DB function — no browser, no IO besides the session.

Signature:
- `is_logged_in: bool | None` — `True` → `ok` + bump `cookie_last_active_at`; `False` → `expired`; `None` → `unknown` (browser threw)
- `now: datetime` — caller supplies, makes tests deterministic
- Mutates `account.cookie_status`, `account.cookie_last_checked_at`, and (only on `True`) `account.cookie_last_active_at`. **Does not commit** — caller controls the transaction, mirroring the `transition_task` contract in `db.py`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_doctor.py`:

```python
"""doctor.record_cookie_check + doctor.refresh_cookie_status unit tests.

`refresh_cookie_status` accepts an injected `cookie_checker` callable so we
exercise it without ever launching a real browser.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)


@pytest.fixture
def engine() -> Any:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine: Any) -> Any:
    with Session(engine) as s:
        yield s


def _add_account(session: Session, account_id: str = "account_a") -> Account:
    account = Account(
        id=account_id,
        display_name=f"display-{account_id}",
        user_data_dir=f"/tmp/profiles/{account_id}",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def test_record_cookie_check_marks_ok_and_bumps_last_active(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=True, now=now)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_OK
    assert account.cookie_last_checked_at == now
    assert account.cookie_last_active_at == now


def test_record_cookie_check_marks_expired_and_does_not_bump_last_active(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    earlier = datetime(2026, 5, 10, 12, 0, 0)
    account = _add_account(session)
    account.cookie_last_active_at = earlier
    session.add(account)
    session.commit()

    later = datetime(2026, 5, 12, 14, 30, 0)
    record_cookie_check(session, "account_a", is_logged_in=False, now=later)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_EXPIRED
    assert account.cookie_last_checked_at == later
    assert account.cookie_last_active_at == earlier  # unchanged


def test_record_cookie_check_marks_unknown_when_is_logged_in_is_none(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=None, now=now)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_UNKNOWN
    assert account.cookie_last_checked_at == now
    assert account.cookie_last_active_at is None


def test_record_cookie_check_missing_account_raises(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    with pytest.raises(LookupError):
        record_cookie_check(
            session, "does_not_exist", is_logged_in=True, now=datetime.now()
        )


def test_record_cookie_check_does_not_commit(session: Session) -> None:
    """Mirror transition_task: caller controls the transaction."""
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=True, now=now)
    # rollback BEFORE caller commits — should undo
    session.rollback()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == "unknown"  # default, not "ok"
    assert account.cookie_last_active_at is None
```

- [ ] **Step 2: Run tests — they should fail**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: ALL 5 tests FAIL with `ImportError: cannot import name 'record_cookie_check' from 'wxsp.doctor'`.

- [ ] **Step 3: Commit RED**

```bash
git add tests/test_doctor.py
git commit -m "test: add failing tests for doctor.record_cookie_check"
```

- [ ] **Step 4: Implement `record_cookie_check`**

Replace the contents of `wxsp/doctor.py` with:

```python
"""健康检查命令实现(M2)。

`record_cookie_check` 是写入 cookie 状态的唯一入口,被 `wxsp login` 和
`refresh_cookie_status` 共用。与 `db.transition_task` 一致:**不 commit**,
让调用方决定事务边界(login 成功后回写 + doctor 批量刷新都受益)。
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)


def record_cookie_check(
    session: Session,
    account_id: str,
    *,
    is_logged_in: bool | None,
    now: datetime,
) -> None:
    """更新一个 Account 的 cookie 状态字段。**调用方负责 commit**。

    `is_logged_in`:
      - `True`  → status='ok',`cookie_last_active_at` 更新为 `now`
      - `False` → status='expired',`cookie_last_active_at` 不动
      - `None`  → status='unknown'(浏览器启动失败等异常路径),`cookie_last_active_at` 不动

    `cookie_last_checked_at` 任何情况都更新为 `now`。

    `account_id` 不存在 → `LookupError`。
    """
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"Account id={account_id!r} not found")

    if is_logged_in is True:
        account.cookie_status = COOKIE_STATUS_OK
        account.cookie_last_active_at = now
    elif is_logged_in is False:
        account.cookie_status = COOKIE_STATUS_EXPIRED
    else:
        account.cookie_status = COOKIE_STATUS_UNKNOWN

    account.cookie_last_checked_at = now
    session.add(account)
```

- [ ] **Step 5: Run tests — they should pass**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: ALL 5 tests PASS.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest -q`
Expected: 57 tests pass (52 + 5 new).

- [ ] **Step 7: Commit GREEN**

```bash
git add wxsp/doctor.py
git commit -m "feat(doctor): implement record_cookie_check DB writer"
```

---

## Task 4: doctor.refresh_cookie_status (with injected cookie_checker)

**Files:**
- Modify: `tests/test_doctor.py` (extend with refresh_cookie_status tests)
- Modify: `wxsp/doctor.py` (add `CookieStatusRow` + `refresh_cookie_status`)

`refresh_cookie_status(session, *, cookie_checker, now_fn=datetime.now)` iterates every account in DB (no `is_active` filter — doctor is diagnostic, paused accounts still need their cookie status surfaced), calls the injected `cookie_checker(Path) -> bool`, and updates DB via `record_cookie_check`. Returns a list of `CookieStatusRow` (NamedTuple) for the CLI to format.

The `cookie_checker` injection is the key testability move: tests pass a stub that returns a fixed bool or raises; no real browser is launched.

- [ ] **Step 1: Extend `tests/test_doctor.py` with refresh_cookie_status tests**

Append at the end of `tests/test_doctor.py`:

```python
# ============== refresh_cookie_status ==============


def test_refresh_cookie_status_uses_injected_checker(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    _add_account(session, "account_a")
    _add_account(session, "account_b")

    fixed_now = datetime(2026, 5, 12, 14, 30, 0)
    calls: list[Path] = []

    def fake_checker(path: Path) -> bool:
        calls.append(path)
        return path.name == "account_a"  # only A is logged in

    rows = refresh_cookie_status(
        session,
        cookie_checker=fake_checker,
        now_fn=lambda: fixed_now,
    )
    session.commit()

    assert [p.name for p in calls] == ["account_a", "account_b"]
    assert [(r.account_id, r.status) for r in rows] == [
        ("account_a", COOKIE_STATUS_OK),
        ("account_b", COOKIE_STATUS_EXPIRED),
    ]

    a = session.get(Account, "account_a")
    b = session.get(Account, "account_b")
    assert a is not None and a.cookie_status == COOKIE_STATUS_OK
    assert a.cookie_last_active_at == fixed_now
    assert b is not None and b.cookie_status == COOKIE_STATUS_EXPIRED
    assert b.cookie_last_active_at is None


def test_refresh_cookie_status_handles_checker_exception_as_unknown(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    _add_account(session, "account_a")
    fixed_now = datetime(2026, 5, 12, 14, 30, 0)

    def crashing_checker(path: Path) -> bool:
        raise RuntimeError("simulated patchright crash")

    rows = refresh_cookie_status(
        session,
        cookie_checker=crashing_checker,
        now_fn=lambda: fixed_now,
    )
    session.commit()

    assert [(r.account_id, r.status) for r in rows] == [
        ("account_a", COOKIE_STATUS_UNKNOWN),
    ]
    a = session.get(Account, "account_a")
    assert a is not None
    assert a.cookie_status == COOKIE_STATUS_UNKNOWN
    assert a.cookie_last_checked_at == fixed_now


def test_refresh_cookie_status_empty_db_returns_empty_list(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    rows = refresh_cookie_status(
        session,
        cookie_checker=lambda _p: True,
        now_fn=lambda: datetime(2026, 5, 12, 14, 30, 0),
    )

    assert rows == []


def test_refresh_cookie_status_includes_inactive_accounts(session: Session) -> None:
    """Doctor is diagnostic: surface every account regardless of is_active."""
    from wxsp.doctor import refresh_cookie_status

    active = _add_account(session, "account_a")
    paused = _add_account(session, "account_b")
    paused.is_active = False
    paused.paused_until = datetime(2026, 6, 1)
    session.add(paused)
    session.commit()

    rows = refresh_cookie_status(
        session,
        cookie_checker=lambda _p: True,
        now_fn=lambda: datetime(2026, 5, 12, 14, 30, 0),
    )

    assert {r.account_id for r in rows} == {"account_a", "account_b"}


def test_refresh_cookie_status_passes_pathlib_path_to_checker(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    _add_account(session, "account_a")
    received: list[object] = []

    def fake_checker(path: Path) -> bool:
        received.append(path)
        return True

    refresh_cookie_status(
        session,
        cookie_checker=fake_checker,
        now_fn=lambda: datetime(2026, 5, 12, 14, 30, 0),
    )

    assert len(received) == 1
    assert isinstance(received[0], Path)
    assert str(received[0]) == "/tmp/profiles/account_a"
```

- [ ] **Step 2: Run tests — they should fail**

Run: `uv run pytest tests/test_doctor.py -v -k refresh`
Expected: ALL 5 new tests FAIL with `ImportError: cannot import name 'refresh_cookie_status'`.

- [ ] **Step 3: Commit RED**

```bash
git add tests/test_doctor.py
git commit -m "test: add failing tests for doctor.refresh_cookie_status"
```

- [ ] **Step 4: Implement `refresh_cookie_status`**

Open `wxsp/doctor.py` and add (after the existing imports, before `record_cookie_check`):

```python
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from sqlmodel import select
```

(merge with the existing import block; keep imports sorted by `ruff`)

Then at the bottom of the file, add:

```python
CookieChecker = Callable[[Path], bool]


class CookieStatusRow(NamedTuple):
    """`refresh_cookie_status` 返回行,CLI 输出层用。"""

    account_id: str
    status: str
    last_active_at: datetime | None


def refresh_cookie_status(
    session: Session,
    *,
    cookie_checker: CookieChecker,
    now_fn: Callable[[], datetime] = datetime.now,
) -> list[CookieStatusRow]:
    """对所有账号跑一次 cookie 检查,回写状态。**调用方负责 commit**。

    `cookie_checker` 是注入点:传一个 `Path -> bool` 的回调。生产代码传
    `wxsp.browser.check_cookie`(真打开浏览器);测试传一个 stub。

    `cookie_checker` 抛异常 → 该账号被标 `unknown`,不影响其它账号继续检查。

    返回每账号一行 `CookieStatusRow`,顺序与 `Account.id` 字典序一致。
    """
    accounts = session.exec(select(Account).order_by(Account.id)).all()
    rows: list[CookieStatusRow] = []
    for account in accounts:
        now = now_fn()
        try:
            is_logged_in: bool | None = cookie_checker(Path(account.user_data_dir))
        except Exception:
            is_logged_in = None
        record_cookie_check(session, account.id, is_logged_in=is_logged_in, now=now)
        rows.append(
            CookieStatusRow(
                account_id=account.id,
                status=account.cookie_status,
                last_active_at=account.cookie_last_active_at,
            )
        )
    return rows
```

Final imports block of `wxsp/doctor.py` should look like:

```python
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from sqlmodel import Session, select

from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)
```

- [ ] **Step 5: Run tests — they should pass**

Run: `uv run pytest tests/test_doctor.py -v`
Expected: ALL 10 doctor tests PASS (5 record + 5 refresh).

- [ ] **Step 6: Run full suite**

Run: `uv run pytest -q`
Expected: 62 tests pass.

- [ ] **Step 7: Commit GREEN**

```bash
git add wxsp/doctor.py
git commit -m "feat(doctor): implement refresh_cookie_status with injected checker"
```

---

## Task 5: browser module — context factory + login-state polling

**Files:**
- Create: `tests/test_browser.py`
- Modify: `wxsp/browser.py` (currently 1-line docstring)

`browser.py` exposes three things:
1. `WECHAT_CHANNELS_HOME` and `LOGGED_IN_SELECTOR` constants.
2. `browser_context(user_data_dir, *, headless=False)` — context manager yielding a `Page`. Launches a patchright persistent context, injects `INIT_SCRIPT`, ensures `user_data_dir` exists.
3. `wait_for_logged_in(page, *, timeout_ms)` — navigates to home, polls for logged-in selector. Returns `bool`.
4. `check_cookie(user_data_dir, *, timeout_ms=15_000)` — composes the above for the typical one-shot use case (doctor, plus M5 publisher's `verify_logged_in`).

Tests are intentionally limited — actual browser launch is too heavy for CI. We assert constants and that the public surface imports without error. Real-browser verification happens in Task 8's manual smoke.

- [ ] **Step 1: Write failing tests**

Create `tests/test_browser.py`:

```python
"""Smoke tests on the browser module's public surface.

Browser-touching code (`browser_context`, `wait_for_logged_in`) is exercised
manually in M2 acceptance (Task 8) and by `wxsp login` against the test account
— launching real Chromium under pytest is too heavy and not CI-safe.
"""

from __future__ import annotations


def test_wechat_channels_home_constant_is_https():
    from wxsp.browser import WECHAT_CHANNELS_HOME

    assert WECHAT_CHANNELS_HOME.startswith("https://")
    assert "channels.weixin.qq.com" in WECHAT_CHANNELS_HOME


def test_logged_in_selector_targets_publish_buttons():
    from wxsp.browser import LOGGED_IN_SELECTOR

    # The selector must mention a button/text that only appears post-login.
    assert "发表" in LOGGED_IN_SELECTOR or "发布" in LOGGED_IN_SELECTOR


def test_public_callables_importable():
    from wxsp.browser import browser_context, check_cookie, wait_for_logged_in

    assert callable(browser_context)
    assert callable(check_cookie)
    assert callable(wait_for_logged_in)


def test_check_cookie_passes_user_data_dir_through_to_browser_context(tmp_path, monkeypatch):
    """`check_cookie` is a thin wrapper: hand `user_data_dir` to `browser_context`,
    call `wait_for_logged_in`, return its bool.

    We monkeypatch `browser_context` and `wait_for_logged_in` to record their args.
    Real chromium launch is exercised manually in Task 8 acceptance, not under pytest.
    """
    from contextlib import contextmanager
    from wxsp import browser as browser_mod

    udd = tmp_path / "chrome-profiles" / "test_account"
    seen_dirs: list = []
    seen_timeouts: list = []
    sentinel_page = object()

    @contextmanager
    def fake_context(user_data_dir, *, headless=False):
        seen_dirs.append(user_data_dir)
        yield sentinel_page

    def fake_wait(page, *, timeout_ms):
        assert page is sentinel_page
        seen_timeouts.append(timeout_ms)
        return True

    monkeypatch.setattr(browser_mod, "browser_context", fake_context)
    monkeypatch.setattr(browser_mod, "wait_for_logged_in", fake_wait)

    result = browser_mod.check_cookie(udd, timeout_ms=1234)
    assert result is True
    assert seen_dirs == [udd]
    assert seen_timeouts == [1234]


def test_check_cookie_returns_false_when_wait_returns_false(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from wxsp import browser as browser_mod

    @contextmanager
    def fake_context(user_data_dir, *, headless=False):
        yield object()

    def fake_wait(page, *, timeout_ms):
        return False

    monkeypatch.setattr(browser_mod, "browser_context", fake_context)
    monkeypatch.setattr(browser_mod, "wait_for_logged_in", fake_wait)

    result = browser_mod.check_cookie(tmp_path / "udd", timeout_ms=1000)
    assert result is False
```

- [ ] **Step 2: Run tests — they should fail**

Run: `uv run pytest tests/test_browser.py -v`
Expected: ALL 5 tests FAIL with `ImportError: cannot import name 'WECHAT_CHANNELS_HOME' from 'wxsp.browser'` (the module is currently empty except for a docstring).

- [ ] **Step 3: Commit RED**

```bash
git add tests/test_browser.py
git commit -m "test: add failing tests for browser module surface and check_cookie"
```

- [ ] **Step 4: Implement `wxsp/browser.py`**

Replace the contents of `wxsp/browser.py` with:

```python
"""patchright context 工厂 + 视频号登录态轮询(M2)。

设计要点:
  - 每账号独立 `user_data_dir`(persistent context),cookie / localStorage 由
    persistent context 自动持久化,**不**再单独维护 cookie.json。
  - 视频号风控敏感:**永远** `headless=False`(默认值),即使是 doctor 的快速检查
    也开窗。CLAUDE.md 明确"严禁 headless 跑视频号"。
  - 选择器集中在本模块的 `LOGGED_IN_SELECTOR`;M5 publisher 的发布步骤选择器会
    集中到 `wxsp.selectors`(改版时唯一改动点)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from patchright.sync_api import Page, sync_playwright

from wxsp.stealth_js import INIT_SCRIPT

WECHAT_CHANNELS_HOME = "https://channels.weixin.qq.com"

# "已登录"标记:视频号主页/发布页登录后才出现的元素。任一可见 → 视为已登录。
# 选择器来自 social-auto-upload/uploader/tencent_uploader/main.py 的踩坑成果。
LOGGED_IN_SELECTOR = (
    'div:has-text("发表视频"), '
    'button:has-text("发表"), '
    'button:has-text("发布视频")'
)


@contextmanager
def browser_context(
    user_data_dir: Path,
    *,
    headless: bool = False,
) -> Iterator[Page]:
    """打开账号专属 persistent Chrome context,注入 stealth init script。

    - `user_data_dir` 不存在会自动创建(适配 `wxsp accounts add` 后第一次 login)。
    - persistent context 启动时会有一个默认 page(about:blank),直接复用。
    - 退出时 close context;cookie 已由 persistent context 写到 user_data_dir。
    """
    user_data_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context.add_init_script(INIT_SCRIPT)
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            context.close()


def wait_for_logged_in(page: Page, *, timeout_ms: int) -> bool:
    """导航到视频号主页,轮询 `LOGGED_IN_SELECTOR` 直到可见或超时。

    - `timeout_ms` ≥ 视频号页面 DOM ready 时间(经验值:5-15s)+ 扫码等待时间。
    - login 场景:`timeout_ms=300_000`(5 分钟,够扫码)。
    - doctor 场景:`timeout_ms=15_000`(已登录的话主页几秒就出按钮)。

    返回 True = 找到登录标记;False = 超时(扫码未完成 / cookie 失效 / 网络问题)。
    """
    page.goto(WECHAT_CHANNELS_HOME, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(LOGGED_IN_SELECTOR, timeout=timeout_ms, state="visible")
        return True
    except Exception:
        # patchright 超时抛 TimeoutError,网络/导航错误抛其它子类。任何异常都视为
        # "没找到登录标记" —— 真正可补救的错误(网络等)在 M5 publisher 层细分。
        return False


def check_cookie(user_data_dir: Path, *, timeout_ms: int = 15_000) -> bool:
    """一站式:开浏览器 → 检查登录态 → 关浏览器。

    被 `wxsp doctor`(`refresh_cookie_status` 的 cookie_checker)和 `wxsp login`
    复用。`login` 传 `timeout_ms=300_000` 等扫码完成。
    """
    with browser_context(user_data_dir) as page:
        return wait_for_logged_in(page, timeout_ms=timeout_ms)
```

- [ ] **Step 5: Run tests — they should pass**

Run: `uv run pytest tests/test_browser.py -v`
Expected: ALL 5 tests PASS.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest -q`
Expected: 67 tests pass.

- [ ] **Step 7: Commit GREEN**

```bash
git add wxsp/browser.py
git commit -m "feat(browser): add patchright persistent context + login-state polling"
```

---

## Task 6: CLI `wxsp login <account_id>` command

**Files:**
- Create: `tests/test_cli_login.py`
- Modify: `wxsp/cli.py` (replace `login` placeholder body)

The `login` handler:
1. Looks up the account in DB → exits 1 if not found.
2. Reads `user_data_dir` (str) → converts to `Path`. Closes the session.
3. Calls `check_cookie(user_data_dir, timeout_ms=300_000)` — this is the long-running step where the user scans QR. Session is **closed during this 5-minute window** so we don't hold a connection open.
4. Opens a new session, calls `record_cookie_check(session, account_id, is_logged_in=<bool>, now=datetime.now())`.
5. Prints success / failure, exits 1 on failure.

Tests use `monkeypatch.setattr(wxsp.cli, "check_cookie", stub)` to short-circuit the browser.

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_login.py`:

```python
"""CLI `wxsp login <account_id>` tests.

Browser is stubbed via monkeypatch — we never launch real Chromium under pytest.
The CLI is the orchestration layer; what we verify here is:
  - account-not-found path → exit 1, no DB write
  - successful login → cookie_status='ok' + cookie_last_active_at bumped
  - failed login → cookie_status='expired' + exit 1
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    return db_path


def _add_account(db_path: Path, account_id: str = "account_a") -> None:
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            Account(
                id=account_id,
                display_name=f"display-{account_id}",
                user_data_dir=f"/tmp/profiles/{account_id}",
            )
        )
        session.commit()


def test_login_unknown_account_exits_with_error(db_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["login", "missing"])

    assert result.exit_code != 0
    assert "不存在" in result.output


def test_login_success_marks_cookie_ok_and_bumps_last_active(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)
    captured: list[Path] = []

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        captured.append(path)
        assert timeout_ms >= 60_000, "login must use a generous timeout for QR scan"
        return True

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code == 0, result.output

    # browser path was the str-converted user_data_dir
    assert captured == [Path("/tmp/profiles/account_a")]

    # DB state
    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == COOKIE_STATUS_OK
        assert account.cookie_last_active_at is not None
        assert account.cookie_last_checked_at is not None


def test_login_failure_marks_cookie_expired_and_exits_nonzero(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        return False  # simulated scan timeout

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code != 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == COOKIE_STATUS_EXPIRED
        assert account.cookie_last_active_at is None
        assert account.cookie_last_checked_at is not None


def test_login_browser_crash_marks_cookie_unknown(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)

    def crashing_check(path: Path, *, timeout_ms: int) -> bool:
        raise RuntimeError("simulated patchright crash")

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", crashing_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code != 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == COOKIE_STATUS_UNKNOWN
        assert account.cookie_last_active_at is None


def test_login_outputs_chinese_success_message(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env)

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        return True

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["login", "account_a"])
    assert result.exit_code == 0
    assert "account_a" in result.output
    assert "登录成功" in result.output
```

- [ ] **Step 2: Run tests — they should fail**

Run: `uv run pytest tests/test_cli_login.py -v`
Expected: Tests FAIL with output like `[wxsp] 命令 \`login account_a\` 还未实现(M0 骨架阶段)。` and exit code 0 (the placeholder body uses `_not_implemented` which does NOT exit). The success/failure tests will additionally fail their DB assertions because no write happened.

- [ ] **Step 3: Commit RED**

```bash
git add tests/test_cli_login.py
git commit -m "test: add failing tests for wxsp login command"
```

- [ ] **Step 4: Implement `login` handler**

Open `wxsp/cli.py`. Modify the imports block at the top to add:

```python
from datetime import datetime, timedelta

from wxsp.browser import check_cookie
from wxsp.doctor import record_cookie_check
```

(`datetime` and `timedelta` are already imported — verify and keep). The final import block should include those two new lines from `wxsp.browser` and `wxsp.doctor`.

Then replace the existing `login` function body (currently at lines ~40-43):

```python
@app.command("login")
def login(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    """扫码登录指定账号,刷新 Cookie。打开浏览器后扫描页面上的二维码即可。"""
    # 1. 拿 user_data_dir,session 立刻关闭(浏览器扫码可能开 5 分钟,不能持 session)
    with _open_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 不存在。先 `wxsp accounts add`。")
            raise typer.Exit(code=1)
        user_data_dir = Path(account.user_data_dir)

    # 2. 启浏览器,等扫码 / 等已登录标记可见(最长 5 分钟)
    typer.echo(f"[wxsp] 打开浏览器,请在弹出窗口中扫码登录 {account_id}(最长 5 分钟)...")
    try:
        is_logged_in: bool | None = check_cookie(user_data_dir, timeout_ms=300_000)
    except Exception as exc:
        typer.echo(f"[wxsp] 浏览器启动失败:{exc}")
        is_logged_in = None

    # 3. 回写 DB
    now = datetime.now()
    with _open_session() as session:
        record_cookie_check(session, account_id, is_logged_in=is_logged_in, now=now)

    if is_logged_in is True:
        typer.echo(f"[wxsp] ✓ 账号 {account_id} 登录成功,cookie 已持久化。")
    elif is_logged_in is False:
        typer.echo(f"[wxsp] ✗ 登录超时:未在 5 分钟内完成扫码,cookie 标记为 expired。")
        raise typer.Exit(code=1)
    else:
        typer.echo(f"[wxsp] ✗ 浏览器异常,cookie 状态标记为 unknown。")
        raise typer.Exit(code=1)
```

Make sure `Path` is imported at the top of `cli.py` — open the file and check the imports. If `pathlib.Path` is missing, add:

```python
from pathlib import Path
```

- [ ] **Step 5: Run tests — they should pass**

Run: `uv run pytest tests/test_cli_login.py -v`
Expected: ALL 5 tests PASS.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest -q`
Expected: 72 tests pass.

- [ ] **Step 7: Commit GREEN**

```bash
git add wxsp/cli.py
git commit -m "feat(cli): implement wxsp login with patchright QR scan flow"
```

---

## Task 7: CLI `wxsp doctor` command

**Files:**
- Create: `tests/test_cli_doctor.py`
- Modify: `wxsp/cli.py` (replace `doctor` placeholder body)

The `doctor` handler:
1. Opens session, loads all accounts.
2. If empty → echo hint, return (exit 0; doctor with nothing to check isn't an error).
3. Otherwise calls `refresh_cookie_status(session, cookie_checker=check_cookie)` — letting `refresh_cookie_status` drive the per-account loop and DB updates.
4. Prints a formatted table from the returned `CookieStatusRow` list.

Tests use `monkeypatch.setattr(wxsp.cli, "check_cookie", stub)` again.

For M2, the doctor output is just `账号 / Cookie / 最后活跃`. M3/M4 will add NAS + 飞书 lines.

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli_doctor.py`:

```python
"""CLI `wxsp doctor` tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.models import Account


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    return db_path


def _add_account(db_path: Path, account_id: str) -> None:
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            Account(
                id=account_id,
                display_name=f"d-{account_id}",
                user_data_dir=f"/tmp/profiles/{account_id}",
            )
        )
        session.commit()


def test_doctor_no_accounts_shows_hint(db_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "无账号" in result.output


def test_doctor_lists_each_account_with_status(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    calls: list[Path] = []

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        calls.append(path)
        assert timeout_ms <= 30_000, "doctor should use a short timeout (already-logged-in path)"
        return path.name == "account_a"  # only A is logged in

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output

    # both accounts appear in output, with their status
    assert "account_a" in result.output
    assert "account_b" in result.output
    assert "ok" in result.output
    assert "expired" in result.output

    # checker was called once per account, with the right path
    assert sorted(str(p) for p in calls) == [
        "/tmp/profiles/account_a",
        "/tmp/profiles/account_b",
    ]


def test_doctor_persists_cookie_status_to_db(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env, "account_a")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        return True

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == "ok"
        assert account.cookie_last_active_at is not None


def test_doctor_continues_after_one_account_browser_crash(
    db_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    def fake_check(path: Path, *, timeout_ms: int) -> bool:
        if path.name == "account_a":
            raise RuntimeError("simulated crash")
        return True

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        a = session.get(Account, "account_a")
        b = session.get(Account, "account_b")
        assert a is not None and a.cookie_status == "unknown"
        assert b is not None and b.cookie_status == "ok"
```

- [ ] **Step 2: Run tests — they should fail**

Run: `uv run pytest tests/test_cli_doctor.py -v`
Expected: Tests FAIL. The placeholder body prints `[wxsp] 命令 \`doctor\` 还未实现(M0 骨架阶段)。` and the DB assertions don't match.

- [ ] **Step 3: Commit RED**

```bash
git add tests/test_cli_doctor.py
git commit -m "test: add failing tests for wxsp doctor command"
```

- [ ] **Step 4: Implement `doctor` handler**

Open `wxsp/cli.py`. Add to the imports near the top (next to the `record_cookie_check` import added in Task 6):

```python
from wxsp.doctor import record_cookie_check, refresh_cookie_status
```

Then replace the existing `doctor` function (currently at lines ~124-127). `Account` and `select` are already imported at the top of `cli.py` from M1, so we can use them directly:

```python
@app.command("doctor")
def doctor() -> None:
    """健康检查:账号 / Cookie(M2)。NAS / 飞书 API 在 M3-M4 接入。"""
    # cookie_checker 注入点:生产用 wxsp.browser.check_cookie(打开浏览器);测试可 monkeypatch
    def cookie_checker(user_data_dir: Path) -> bool:
        return check_cookie(user_data_dir, timeout_ms=15_000)

    with _open_session() as session:
        # 先看有没有账号 —— 没有就给提示退出,不让 refresh_cookie_status 跑空循环
        if not session.exec(select(Account)).first():
            typer.echo("[wxsp] 无账号。先 `wxsp accounts add`,再 `wxsp login <id>` 扫码。")
            return

        rows = refresh_cookie_status(session, cookie_checker=cookie_checker)

    typer.echo(f"{'ID':<14} {'Cookie':<10} {'最后活跃':<20}")
    for row in rows:
        last_active = (
            row.last_active_at.strftime("%Y-%m-%d %H:%M") if row.last_active_at else "-"
        )
        typer.echo(f"{row.account_id:<14} {row.status:<10} {last_active:<20}")
```

- [ ] **Step 5: Run tests — they should pass**

Run: `uv run pytest tests/test_cli_doctor.py -v`
Expected: ALL 4 tests PASS.

- [ ] **Step 6: Run full suite**

Run: `uv run pytest -q`
Expected: 76 tests pass.

- [ ] **Step 7: Run pre-commit**

Run: `uv run pre-commit run --all-files`
Expected: all hooks pass (ruff, ruff-format, mypy, end-of-file-fixer, trailing-whitespace).

If `mypy` complains about `check_cookie` or `refresh_cookie_status` types, double-check imports and type annotations match the signatures defined in Tasks 4 and 5.

- [ ] **Step 8: Commit GREEN**

```bash
git add wxsp/cli.py
git commit -m "feat(cli): implement wxsp doctor with patchright cookie health check"
```

---

## Task 8: M2 acceptance — real QR scan smoke test

**Files:** None (manual verification).

This task does not write code. It runs the M2 acceptance criteria against a real test account, confirming patchright actually works on this machine and the persistent context preserves the cookie across restarts.

The user has prepared a test account for this purpose (per CLAUDE.md "测试号").

- [ ] **Step 1: Ensure a test account exists in DB**

Run: `uv run wxsp accounts list`
Expected: at least one account exists. If not, add one first:
```bash
uv run wxsp accounts add account_test \
  --display-name "测试号" \
  --user-data-dir ./data/chrome-profiles/account_test
```

- [ ] **Step 2: Run the login flow and scan QR**

Run: `uv run wxsp login account_test`
Expected:
- A Chromium window opens (not headless).
- Navigates to `https://channels.weixin.qq.com`.
- A QR code appears on the page.
- Use WeChat on phone to scan the QR.
- After scanning, the page transitions to the channels home/admin view.
- The terminal prints: `[wxsp] ✓ 账号 account_test 登录成功,cookie 已持久化。`
- The Chromium window closes.

If the page does not show the "发表视频"/"发表" button within 5 minutes after scanning, the login command will time out and print the failure message. In that case, troubleshoot (network? wrong account? page change?) before continuing.

- [ ] **Step 3: Verify DB write**

Run: `uv run wxsp accounts list`
Expected: `account_test` row shows `Cookie` column = `ok`.

- [ ] **Step 4: Verify persistent context survival**

Run: `uv run wxsp doctor`
Expected:
- Chromium window opens briefly (once per account).
- For `account_test`: navigates to home, finds the "发表" button within 15 seconds (no QR re-scan needed because cookies are reused from `data/chrome-profiles/account_test/`).
- Window closes.
- Terminal prints a table with `account_test  ok  <fresh timestamp>`.

If this step requires re-scanning, the persistent context is not actually persisting → investigate `user_data_dir` contents (`ls data/chrome-profiles/account_test/`) and that the patchright launch args are correct.

- [ ] **Step 5: Simulate cookie expiry (optional but recommended)**

Run: `rm -rf data/chrome-profiles/account_test/`
Then run: `uv run wxsp doctor`
Expected:
- Chromium opens, navigates to channels home, finds NO "发表" button (since cookies are gone, the page shows QR), times out after 15s.
- Terminal prints: `account_test  expired  -` (or the previous `cookie_last_active_at`).

This confirms doctor correctly distinguishes ok vs expired.

- [ ] **Step 6: Document completion in a wrap-up commit**

There are no code changes for this task. To record M2 completion, create an empty commit summarizing the acceptance run (replace the example outputs with your actual ones):

```bash
git commit --allow-empty -m "$(cat <<'EOF'
chore: mark M2 acceptance complete

M2 验收:
- `wxsp login account_test` 扫码登录成功,DB cookie_status=ok
- 重启 shell 后 `wxsp doctor` 直接显示 ok,未要求重新扫码(persistent context 生效)
- 删除 user_data_dir 后再 doctor 显示 expired,状态机正确

Carry-over to M3+:
- `selectors.py` 仍为空,M5 publisher 实现时再集中
- cookie_warn 状态由 M7 接 monitoring.cookie_warn_days 时启用
- doctor 暂只检查 cookie;NAS (M4) 和飞书 API (M3) 健康检查后续 milestone 接入
EOF
)"
```

---

## Post-M2 Carry-Overs

These items are intentionally deferred — listed here so M3+ planners don't re-discover them.

1. **`cookie_status = "warn"` is unused.** The constant exists for forward compatibility. M7's notify wiring will compare `now - cookie_last_active_at` against `config.monitoring.cookie_warn_days` and set `warn` for "expiring soon" cookies. Doctor at that point will surface the warning.

2. **`selectors.py` stays empty.** M5 publisher will populate it with the publish-flow selectors. M2's `LOGGED_IN_SELECTOR` stays local to `browser.py` because (a) it's the only browser-level selector and (b) the publish flow's "verify_logged_in" step in M5 will likely re-import or re-define it depending on where `selectors.py`'s structure lands.

3. **No `config.yaml` reading.** M2 currently treats DB as the only source for the account list. The `accounts:` section in `config.yaml` is unused. M5 may or may not change this — for now, `wxsp accounts add` is the canonical bootstrap.

4. **Doctor doesn't check NAS or 飞书.** Those land in M4 (NAS reachability via `nas.py`) and M3 (`feishu.py::ping()` style probe). Their output rows append to the existing cookie table.

5. **No retry on cookie check.** If the channels page is briefly unreachable during `wxsp doctor`, the account gets marked `unknown`. M7's notify layer will catch repeated `unknown` and alert — for M2 it's good enough to record the state.

6. **No `--format json` on doctor.** Plain text table only. M8 Web UI hits `refresh_cookie_status` directly via FastAPI routes, so a JSON CLI output isn't blocking.

---

## Self-Review Summary

**Spec coverage:**
- CLAUDE.md M2 row deliverables: ✓ `browser.py` (Task 5), ✓ `wxsp login <account>` (Task 6), ✓ `doctor` checking cookie state (Task 7), ✓ patchright + user_data_dir + init script (Tasks 1, 2, 5).
- Design doc §3.2 module split: ✓ `browser.py` (Task 5), ✓ `stealth_js.py` (Task 2), ✓ `doctor.py` (Tasks 3-4); `selectors.py` deferred to M5 (carry-over #2).
- Acceptance criteria: ✓ QR scan works (Task 8 Step 2), ✓ restart preserves cookie (Task 8 Step 4), ✓ doctor outputs cookie_status per account (Task 7).
- CLAUDE.md hard constraints: ✓ headless=False default (Task 5), ✓ Path() wrap of `user_data_dir` (Tasks 5, 6, 7), ✓ persistent context (Task 5), ✓ no cookie.json (Task 5).

**Type consistency:**
- `record_cookie_check(session, account_id, *, is_logged_in: bool | None, now: datetime)` — same signature in Tasks 3, 4, 6, 7.
- `refresh_cookie_status(session, *, cookie_checker, now_fn)` — same signature in Tasks 4, 7.
- `check_cookie(user_data_dir: Path, *, timeout_ms: int)` — same signature in Tasks 5, 6, 7.
- `CookieStatusRow(account_id: str, status: str, last_active_at: datetime | None)` — defined Task 4, consumed Task 7.

**Placeholder scan:** No TBD / TODO / "add error handling" / etc. All code blocks are complete and runnable.
