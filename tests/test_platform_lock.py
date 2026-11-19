"""插件化改造完成前,非插件平台锁定:界面切换显示没权限,定时/手动/命令行都不发布。

conftest 默认关掉锁(让各平台的通用逻辑照常被测);这里用 `locked` 打开。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

import wxsp.platform_meta as pm
from tests.conftest import make_settings
from wxsp.db import get_engine, init_db
from wxsp.models import Account, Task, Video


@pytest.fixture
def locked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pm, "LOCK_UNMIGRATED", True)


def test_only_extension_platforms_are_available(locked: None) -> None:
    assert pm.is_available("pinduoduo")
    for p in ("tencent_channel", "taobao_guanghe", "douyin", "kuaishou", "xiaohongshu"):
        assert not pm.is_available(p), p
    assert not pm.is_available("nope")


def test_default_platform_skips_locked(locked: None) -> None:
    from wxsp.config import get_default_platform

    # conftest 把默认平台文件固定成 tencent_channel(已锁)
    assert get_default_platform() == "pinduoduo"


def test_daily_cron_registered_only_for_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: None
) -> None:
    from apscheduler.schedulers.background import BackgroundScheduler

    import wxsp.config as cfg
    from wxsp.scheduler import _register_daily_crons

    existing = tmp_path / "exists.yaml"
    existing.write_text("x")
    monkeypatch.setattr(cfg, "get_config_path", lambda p="tencent_channel": existing)
    monkeypatch.setattr(
        cfg, "load_settings", lambda platform="x": make_settings(tmp_path, tmp_path)
    )

    sched = BackgroundScheduler()
    assert _register_daily_crons(sched, make_settings(tmp_path, tmp_path)) == ["pinduoduo"]


def test_run_today_skips_locked_platform_without_syncing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: None
) -> None:
    import wxsp.scheduler as sch

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("锁定平台不该去同步飞书")

    monkeypatch.setattr(sch, "sync_now", boom)
    summary = sch.run_today_pending(make_settings(tmp_path, tmp_path), platform="douyin")
    assert summary.attempted == 0


def test_publish_refuses_locked_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: None
) -> None:
    from wxsp.publisher import publish

    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))
    engine = get_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with Session(engine) as s:
        s.add(
            Account(id="a", display_name="A", user_data_dir="x", daily_limit=1, platform="douyin")
        )
        s.add(Video(id="v", file_path="x.mp4", title="t", ingested_at=datetime.now()))
        task = Task(
            video_id="v",
            account_id="a",
            execute_date=date.today(),
            publish_at=datetime.now() + timedelta(hours=2),
            status="pending",
            platform="douyin",
        )
        s.add(task)
        s.commit()
        task_id = task.id
    assert task_id is not None
    with pytest.raises(ValueError, match="锁定"):
        publish(task_id, settings=make_settings(tmp_path, tmp_path))


@pytest.fixture
def client(locked: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from wxsp.api.app import create_app

    monkeypatch.setenv("WXSP_DB_PATH", str(tmp_path / "db.sqlite"))  # 不碰仓库里的真实库
    init_db(get_engine(tmp_path / "db.sqlite"))
    # 不用 with:不跑 lifespan(它会读仓库里的真实平台配置注册 cron),只测中间件和页面
    yield TestClient(create_app())


def test_locked_platform_page_shows_no_permission(client: TestClient) -> None:
    r = client.get("/?platform=douyin", follow_redirects=False)
    assert r.status_code == 403
    assert "没权限" in r.text


def test_locked_platform_write_is_refused(client: TestClient) -> None:
    r = client.post("/tasks/run-today?platform=douyin", headers={"Origin": "http://testserver"})
    assert r.status_code == 403


def test_topbar_marks_locked_platforms(client: TestClient) -> None:
    r = client.get("/tasks?platform=pinduoduo")
    assert r.status_code == 200
    assert 'data-locked="抖音"' in r.text  # 锁定平台不是链接,点击弹「没权限」
    assert 'href="/tasks?platform=douyin"' not in r.text
