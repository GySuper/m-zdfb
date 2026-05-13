"""publisher 模块单元测试(helpers + publish() 顶层编排)。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, select

from tests.conftest import make_settings
from wxsp.db import claim_task, get_engine, init_db
from wxsp.models import Account, Task, Video
from wxsp.publisher import AlreadyClaimed, PublishResult, publish, random_pause, screenshot


def test_screenshot_writes_to_yyyymm_subdir(tmp_path: Path) -> None:
    page = MagicMock()
    out = screenshot(
        page,
        task_id=42,
        step="upload",
        screenshots_root=tmp_path,
        now=datetime(2026, 5, 13, 10, 30),
    )
    assert out == tmp_path / "202605" / "42_upload.png"
    assert out.parent.is_dir()
    page.screenshot.assert_called_once_with(path=str(out), full_page=False)


def test_screenshot_does_not_propagate_page_errors(tmp_path: Path) -> None:
    page = MagicMock()
    page.screenshot.side_effect = RuntimeError("浏览器已关")
    out = screenshot(page, task_id=1, step="x", screenshots_root=tmp_path, now=datetime(2026, 1, 1))
    assert out.name == "1_x.png"


def test_random_pause_uses_injected_sleep_within_range() -> None:
    sleeps: list[float] = []
    random_pause((1.0, 3.0), sleep=sleeps.append)
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 3.0


def test_publish_result_defaults() -> None:
    r = PublishResult(task_id=1, ok=False, dry_run=True)
    assert r.remote_url is None
    assert r.screenshots == []


# =========== publish() 顶层编排 ===========


@pytest.fixture()
def pending_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    """建一个最小可用的 DB:1 account + 1 video(占位 .mp4)+ 1 pending task。

    返回 (task_id, tmp_path)。WXSP_DB_PATH 被 monkeypatch 到 tmp_path/test.sqlite,
    所以 publish() 内部的 get_engine() 会自然读这个 DB。
    """
    db_path = tmp_path / "test.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)

    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"fake-video")

    with Session(engine) as session:
        session.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir=str(tmp_path / "profile"),
                daily_limit=20,
            )
        )
        session.add(
            Video(
                id="v1",
                file_path=str(video_file),
                title="标题" * 5,
                ingested_at=datetime.now(),
            )
        )
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=date.today(),
                publish_at=datetime.now() + timedelta(hours=2),
                status="pending",
            )
        )
        session.commit()
        task = session.exec(select(Task)).first()
        assert task is not None and task.id is not None
        task_id = task.id

    return task_id, tmp_path


def test_publish_dry_run_short_circuits_before_click_publish(
    pending_task: tuple[int, Path],
) -> None:
    """dry_run=True:跑到 risk_control_probe 后停下,截图,不点发表。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    call_log: list[str] = []

    def fake_step(name: str):
        def _impl(*args, **kwargs):
            call_log.append(name)

        return _impl

    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = MagicMock(name="page")
    fake_ctx.__exit__.return_value = False

    with (
        patch("wxsp.publisher.browser_context", return_value=fake_ctx),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch.multiple(
            "wxsp.publisher",
            open_publish_page=fake_step("open"),
            verify_logged_in=fake_step("login"),
            upload_video=fake_step("upload"),
            fill_title=fake_step("title"),
            fill_description=fake_step("desc"),
            add_tags=fake_step("tags"),
            set_cover=fake_step("cover"),
            bind_topic=fake_step("topic"),
            toggle_original=fake_step("orig"),
            set_schedule=fake_step("sched"),
            risk_control_probe=fake_step("risk"),
            click_publish=fake_step("publish"),
            wait_for_success_indicator=fake_step("wait"),
            extract_remote_video_id_and_url=lambda page: (None, None),
            screenshot=lambda *a, **kw: tmp_path / "shot.png",
            random_pause=lambda *a, **kw: None,
        ),
    ):
        result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok is True, f"err={result.error_type} {result.error_msg}"
    assert result.dry_run is True
    assert "publish" not in call_log  # 关键:没点发表
    assert "wait" not in call_log
    assert call_log[-1] == "risk"  # 最后一步是 risk_control_probe


def test_publish_already_claimed_raises_if_task_in_running(
    pending_task: tuple[int, Path],
) -> None:
    """先调一次 claim_task 抢占 → 第二次 publish 抛 AlreadyClaimed。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    engine = get_engine()
    with Session(engine) as session:
        assert claim_task(session, task_id) is True

    with pytest.raises(AlreadyClaimed):
        publish(task_id, dry_run=True, settings=settings)
