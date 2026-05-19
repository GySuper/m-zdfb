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


def _fake_browser_ctx(tmp_path: Path):
    """造一个 with browser_context(...) as page: 兼容的 fake,page 是 MagicMock。"""
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = MagicMock(name="page")
    fake_ctx.__exit__.return_value = False
    return fake_ctx


def _noop_steps(**overrides):
    """把所有步骤函数 mock 成 no-op,允许某些显式 override(用于注入异常)。"""
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


def test_publish_failure_writes_status_failed_with_screenshot(
    pending_task: tuple[int, Path],
) -> None:
    """步骤抛 PublisherError → DB 写 failed + last_error_type + 截图记录。"""
    from wxsp.errors import CookieExpired
    from wxsp.models import Task

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    def raise_cookie_expired(*_a, **_kw):
        raise CookieExpired("二维码出来了")

    shots_captured: list[str] = []

    def fake_screenshot(page, *, task_id, step, screenshots_root, now=None):
        shots_captured.append(step)
        return tmp_path / f"{task_id}_{step}.png"

    overrides = _noop_steps(verify_logged_in=raise_cookie_expired)

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=fake_screenshot),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    assert result.error_type == "cookie_expired"
    assert "step=login" in (result.error_msg or "")
    assert "err_login" in shots_captured  # I1: 趁 page 还活着截图

    engine = get_engine()
    with Session(engine) as session:
        task_db = session.get(Task, task_id)
        assert task_db is not None
        assert task_db.status == "failed"
        assert task_db.last_error_type == "cookie_expired"
        assert task_db.screenshots_json != "[]"
        assert task_db.finished_at is not None
        # I3b: cookie_expired 时回写 Account.cookie_status,避免 /accounts 还显示绿色 ok
        acc_db = session.get(Account, "a")
        assert acc_db is not None
        assert acc_db.cookie_status == "expired"


def test_publish_risk_control_pauses_account_24h(
    pending_task: tuple[int, Path],
) -> None:
    """RiskControl → account.paused_until ≈ now+24h(CLAUDE.md 核心约束 §1)。"""
    from wxsp.errors import RiskControl

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    def raise_risk(*_a, **_kw):
        raise RiskControl("操作过于频繁")

    overrides = _noop_steps(risk_control_probe=raise_risk)

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    assert result.error_type == "risk_control"

    engine = get_engine()
    with Session(engine) as session:
        acc = session.get(Account, "a")
        assert acc is not None
        assert acc.paused_until is not None
        delta = acc.paused_until - datetime.now()
        # 容差 ±10s,因为 datetime.now() 在测试和实现里调用时刻不同
        assert timedelta(hours=23, minutes=59, seconds=50) < delta <= timedelta(hours=24)


def test_publish_dry_run_success_resets_claim_residue(
    pending_task: tuple[int, Path],
) -> None:
    """dry_run 成功 → task 视作没跑过:status=pending, attempts=0, lease/started_at=None。"""
    from wxsp.models import Task

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    overrides = _noop_steps()

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok is True

    engine = get_engine()
    with Session(engine) as session:
        task_db = session.get(Task, task_id)
        assert task_db is not None
        assert task_db.status == "pending"
        assert task_db.attempts == 0  # claim 加的 1 被抹掉
        assert task_db.lease_token is None
        assert task_db.lease_expires_at is None
        assert task_db.started_at is None
        assert task_db.finished_at is None


# =========== M7: publisher → notify 链路 ===========


@pytest.mark.parametrize(
    ("step_name", "exception_cls", "exception_msg", "expected_notify_type"),
    [
        ("verify_logged_in", "CookieExpired", "扫码框出现了", "cookie_expired"),
        ("risk_control_probe", "RiskControl", "操作过于频繁", "risk_control"),
        ("upload_video", "UploadFailed", "页面提示上传失败", "task_failed"),
    ],
)
def test_publish_failure_writes_event_with_mapped_notify_type(
    pending_task: tuple[int, Path],
    step_name: str,
    exception_cls: str,
    exception_msg: str,
    expected_notify_type: str,
) -> None:
    """失败时按 error_type 映射写 Event(M7):cookie/risk/upload 三种代表场景。"""
    from wxsp import errors as err_mod
    from wxsp.models import Event

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    exc_cls = getattr(err_mod, exception_cls)

    def raise_it(*_a, **_kw):
        raise exc_cls(exception_msg)

    overrides = _noop_steps(**{step_name: raise_it})

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False

    engine = get_engine()
    with Session(engine) as session:
        rows = list(session.exec(select(Event)).all())
        assert len(rows) == 1, f"应有 1 行 Event, 实有 {len(rows)}"
        ev = rows[0]
        assert ev.type == expected_notify_type
        assert ev.task_id == task_id
        assert ev.account_id == "a"
        # 错误类型 + 最近步骤 进 context_json(中文 key)
        ctx_json = ev.context_json
        assert "错误类型" in ctx_json
        assert "最近步骤" in ctx_json


def test_publish_success_does_not_write_notify_event(
    pending_task: tuple[int, Path],
) -> None:
    """成功路径不发任何告警,Event 表为空(M7 设计:成功不刷屏)。"""
    from wxsp.models import Event

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    overrides = _noop_steps()

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True

    engine = get_engine()
    with Session(engine) as session:
        rows = list(session.exec(select(Event)).all())
        assert rows == []


