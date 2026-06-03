"""抖音 publisher.publish() 的 APC 故障注入(镜像 test_taobao_apc_injection.py)。

`check_pass()=False` 时,_pre_publish 必须在 step [verify_login] 之后:
- sleep 45-75 秒(注入点用 monkeypatch 短路)
- 截图保存到 screenshots/{YYYYMM}/{task_id}_wait_upload_area.png
- raise ElementNotFound("等待上传区域超时(60s)")
- 不调 upload / title / click_publish

风格对齐 tests/test_taobao_apc_injection.py:基础设施 patch 打 runner,步骤 patch 打 douyin。
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

MOD = "wxsp.platforms.douyin"
RUNNER = "wxsp.platforms.runner"


@pytest.fixture
def pending_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    """最小可用 DB:1 account + 1 video + 1 pending task(platform=douyin)。"""
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
                display_name="抖音A",
                user_data_dir=str(tmp_path / "profile"),
                daily_limit=20,
                platform="douyin",
            )
        )
        session.add(
            Video(
                id="v1",
                file_path=str(video_file),
                title="抖音标题",
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
                platform="douyin",
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
    """所有 douyin 步骤函数 mock 成 no-op;显式 override 用来注入异常 / 计数。"""
    fakes = {
        "_open_publish_page": lambda *a, **kw: None,
        "_verify_logged_in": lambda *a, **kw: None,
        "_upload_video": lambda *a, **kw: None,
        "_fill_title": lambda *a, **kw: None,
        "_fill_description": lambda *a, **kw: None,
        "_add_tags": lambda *a, **kw: None,
        "_set_cover": lambda *a, **kw: None,
        "_handle_auto_cover": lambda *a, **kw: None,
        "_set_schedule": lambda *a, **kw: None,
        "_risk_control_probe": lambda *a, **kw: None,
        "_click_publish": lambda *a, **kw: None,
        "_wait_for_success": lambda *a, **kw: None,
        "random_pause": lambda *a, **kw: None,
    }
    fakes.update(overrides)
    return fakes


def test_douyin_apc_pass_runs_full_pipeline(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_pass=True 时,publisher 跑过 _upload_video(并最终成功)。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    monkeypatch.setattr("wxsp.apc.check_pass", lambda: True)

    upload_calls: list[bool] = []
    overrides = _noop_steps(_upload_video=lambda *a, **kw: upload_calls.append(True))

    with (
        patch(f"{RUNNER}.browser_context", return_value=_fake_browser_ctx()),
        patch(f"{RUNNER}.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch(f"{RUNNER}.cleanup_tmp"),
        patch(f"{MOD}.screenshot", return_value=tmp_path / "shot.png"),
        patch.multiple(MOD, **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True, f"err={result.error_type} {result.error_msg}"
    assert upload_calls == [True]


def test_douyin_apc_deny_injects_element_not_found(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """check_pass=False 时:不调 upload / title / click,error_type=element_not_found。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    monkeypatch.setattr("wxsp.apc.check_pass", lambda: False)
    # 跳过真实 45-75s 等待(只短路 sleep;random.uniform 计算很快,无需 patch)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    upload_calls: list[bool] = []
    title_calls: list[bool] = []
    click_calls: list[bool] = []
    shot_steps: list[str] = []

    def fake_screenshot(page, *, task_id, step, screenshots_root, now=None):
        shot_steps.append(step)
        return tmp_path / f"{task_id}_{step}.png"

    overrides = _noop_steps(
        _upload_video=lambda *a, **kw: upload_calls.append(True),
        _fill_title=lambda *a, **kw: title_calls.append(True),
        _click_publish=lambda *a, **kw: click_calls.append(True),
    )

    with (
        patch(f"{RUNNER}.browser_context", return_value=_fake_browser_ctx()),
        patch(f"{RUNNER}.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch(f"{RUNNER}.cleanup_tmp"),
        patch(f"{MOD}.screenshot", side_effect=fake_screenshot),
        patch.multiple(MOD, **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    assert result.error_type == "element_not_found"
    assert upload_calls == []
    assert title_calls == []
    assert click_calls == []
    assert "wait_upload_area" in shot_steps


def test_douyin_dev_mode_no_apc_call(
    pending_task: tuple[int, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 patch is_dev_mode=True → check_pass 短路返 True,不触 ApcClient。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    monkeypatch.setattr("wxsp.apc.is_dev_mode", lambda: True)
    monkeypatch.setattr(
        "wxsp.apc._client", lambda: (_ for _ in ()).throw(AssertionError("dev-mode 不应调网络"))
    )

    overrides = _noop_steps()
    with (
        patch(f"{RUNNER}.browser_context", return_value=_fake_browser_ctx()),
        patch(f"{RUNNER}.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch(f"{RUNNER}.cleanup_tmp"),
        patch(f"{MOD}.screenshot", return_value=tmp_path / "shot.png"),
        patch.multiple(MOD, **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
