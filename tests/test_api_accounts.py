"""Accounts 路由(M8)冒烟测试。

策略:
- override get_session / get_settings
- pause/resume:确认 DB.paused_until 写入
- login:确认后台线程被启动(monkeypatch threading.Thread.start)
- sync:确认 sync_now 被调用(monkeypatch)
- GET /accounts:确认列表渲染
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

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
            display_name="美食号",
            daily_limit=20,
            user_data_dir=tmp_path / "a",
            video_search_root=tmp_path / "videos",
            cover_search_root=tmp_path / "covers",
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


def test_login_triggers_background_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """login POST 启动后台 thread 跑 _run_login(check_cookie 在里头开浏览器)。
    测试里把 _run_login 替成 noop,只验证 thread 被起 + redirect 到位 + 收尾清理。
    """
    calls: list[tuple[str, Path]] = []

    def fake_run_login(
        account_id: str, user_data_dir: Path, *, platform: str = "tencent_channel"
    ) -> None:
        calls.append((account_id, user_data_dir))

    monkeypatch.setattr(routes_accounts, "_run_login", fake_run_login)
    r = client.post("/accounts/account_a/login", follow_redirects=False)
    assert r.status_code == 303
    # noop 跑得很快,几十毫秒内 thread 应该已经退出 + 自清
    for _ in range(50):  # 最多等 1s
        t = routes_accounts._login_in_flight.get("account_a")
        if t is None or not t.is_alive():
            break
        time.sleep(0.02)
    assert len(calls) == 1
    assert calls[0][0] == "account_a"
    assert isinstance(calls[0][1], Path)
    # finally 块清掉 _login_in_flight
    assert "account_a" not in routes_accounts._login_in_flight


def test_login_dedups_when_already_in_flight(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一次 login 起线程,第二次同账号 login 应该 *不* 起新线程,直接 redirect 提示。

    回归:运营点了两次扫码登录 → chromium 抢同一个 user_data_dir,后开的崩了 +
    DB cookie_status 被覆盖成 unknown。
    """
    start_event = threading.Event()
    release_event = threading.Event()

    def slow_run_login(
        account_id: str, user_data_dir: Path, *, platform: str = "tencent_channel"
    ) -> None:
        start_event.set()
        release_event.wait(timeout=5.0)  # 卡住线程,模拟"还在扫码中"

    monkeypatch.setattr(routes_accounts, "_run_login", slow_run_login)
    # 第一次 POST → 起线程,线程卡住
    r1 = client.post("/accounts/account_a/login", follow_redirects=False)
    assert r1.status_code == 303
    assert "已弹出浏览器" in unquote(r1.headers["location"])
    assert start_event.wait(timeout=2.0), "第一个 thread 没起来"

    # 第二次 POST → 不起新线程,提示已在扫码
    r2 = client.post("/accounts/account_a/login", follow_redirects=False)
    assert r2.status_code == 303
    assert "已在扫码中" in unquote(r2.headers["location"])

    # 放第一个线程退出,清理 in_flight
    release_event.set()
    for _ in range(50):
        if "account_a" not in routes_accounts._login_in_flight:
            break
        time.sleep(0.02)
    assert "account_a" not in routes_accounts._login_in_flight


