"""Plans / Config 路由(M8)冒烟测试。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date as _date
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import make_settings
from wxsp.api.app import create_app
from wxsp.api.deps import get_session, get_settings
from wxsp.api.routes_config import _mask_yaml
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Task, Video


@pytest.fixture
def client_empty(tmp_path: Path) -> Iterator[TestClient]:
    engine = get_engine(tmp_path / "db.sqlite")
    init_db(engine)
    settings = make_settings(tmp_path, tmp_path)
    app = create_app()

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


# ============== Plans ==============


def test_plans_empty_day_renders_friendly_msg(client_empty: TestClient) -> None:
    r = client_empty.get("/plans?date=2026-01-01")
    assert r.status_code == 200
    assert "没有任务" in r.text


def test_plans_groups_by_account(tmp_path: Path) -> None:
    engine = get_engine(tmp_path / "db.sqlite")
    init_db(engine)
    today = _date.today()
    with session_scope(engine) as s:
        s.add(
            Account(
                id="account_a", display_name="A", user_data_dir=str(tmp_path / "a"), daily_limit=20
            )
        )
        s.add(
            Account(
                id="account_b", display_name="B", user_data_dir=str(tmp_path / "b"), daily_limit=20
            )
        )
        for vid, aid, title in [
            ("v1", "account_a", "标题A1"),
            ("v2", "account_a", "标题A2"),
            ("v3", "account_b", "标题B1"),
        ]:
            s.add(
                Video(
                    id=vid,
                    file_path=f"/x/{vid}.mp4",
                    title=title,
                    description=None,
                    tags_json="[]",
                    cover_path=None,
                    topic=None,
                    original_claim=False,
                    file_hash=None,
                    ingested_at=datetime.now(),
                )
            )
            s.add(
                Task(
                    video_id=vid,
                    account_id=aid,
                    execute_date=today,
                    publish_at=datetime(today.year, today.month, today.day, 18),
                    status="pending",
                )
            )

    app = create_app()
    settings = make_settings(tmp_path, tmp_path)

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        r = c.get(f"/plans?date={today}")
    assert r.status_code == 200
    assert "标题A1" in r.text and "标题A2" in r.text and "标题B1" in r.text
    assert "共 3 条" in r.text
    # account_a 那一组要列出 2 条
    assert "account_a" in r.text and "account_b" in r.text


# ============== Config ==============


def test_config_renders_when_file_missing(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # 没有 config.yaml
    r = client_empty.get("/config")
    assert r.status_code == 200
    assert "未找到" in r.text or "找不到 config.yaml" in r.text


def test_config_masks_secrets(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "feishu:\n"
        "  app_id: cli_abc\n"
        "  app_secret: super_secret_value\n"
        "monitoring:\n"
        "  notifiers:\n"
        "    wecom:\n"
        "      webhook: https://qyapi.fake/key=xyz\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    r = client_empty.get("/config")
    assert r.status_code == 200
    assert "cli_abc" in r.text  # app_id 不敏感,保留
    assert "super_secret_value" not in r.text
    assert "xyz" not in r.text
    assert "***" in r.text


def test_mask_keeps_env_var_references() -> None:
    src = "app_secret: ${FEISHU_APP_SECRET}\nwebhook: ${WECOM_BOT_WEBHOOK}\n"
    out = _mask_yaml(src)
    assert "${FEISHU_APP_SECRET}" in out  # ENV 引用保留
    assert "${WECOM_BOT_WEBHOOK}" in out
    assert "***" not in out


def test_mask_normal_keys_untouched() -> None:
    src = "app:\n  data_dir: ./data\n  timezone: Asia/Shanghai"
    assert _mask_yaml(src) == src
