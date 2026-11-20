"""拼多多账密登录:配置页开关 + 凭据(掩码)、预检不跳过、自动登录成功后登录态回到 ok。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import unquote

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import make_settings
from wxsp.config import AccountConfig
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Task, Video
from wxsp.platforms.base import PublishContext, PublishResult


def _yaml_without_accounts() -> str:
    from tests.test_api_plans_config import _VALID_YAML

    data = yaml.safe_load(_VALID_YAML)
    data["accounts"] = {}
    return yaml.safe_dump(data, allow_unicode=True)


SECRET = "pw-should-never-render"  # 测试用假密码


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from wxsp.api.app import create_app

    for p in ("tencent_channel", "pinduoduo"):
        (tmp_path / f"config_{p}.yaml").write_text(_yaml_without_accounts(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    init_db(get_engine(tmp_path / "db.sqlite"))
    yield TestClient(create_app())  # 不跑 lifespan


def _accounts(tmp_path: Path, platform: str = "pinduoduo") -> dict[str, dict[str, object]]:
    data = yaml.safe_load((tmp_path / f"config_{platform}.yaml").read_text("utf-8"))
    return data["accounts"]


def _add(client: TestClient, platform: str = "pinduoduo", **extra: str) -> object:
    return client.post(
        f"/config/accounts/add?platform={platform}",
        data={
            "display_name": "多多号",
            "daily_limit": "10",
            "video_search_root": "/tmp/v",
            "cover_search_root": "/tmp/c",
            "enabled": "on",
            **extra,
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )


def test_add_with_password_login_saves_credentials_but_never_renders_password(
    client: TestClient, tmp_path: Path
) -> None:
    r = _add(client, password_login="on", login_username="13800000000", login_password=SECRET)
    assert r.status_code == 303  # type: ignore[attr-defined]
    entry = next(iter(_accounts(tmp_path).values()))
    assert entry["password_login"] is True
    assert entry["login_username"] == "13800000000"
    assert entry["login_password"] == SECRET
    page = client.get("/config?platform=pinduoduo").text
    assert SECRET not in page
    assert '"has_password": true' in page  # 编辑弹窗只知道「已设置」


def test_password_login_requires_credentials(client: TestClient, tmp_path: Path) -> None:
    r = _add(client, password_login="on", login_username="13800000000")
    assert r.status_code == 303  # type: ignore[attr-defined]
    assert "账号和密码" in unquote(r.headers["location"])  # type: ignore[attr-defined]
    assert _accounts(tmp_path) == {}


def test_update_keeps_password_when_left_blank(client: TestClient, tmp_path: Path) -> None:
    _add(client, password_login="on", login_username="u1", login_password=SECRET)
    aid = next(iter(_accounts(tmp_path)))
    form = {
        "display_name": "多多号",
        "daily_limit": "10",
        "video_search_root": "/tmp/v",
        "cover_search_root": "/tmp/c",
        "enabled": "on",
        "password_login": "on",
        "login_username": "u2",
        "login_password": "",
    }
    client.post(
        f"/config/accounts/{aid}/update?platform=pinduoduo",
        data=form,
        headers={"Origin": "http://testserver"},
    )
    entry = _accounts(tmp_path)[aid]
    assert entry["login_username"] == "u2"
    assert entry["login_password"] == SECRET  # 留空 = 保持原密码


def test_platform_without_password_login_writes_no_credential_keys(
    client: TestClient, tmp_path: Path
) -> None:
    _add(client, platform="tencent_channel")
    entry = next(iter(_accounts(tmp_path, "tencent_channel").values()))
    assert "password_login" not in entry
    assert "login_password" not in entry


def test_run_today_does_not_skip_expired_account_with_password_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wxsp.scheduler import run_today_pending

    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    engine = get_engine(tmp_path / "db.sqlite")
    init_db(engine)
    now = datetime.now()
    with session_scope(engine) as s:
        s.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir="/tmp",
                daily_limit=20,
                cookie_status="expired",
            )
        )
        s.add(Video(id="v1", file_path="/tmp/v.mp4", title="x", ingested_at=now))
        s.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=date.today(),
                publish_at=now,
                status="pending",
            )
        )
    settings = make_settings(tmp_path, tmp_path)
    settings.accounts["a"] = AccountConfig(
        display_name="A",
        daily_limit=20,
        user_data_dir=Path("/tmp"),
        video_search_root=Path("/tmp"),
        cover_search_root=Path("/tmp"),
        password_login=True,
        login_username="u",
        login_password="p",
    )
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda *a, **k: None)
    calls: list[int] = []

    def fake_publish(task_id: int, *, dry_run: bool, settings: object) -> PublishResult:
        calls.append(task_id)
        return PublishResult(task_id=task_id, ok=True, dry_run=False)

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)
    run_today_pending(settings)
    assert len(calls) == 1, "开了账密登录的账号登录态失效也要照常跑,由插件自动登录"


def test_pump_marks_relogin() -> None:
    from wxsp.platforms.pinduoduo import _pump

    ctx = MagicMock()
    ctx.result = PublishResult(task_id=1, ok=False, dry_run=True)
    session = MagicMock()
    session.next_message.side_effect = [{"type": "relogin"}, {"type": "gate"}]
    _pump(session, ctx, "gate", timeout=1)
    assert ctx.result.relogged_in is True


def test_finalize_marks_account_logged_in_after_relogin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wxsp.platforms.runner import _finalize

    engine = get_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with session_scope(engine) as s:
        s.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir="/tmp",
                daily_limit=20,
                cookie_status="expired",
            )
        )
        s.add(Video(id="v1", file_path="/tmp/v.mp4", title="x", ingested_at=datetime.now()))
        task = Task(
            video_id="v1",
            account_id="a",
            execute_date=date.today(),
            publish_at=datetime.now(),
            status="running",
        )
        s.add(task)
        s.flush()
        task_id = task.id
    assert task_id is not None
    settings = make_settings(tmp_path, tmp_path)
    result = PublishResult(task_id=task_id, ok=True, dry_run=True, relogged_in=True)
    ctx = PublishContext(
        task_id=task_id,
        result=result,
        dry_run=True,
        step_pause=(0, 0),
        screenshots_root=tmp_path,
        tmp_root=tmp_path,
        settings=settings,
    )
    bundle = MagicMock(account_id="a")
    _finalize(engine, settings, ctx, bundle, MagicMock(display_name="拼多多"))
    with Session(engine) as s:
        acc = s.get(Account, "a")
        assert acc is not None
        assert acc.cookie_status == "ok"
        assert acc.cookie_last_active_at is not None
