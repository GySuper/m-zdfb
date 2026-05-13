"""Accounts 路由(M8)冒烟测试。

策略:
- override get_session / get_settings
- pause/resume:确认 DB.paused_until 写入
- login:确认后台线程被启动(monkeypatch threading.Thread.start)
- sync:确认 sync_now 被调用(monkeypatch)
- GET /accounts:确认列表渲染
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import make_settings
from wxsp.api import routes_accounts
from wxsp.api.app import create_app
from wxsp.api.deps import get_session, get_settings
from wxsp.config import AccountConfig, Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account


def _settings_with_accounts(tmp_path: Path, feishu_enabled: bool = False) -> Settings:
    s = make_settings(tmp_path, tmp_path)
    s.accounts = {
        "account_a": AccountConfig(
            display_name="美食号", daily_limit=20, user_data_dir=tmp_path / "a"
        ),
    }
    s.feishu.enabled = feishu_enabled
    return s


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "db.sqlite"
    engine = get_engine(db_path)
    init_db(engine)
    settings = _settings_with_accounts(tmp_path)
    with session_scope(engine) as s:
        s.add(
            Account(
                id="account_a",
                display_name="美食号",
                user_data_dir=str(tmp_path / "a"),
                daily_limit=20,
                cookie_status="ok",
            )
        )

    app = create_app()

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as c:
        yield c


def test_accounts_page_renders_account_row(client: TestClient) -> None:
    r = client.get("/accounts")
    assert r.status_code == 200
    assert "account_a" in r.text
    assert "美食号" in r.text
    assert "扫码登录" in r.text


def test_pause_then_resume_updates_db(client: TestClient, tmp_path: Path) -> None:
    r1 = client.post("/accounts/account_a/pause", data={"hours": "12"}, follow_redirects=False)
    assert r1.status_code == 303
    engine = get_engine(tmp_path / "db.sqlite")
    with session_scope(engine) as s:
        row = s.get(Account, "account_a")
        assert row is not None and row.paused_until is not None
        assert row.paused_until > datetime.now()

    r2 = client.post("/accounts/account_a/resume", follow_redirects=False)
    assert r2.status_code == 303
    with session_scope(engine) as s:
        row = s.get(Account, "account_a")
        assert row is not None and row.paused_until is None


def test_pause_unknown_account_404(client: TestClient) -> None:
    r = client.post("/accounts/no_such/pause", data={"hours": "1"}, follow_redirects=False)
    assert r.status_code == 404


def test_login_triggers_background_spawn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple, dict]] = []

    def fake_spawn(name: str, fn, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        calls.append((name, a, kw))

    monkeypatch.setattr(routes_accounts, "_spawn", fake_spawn)
    r = client.post("/accounts/account_a/login", follow_redirects=False)
    assert r.status_code == 303
    assert calls and calls[0][0] == "login"
    assert calls[0][1][0] == "account_a"  # account_id 传给 _run_login


def test_login_creates_db_row_if_missing(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 配置里加个还没入 DB 的账号
    settings = client.app.dependency_overrides[get_settings]()  # type: ignore[attr-defined]
    settings.accounts["account_b"] = AccountConfig(
        display_name="新号", daily_limit=10, user_data_dir=tmp_path / "b"
    )
    monkeypatch.setattr(routes_accounts, "_spawn", lambda *a, **kw: None)

    r = client.post("/accounts/account_b/login", follow_redirects=False)
    assert r.status_code == 303

    engine = get_engine(tmp_path / "db.sqlite")
    with session_scope(engine) as s:
        assert s.get(Account, "account_b") is not None


def test_sync_skipped_when_feishu_disabled(client: TestClient) -> None:
    r = client.post("/accounts/sync", follow_redirects=False)
    assert r.status_code == 303
    assert "%E8%B7%B3%E8%BF%87" in r.headers["location"] or "跳过" in r.headers["location"]


def test_sync_spawns_when_feishu_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 单独构造 client(让 feishu.enabled=True)
    db_path = tmp_path / "db.sqlite"
    engine = get_engine(db_path)
    init_db(engine)
    settings = _settings_with_accounts(tmp_path, feishu_enabled=True)

    app = create_app()

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = lambda: settings

    calls: list[str] = []
    monkeypatch.setattr(
        routes_accounts,
        "_spawn",
        lambda name, fn, *a, **kw: calls.append(name),
    )

    with TestClient(app) as c:
        r = c.post("/accounts/sync", follow_redirects=False)
        assert r.status_code == 303
    assert calls == ["feishu-sync"]