def test_publish_dry_run_failure_skips_notify_event(
    pending_task: tuple[int, Path],
) -> None:
    """dry-run 期间步骤抛错也算 failure,但 dry-run 是开发场景,不告警。"""
    from wxsp.errors import UploadFailed
    from wxsp.models import Event

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    def raise_upload(*_a, **_kw):
        raise UploadFailed("dev failure")

    overrides = _noop_steps(upload_video=raise_upload)

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok is False  # dry-run 也算失败

    engine = get_engine()
    with Session(engine) as session:
        rows = list(session.exec(select(Event)).all())
        assert rows == []  # dry-run 不告警


# =========== M7: publisher → 飞书回写链路 ===========


def _enable_feishu_writeback(settings) -> None:
    settings.feishu.enabled = True
    settings.feishu.app_id = "cli_test"
    settings.feishu.app_secret = "test_secret"
    settings.feishu.bitable.app_token = "appT"
    settings.feishu.bitable.table_id = "tblT"
    settings.feishu.sync.write_back_enabled = True


def test_publish_success_writes_back_status_and_url_to_feishu(
    pending_task: tuple[int, Path],
) -> None:
    """成功 → 飞书回写 状态=已发布 + 已发布链接(remote_url 有值时)。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)

    captured: dict[str, object] = {}

    def fake_writeback(client, *, app_token, table_id, record_id, fields):
        captured["record_id"] = record_id
        captured["fields"] = fields

    overrides = _noop_steps(
        extract_remote_video_id_and_url=lambda page: ("vid123", "https://channels/x/vid123")
    )

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch("wxsp.publisher.make_client", return_value=MagicMock()),
        patch("wxsp.publisher.writeback_row", side_effect=fake_writeback),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
    assert captured["record_id"] == "v1"  # = video.id
    fields = captured["fields"]
    assert fields == {"状态": "已发布", "已发布链接": "https://channels/x/vid123"}


def test_publish_success_without_remote_url_writes_back_status_only(
    pending_task: tuple[int, Path],
) -> None:
    """定时任务到点前拿不到 remote_url → 只回写 状态=已发布,不写空链接。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)

    captured: dict[str, object] = {}

    def fake_writeback(client, *, app_token, table_id, record_id, fields):
        captured["fields"] = fields

    overrides = _noop_steps()  # extract 默认返回 (None, None)

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch("wxsp.publisher.make_client", return_value=MagicMock()),
        patch("wxsp.publisher.writeback_row", side_effect=fake_writeback),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
    assert captured["fields"] == {"状态": "已发布"}


def test_publish_failure_writes_back_status_failed_with_error_message(
    pending_task: tuple[int, Path],
) -> None:
    """失败 → 飞书回写 状态=失败 + 错误信息。"""
    from wxsp.errors import CookieExpired

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)

    captured: dict[str, object] = {}

    def fake_writeback(client, *, app_token, table_id, record_id, fields):
        captured["fields"] = fields

    def raise_cookie(*_a, **_kw):
        raise CookieExpired("二维码出现")

    overrides = _noop_steps(verify_logged_in=raise_cookie)

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch("wxsp.publisher.make_client", return_value=MagicMock()),
        patch("wxsp.publisher.writeback_row", side_effect=fake_writeback),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    fields = captured["fields"]
    assert fields["状态"] == "失败"
    assert "step=login" in fields["错误信息"]
    assert "二维码" in fields["错误信息"]


def test_publish_dry_run_skips_feishu_writeback(
    pending_task: tuple[int, Path],
) -> None:
    """dry-run 不真发,不回写飞书(避免污染 Bitable 状态)。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)

    overrides = _noop_steps()

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch("wxsp.publisher.make_client", return_value=MagicMock()) as mc,
        patch("wxsp.publisher.writeback_row") as wb,
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok is True
    mc.assert_not_called()
    wb.assert_not_called()


def test_publish_feishu_disabled_skips_writeback(
    pending_task: tuple[int, Path],
) -> None:
    """feishu.enabled=False → 不构造 client,不回写。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    # 默认 enabled=False;留作 sanity check
    assert settings.feishu.enabled is False

    overrides = _noop_steps()

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch("wxsp.publisher.make_client") as mc,
        patch("wxsp.publisher.writeback_row") as wb,
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
    mc.assert_not_called()
    wb.assert_not_called()


def test_publish_write_back_disabled_skips_writeback(
    pending_task: tuple[int, Path],
) -> None:
    """feishu.sync.write_back_enabled=False → 不回写(企业内允许只读模式)。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)
    settings.feishu.sync.write_back_enabled = False

    overrides = _noop_steps()

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch("wxsp.publisher.make_client") as mc,
        patch("wxsp.publisher.writeback_row") as wb,
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
    mc.assert_not_called()
    wb.assert_not_called()


def test_publish_writeback_feishu_api_error_does_not_propagate(
    pending_task: tuple[int, Path],
) -> None:
    """飞书 API 持续失败 → 不影响 publish() 返回 ok=True(回写是次要副作用)。"""
    from wxsp.feishu import FeishuApiError

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)

    overrides = _noop_steps()

    with (
        patch("wxsp.publisher.browser_context", return_value=_fake_browser_ctx(tmp_path)),
        patch("wxsp.publisher.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch("wxsp.publisher.cleanup_tmp"),
        patch("wxsp.publisher.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch("wxsp.publisher.make_client", return_value=MagicMock()),
        patch("wxsp.publisher.writeback_row", side_effect=FeishuApiError("API 持续失败")),
        patch.multiple("wxsp.publisher", **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True  # 主流程成功