def test_login_creates_db_row_if_missing(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 配置里加个还没入 DB 的账号
    settings = client.app.dependency_overrides[get_settings]()  # type: ignore[attr-defined]
    settings.accounts["account_b"] = AccountConfig(
        display_name="新号",
        daily_limit=10,
        user_data_dir=tmp_path / "b",
        video_search_root=tmp_path / "videos_b",
        cover_search_root=tmp_path / "covers_b",
    )
    monkeypatch.setattr(routes_accounts, "_spawn", lambda *a, **kw: None)

    r = client.post("/accounts/account_b/login", follow_redirects=False)
    assert r.status_code == 303

    engine = get_engine(tmp_path / "db.sqlite")
    with session_scope(engine) as s:
        assert s.get(Account, "account_b") is not None


def test_open_browser_triggers_background_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open-browser POST 启动后台 thread 跑 _run_open_browser(里头开浏览器停主页)。
    测试把 _run_open_browser 替成 noop,只验证 thread 被起 + redirect + 收尾清理。
    """
    calls: list[tuple[str, Path]] = []

    def fake_run_open(
        account_id: str, user_data_dir: Path, *, platform: str = "tencent_channel"
    ) -> None:
        calls.append((account_id, user_data_dir))

    monkeypatch.setattr(routes_accounts, "_run_open_browser", fake_run_open)
    r = client.post("/accounts/account_a/open-browser", follow_redirects=False)
    assert r.status_code == 303
    assert "已打开" in unquote(r.headers["location"])
    for _ in range(50):  # 最多等 1s
        t = routes_accounts._login_in_flight.get("account_a")
        if t is None or not t.is_alive():
            break
        time.sleep(0.02)
    assert len(calls) == 1
    assert calls[0][0] == "account_a"
    assert isinstance(calls[0][1], Path)
    assert "account_a" not in routes_accounts._login_in_flight


def test_open_browser_unknown_account_404(client: TestClient) -> None:
    r = client.post("/accounts/no_such/open-browser", follow_redirects=False)
    assert r.status_code == 404


def test_open_browser_and_login_are_mutually_exclusive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 profile 只能开一个 chromium:open-browser 在跑时,同账号 login 不起新线程。

    两者共用 _login_in_flight,避免两个 chromium 抢同一个 user_data_dir。
    """
    started = threading.Event()
    release = threading.Event()
    login_calls: list[str] = []

    def slow_open(
        account_id: str, user_data_dir: Path, *, platform: str = "tencent_channel"
    ) -> None:
        started.set()
        release.wait(timeout=5.0)  # 卡住线程,模拟"窗口还开着"

    monkeypatch.setattr(routes_accounts, "_run_open_browser", slow_open)
    monkeypatch.setattr(
        routes_accounts, "_run_login", lambda account_id, *a, **k: login_calls.append(account_id)
    )

    r1 = client.post("/accounts/account_a/open-browser", follow_redirects=False)
    assert r1.status_code == 303
    assert started.wait(timeout=2.0), "open-browser thread 没起来"
    try:
        # 窗口还开着时点登录 → login 命中自己的去重分支,不起新线程
        r2 = client.post("/accounts/account_a/login", follow_redirects=False)
        assert r2.status_code == 303
        assert "已在扫码中" in unquote(r2.headers["location"])
        assert login_calls == []
    finally:
        release.set()
    for _ in range(50):
        if "account_a" not in routes_accounts._login_in_flight:
            break
        time.sleep(0.02)
    assert "account_a" not in routes_accounts._login_in_flight


def test_sync_skipped_when_feishu_disabled(client: TestClient) -> None:
    """飞书 disabled → 返回 warn 片段(HTMX),不抛 302。"""
    r = client.post("/accounts/sync", follow_redirects=False)
    assert r.status_code == 200
    assert 'class="flash warn"' in r.text
    assert "飞书未启用" in r.text


def test_sync_runs_inline_when_feishu_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """飞书 enabled → 路由内同步阻塞跑 sync_now,返回 ok 片段(无 HX-Trigger 头)。"""
    from wxsp.sync import SyncResult

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

    monkeypatch.setattr(
        "wxsp.sync.sync_now",
        lambda *a, **kw: SyncResult(pulled=3, accepted=2, rejected=1),
    )

    with TestClient(app) as c:
        r = c.post("/accounts/sync", follow_redirects=False)
        assert r.status_code == 200
        assert 'class="flash ok"' in r.text
        assert "新入库 2" in r.text
        assert "校验失败 1" in r.text
        assert "HX-Trigger" not in r.headers


def test_sync_error_returns_hx_trigger_for_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync_now 抛异常 → 路由不 500,返回红色片段 + HX-Trigger:opError 头让前端弹窗。"""
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

    def _boom(*a: object, **kw: object) -> object:
        raise RuntimeError("fake feishu down")

    monkeypatch.setattr("wxsp.sync.sync_now", _boom)

    with TestClient(app) as c:
        r = c.post("/accounts/sync", follow_redirects=False)
        assert r.status_code == 200
        assert 'class="flash error"' in r.text
        assert "fake feishu down" in r.text
        # HX-Trigger 必须是 htmx 可解的 JSON 形状,前端 event.detail.title /
        # event.detail.detail 才能取到。光检查 substring 不够 —— 如果有人误
        # 把它写成 {"opError": "字符串"} htmx 会把 detail 设成 .value,前端
        # 用 e.detail.title 就拿不到 → 弹窗只显示通用标题。
        hx_raw = r.headers.get("HX-Trigger", "")
        hx = json.loads(hx_raw)
        assert "opError" in hx
        assert isinstance(hx["opError"], dict), "opError 必须是 dict,前端按 detail.title/detail 取"
        assert hx["opError"]["title"] == "飞书同步失败"
        assert "fake feishu down" in hx["opError"]["detail"]
