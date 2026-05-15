"""publisher.publish() 装故障注入。

`check_pass()=False` 时,publisher 在 step [4] 之后必须:
- sleep 45-75 秒(注入点用 monkeypatch 短路)
- 截图保存到 screenshots/{YYYYMM}/{task_id}_wait_upload_area.png
- raise ElementNotFound("等待上传区域超时(60s)")

风格对齐 tests/test_publisher.py:make_settings fixture from conftest,
WXSP_DB_PATH 经 monkeypatch 到 tmp_path,数据库 seed 在 fixture 里。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, select

from tests.conftest import make_settings
from wxsp.db import get_engine, init_db
from wxsp.models import Account, Task, Video
from wxsp.publisher import publish


@pytest.fixture
def pending_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    """最小可用 DB:1 account + 1 video + 1 pending task。复用 test_publisher.py 风格。"""
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


def _fake_browser_ctx() -> MagicMock:
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = MagicMock(name="page")
    fake_ctx.__exit__.return_value = False
    return fake_ctx


def _noop_steps(**overrides):
    """所有步骤函数 mock 成 no-op;显式 override 用来注入异常 / 计数。"""
    fakes = {
        "open_publish_page": lambda *a, **kw: None,
        "verify_logged_in": lambda *a, **kw: None,
        "upload_video": lambda *a, **kw: None,
        "fill_title": lambda *a, **kw: None,
        "fill_description": lambda *a, **kw: None,
        "add_tags": lambda *a, **kw: None,
        "set_cover": lambda *a, **kw: None,
        "bind_topic": lambda *a, **kw: None,
        "toggle_original": lambda *a, **kw: None,
        "set_schedule": lambda *a, **kw: None,
        "risk_control_probe": lambda *a, **kw: None,
        "click_publish": lambda *a, **kw: None,
        "wait_for_success_indicator": lambda *a, **kw: None,
        "extract_remote_video_id_and_url": lambda page: (None, None),
        "random_pause": lambda *a, **kw: None,
    }
    fakes.update(overrides)
    return fakes


def test_publish_apc_pass_runs_full_pipeline(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_pass=True 时,publisher 跑过 upload_video(并最终成功)。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    monkeypatch.setattr("wxsp.apc.check_pass", lambda: True)

    upload_calls: list[bool] = []
    overrides = _noop_steps(
        upload_video=lambda *a, **kw: upload_calls.append(True),
        extract_remote_video_id_and_url=lambda page: ("rid", "https://channels/x"),
    )

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx()),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", return_value=tmp_path / "shot.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True, f"err={result.error_type} {result.error_msg}"
    assert upload_calls == [True]


def test_publish_apc_deny_injects_element_not_found(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_pass=False 时:不调 upload / title / click_publish,error_type=element_not_found。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    monkeypatch.setattr("wxsp.apc.check_pass", lambda: False)
    # 跳过真实 45-75s 等待
    monkeypatch.setattr("wxsp.publisher.time.sleep", lambda _s: None)
    monkeypatch.setattr("wxsp.publisher.random.uniform", lambda a, b: 0.0)

    upload_calls: list[bool] = []
    title_calls: list[bool] = []
    click_calls: list[bool] = []
    shot_steps: list[str] = []

    def fake_screenshot(page, *, task_id, step, screenshots_root, now=None):
        shot_steps.append(step)
        return tmp_path / f"{task_id}_{step}.png"

    overrides = _noop_steps(
        upload_video=lambda *a, **kw: upload_calls.append(True),
        fill_title=lambda *a, **kw: title_calls.append(True),
        click_publish=lambda *a, **kw: click_calls.append(True),
    )

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx()),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=fake_screenshot),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    assert result.error_type == "element_not_found"
    assert upload_calls == []
    assert title_calls == []
    assert click_calls == []
    assert "wait_upload_area" in shot_steps


def test_publish_dev_mode_no_apc_call(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest 默认 is_packaged()=False → check_pass True,不触 ApcClient。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    # 让 _client() 一旦被调到就爆炸(确认 dev-mode 短路)
    monkeypatch.setattr(
        "wxsp.apc._client", lambda: (_ for _ in ()).throw(AssertionError("dev-mode 不应调网络"))
    )
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)

    overrides = _noop_steps(
        extract_remote_video_id_and_url=lambda page: ("rid", "https://channels/x"),
    )
    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx()),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", return_value=tmp_path / "shot.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
