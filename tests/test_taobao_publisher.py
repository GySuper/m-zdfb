"""淘宝光合 publisher 编排的特征测试(安全网)。

抽取共享编排器前先固定 taobao `_publish_one_body` 的现有行为:
dry_run 短路 / 失败状态机 / cookie_expired 回退 / 风控暂停 / 通知 / 飞书回写。
镜像 tests/test_publisher.py 的视频号用例,换成 taobao 的步骤名 + 字段。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, select

from tests.conftest import make_settings
from wxsp.db import claim_task, get_engine, init_db
from wxsp.models import Account, Task, Video
from wxsp.publisher import AlreadyClaimed, PublishResult, publish

# patch 根:抽取前所有依赖都在 taobao_guanghe 模块命名空间里。
# 迁移到 runner 后,基础设施类的 patch 目标会改成 wxsp.platforms.runner.*。
MOD = "wxsp.platforms.taobao_guanghe"
RUNNER = "wxsp.platforms.runner"


@pytest.fixture()
def pending_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    """最小可用 DB:1 account + 1 video + 1 pending task(platform=taobao_guanghe)。"""
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
                display_name="淘宝A",
                user_data_dir=str(tmp_path / "profile"),
                daily_limit=20,
                platform="taobao_guanghe",
            )
        )
        session.add(
            Video(
                id="v1",
                file_path=str(video_file),
                title="淘宝标题" * 3,
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
                platform="taobao_guanghe",
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
    """所有 taobao 步骤函数 mock 成 no-op;显式 override 用来注入异常 / 计数。"""
    fakes = {
        "_open_publish_page": lambda *a, **kw: None,
        "_verify_logged_in": lambda *a, **kw: None,
        "_upload_video": lambda *a, **kw: None,
        "_wait_cover_generated": lambda *a, **kw: None,
        "_fill_title": lambda *a, **kw: None,
        "_fill_description": lambda *a, **kw: None,
        "_add_topic": lambda *a, **kw: None,
        "_add_products": lambda *a, **kw: None,
        "_set_schedule": lambda *a, **kw: None,
        "_set_declaration": lambda *a, **kw: None,
        "_toggle_ai_optimize": lambda *a, **kw: None,
        "_disable_download": lambda *a, **kw: None,
        "_click_publish": lambda *a, **kw: None,
        "_wait_for_success_indicator": lambda *a, **kw: None,
        "_risk_control_probe": lambda *a, **kw: None,
        "random_pause": lambda *a, **kw: None,
    }
    fakes.update(overrides)
    return fakes


def _patches(tmp_path: Path, overrides: dict):
    """统一的 with-patch 集合:基础设施 + 步骤 no-op。"""
    return (
        patch(f"{RUNNER}.browser_context", return_value=_fake_browser_ctx()),
        patch(f"{RUNNER}.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch(f"{RUNNER}.cleanup_tmp"),
        patch(f"{RUNNER}.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch.multiple(MOD, **overrides),
    )


def test_publish_loads_settings_for_task_platform_when_omitted(
    pending_task: tuple[int, Path],
) -> None:
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    fake_publisher = MagicMock()
    fake_publisher.publish_one.return_value = PublishResult(task_id, ok=True, dry_run=True)

    with (
        patch("wxsp.publisher.load_settings", return_value=settings) as load_settings,
        patch("wxsp.publisher._get_publisher", return_value=fake_publisher),
    ):
        result = publish(task_id, dry_run=True)

    assert result.ok is True
    load_settings.assert_called_once_with(platform="taobao_guanghe")
    fake_publisher.publish_one.assert_called_once_with(task_id, dry_run=True, settings=settings)


def test_publish_reloads_settings_when_source_platform_mismatches_task(
    pending_task: tuple[int, Path],
) -> None:
    task_id, tmp_path = pending_task
    wrong_settings = make_settings(tmp_path, tmp_path)
    wrong_settings._source_platform = "tencent_channel"
    correct_settings = make_settings(tmp_path, tmp_path)
    correct_settings._source_platform = "taobao_guanghe"
    from wxsp.config import AccountConfig

    correct_settings.accounts = {
        "a": AccountConfig(
            display_name="淘宝A",
            platform="taobao_guanghe",
            daily_limit=20,
            user_data_dir=tmp_path / "profile",
            video_search_root=tmp_path,
            cover_search_root=tmp_path,
        )
    }
    fake_publisher = MagicMock()
    fake_publisher.publish_one.return_value = PublishResult(task_id, ok=True, dry_run=True)

    with (
        patch("wxsp.publisher.load_settings", return_value=correct_settings) as load_settings,
        patch("wxsp.publisher._get_publisher", return_value=fake_publisher),
    ):
        result = publish(task_id, dry_run=True, settings=wrong_settings)

    assert result.ok is True
    load_settings.assert_called_once_with(platform="taobao_guanghe")
    fake_publisher.publish_one.assert_called_once_with(
        task_id, dry_run=True, settings=correct_settings
    )


def test_wait_for_success_rejects_unexpected_redirect() -> None:
    from wxsp.errors import ElementNotFound
    from wxsp.platforms.taobao_guanghe import _wait_for_success_indicator

    page = MagicMock()
    page.url = "https://login.taobao.com/member/login.jhtml"

    with pytest.raises(ElementNotFound, match="成功判定超时"):
        _wait_for_success_indicator(page, timeout=0)


def test_wait_for_success_ignores_hidden_indicator() -> None:
    from wxsp.errors import ElementNotFound
    from wxsp.platforms import taobao_selectors as sel
    from wxsp.platforms.taobao_guanghe import _wait_for_success_indicator

    page = MagicMock()
    page.url = sel.PUBLISH_PAGE_URL
    hidden = MagicMock()
    hidden.first = hidden
    hidden.is_visible.return_value = False
    page.locator.return_value = hidden

    with (
        patch("wxsp.platforms.taobao_guanghe.time.time", side_effect=[0, 0, 2]),
        patch("wxsp.platforms.taobao_guanghe.time.sleep"),
        patch("wxsp.platforms.taobao_guanghe._iframe") as iframe,
        pytest.raises(ElementNotFound, match="成功判定超时"),
    ):
        iframe.return_value.locator.return_value = hidden
        _wait_for_success_indicator(page, timeout=1)


def test_add_products_uses_current_card_and_checkbox_dom() -> None:
    from wxsp.platforms import taobao_selectors as sel
    from wxsp.platforms.taobao_guanghe import _add_products

    pid = "1040198270412"
    page = MagicMock()
    iframe = MagicMock()
    trigger = MagicMock()
    heading = MagicMock()
    any_link = MagicMock()
    result_link = MagicMock()
    search = MagicMock()
    confirm = MagicMock()
    card = MagicMock()
    checkbox = MagicMock()
    any_link.first = MagicMock()
    result_link.first = result_link
    checkbox.first = checkbox

    locators = {
        sel.PRODUCT_TRIGGER: trigger,
        sel.PRODUCT_DIALOG_HEADING: heading,
        sel.PRODUCT_ITEM_LINK_ANY: any_link,
        sel.PRODUCT_SEARCH_INPUT: search,
        sel.PRODUCT_ITEM_LINK_BY_ID.format(pid=pid): result_link,
        sel.PRODUCT_CONFIRM_BUTTON: confirm,
    }
    iframe.locator.side_effect = locators.__getitem__

    def locate_card(selector: str) -> MagicMock:
        assert selector == 'xpath=ancestor::div[.//input[@type="checkbox"]][1]'
        return card

    def locate_checkbox(selector: str) -> MagicMock:
        assert selector == 'input.next-checkbox-input[type="checkbox"]'
        return checkbox

    result_link.locator.side_effect = locate_card
    card.locator.side_effect = locate_checkbox

    with (
        patch(f"{MOD}._iframe", return_value=iframe),
        patch(f"{MOD}.expect") as expect_checkbox,
        patch(f"{MOD}.time.sleep"),
    ):
        _add_products(page, [pid])

    search.fill.assert_called_once_with(pid)
    search.press.assert_called_once_with("Enter")
    card.hover.assert_called_once_with()
    checkbox.click.assert_called_once_with()
    expect_checkbox.assert_called_once_with(checkbox)
    expect_checkbox.return_value.to_be_checked.assert_called_once_with(timeout=3_000)
    confirm.click.assert_called_once_with()


def test_dry_run_short_circuits_before_click_publish(pending_task: tuple[int, Path]) -> None:
    """dry_run=True:跑到 risk 后停下,不点发布。"""
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    call_log: list[str] = []

    def fake(name: str):
        def _impl(*a, **kw):
            call_log.append(name)

        return _impl

    overrides = _noop_steps(
        _open_publish_page=fake("open"),
        _verify_logged_in=fake("login"),
        _upload_video=fake("upload"),
        _risk_control_probe=fake("risk"),
        _click_publish=fake("publish"),
        _wait_for_success_indicator=fake("wait"),
    )
    p1, p2, p3, p4, p5 = _patches(tmp_path, overrides)
    with p1, p2, p3, p4, p5:
        result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok is True, f"err={result.error_type} {result.error_msg}"
    assert result.dry_run is True
    assert "publish" not in call_log
    assert "wait" not in call_log
    assert call_log[-1] == "risk"


def test_already_claimed_raises(pending_task: tuple[int, Path]) -> None:
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    engine = get_engine()
    with Session(engine) as session:
        assert claim_task(session, task_id) is True
    with pytest.raises(AlreadyClaimed):
        publish(task_id, dry_run=True, settings=settings)


def test_failure_writes_status_failed_with_screenshot(pending_task: tuple[int, Path]) -> None:
    from wxsp.errors import UploadFailed

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    def raise_upload(*_a, **_kw):
        raise UploadFailed("上传中断")

    shots: list[str] = []

    def fake_screenshot(page, *, task_id, step, screenshots_root, now=None):
        shots.append(step)
        return tmp_path / f"{task_id}_{step}.png"

    overrides = _noop_steps(_upload_video=raise_upload)
    with (
        patch(f"{RUNNER}.browser_context", return_value=_fake_browser_ctx()),
        patch(f"{RUNNER}.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch(f"{RUNNER}.cleanup_tmp"),
        patch(f"{RUNNER}.screenshot", side_effect=fake_screenshot),
        patch.multiple(MOD, **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    assert result.error_type == "upload_failed"
    assert "step=upload" in (result.error_msg or "")
    assert "err_upload" in shots

    engine = get_engine()
    with Session(engine) as session:
        task_db = session.get(Task, task_id)
        assert task_db is not None
        assert task_db.status == "failed"
        assert task_db.last_error_type == "upload_failed"
        assert task_db.screenshots_json != "[]"
        assert task_db.finished_at is not None


def test_cookie_expired_keeps_task_pending(pending_task: tuple[int, Path]) -> None:
    from wxsp.errors import CookieExpired

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    def raise_cookie(*_a, **_kw):
        raise CookieExpired("登录态失效")

    overrides = _noop_steps(_verify_logged_in=raise_cookie)
    p1, p2, p3, p4, p5 = _patches(tmp_path, overrides)
    with p1, p2, p3, p4, p5:
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.error_type == "cookie_expired"
    engine = get_engine()
    with Session(engine) as session:
        task_db = session.get(Task, task_id)
        assert task_db is not None
        assert task_db.status == "pending"
        assert task_db.lease_token is None
        assert task_db.finished_at is None
        assert task_db.attempts == 1
        acc = session.get(Account, "a")
        assert acc is not None
        assert acc.cookie_status == "expired"


def test_risk_control_pauses_account_24h(pending_task: tuple[int, Path]) -> None:
    from wxsp.errors import RiskControl

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    def raise_risk(*_a, **_kw):
        raise RiskControl("操作过于频繁")

    overrides = _noop_steps(_risk_control_probe=raise_risk)
    p1, p2, p3, p4, p5 = _patches(tmp_path, overrides)
    with p1, p2, p3, p4, p5:
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.error_type == "risk_control"
    engine = get_engine()
    with Session(engine) as session:
        acc = session.get(Account, "a")
        assert acc is not None and acc.paused_until is not None
        delta = acc.paused_until - datetime.now()
        assert timedelta(hours=23, minutes=59, seconds=50) < delta <= timedelta(hours=24)


def test_dry_run_success_resets_claim_residue(pending_task: tuple[int, Path]) -> None:
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    overrides = _noop_steps()
    p1, p2, p3, p4, p5 = _patches(tmp_path, overrides)
    with p1, p2, p3, p4, p5:
        result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok is True
    engine = get_engine()
    with Session(engine) as session:
        task_db = session.get(Task, task_id)
        assert task_db is not None
        assert task_db.status == "pending"
        assert task_db.attempts == 0
        assert task_db.lease_token is None
        assert task_db.started_at is None
        assert task_db.finished_at is None


def test_failure_writes_event_with_mapped_notify_type(pending_task: tuple[int, Path]) -> None:
    from wxsp.errors import RiskControl
    from wxsp.models import Event

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    def raise_risk(*_a, **_kw):
        raise RiskControl("操作过于频繁")

    overrides = _noop_steps(_risk_control_probe=raise_risk)
    p1, p2, p3, p4, p5 = _patches(tmp_path, overrides)
    with p1, p2, p3, p4, p5:
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is False
    engine = get_engine()
    with Session(engine) as session:
        rows = list(session.exec(select(Event)).all())
        assert len(rows) == 1
        ev = rows[0]
        assert ev.type == "risk_control"
        assert ev.task_id == task_id
        assert ev.account_id == "a"
        assert ev.platform == "taobao_guanghe"


def test_success_does_not_write_notify_event(pending_task: tuple[int, Path]) -> None:
    from wxsp.models import Event

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)

    overrides = _noop_steps()
    p1, p2, p3, p4, p5 = _patches(tmp_path, overrides)
    with p1, p2, p3, p4, p5:
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
    engine = get_engine()
    with Session(engine) as session:
        assert list(session.exec(select(Event)).all()) == []


def _enable_feishu_writeback(settings) -> None:
    settings.feishu.enabled = True
    settings.feishu.app_id = "cli_test"
    settings.feishu.app_secret = "test_secret"
    settings.feishu.bitable.app_token = "appT"
    settings.feishu.bitable.table_id = "tblT"
    settings.feishu.sync.write_back_enabled = True


def test_success_writes_back_status_to_feishu(pending_task: tuple[int, Path]) -> None:
    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)

    captured: dict[str, object] = {}

    def fake_writeback(client, *, app_token, table_id, record_id, fields):
        captured["record_id"] = record_id
        captured["fields"] = fields

    overrides = _noop_steps()
    with (
        patch(f"{RUNNER}.browser_context", return_value=_fake_browser_ctx()),
        patch(f"{RUNNER}.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch(f"{RUNNER}.cleanup_tmp"),
        patch(f"{RUNNER}.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch(f"{RUNNER}.make_client", return_value=MagicMock()),
        patch(f"{RUNNER}.writeback_row", side_effect=fake_writeback),
        patch.multiple(MOD, **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.ok is True
    assert captured["record_id"] == "v1"
    # taobao 不抽取 remote_url → 只回写状态
    assert captured["fields"] == {"状态": "已发布"}


def test_cookie_expired_skips_feishu_writeback(pending_task: tuple[int, Path]) -> None:
    from wxsp.errors import CookieExpired

    task_id, tmp_path = pending_task
    settings = make_settings(tmp_path, tmp_path)
    _enable_feishu_writeback(settings)

    def raise_cookie(*_a, **_kw):
        raise CookieExpired("登录态失效")

    overrides = _noop_steps(_verify_logged_in=raise_cookie)
    with (
        patch(f"{RUNNER}.browser_context", return_value=_fake_browser_ctx()),
        patch(f"{RUNNER}.stage_to_tmp", return_value=tmp_path / "v.mp4"),
        patch(f"{RUNNER}.cleanup_tmp"),
        patch(f"{RUNNER}.screenshot", side_effect=lambda *a, **kw: tmp_path / "s.png"),
        patch(f"{RUNNER}.make_client", return_value=MagicMock()) as mc,
        patch(f"{RUNNER}.writeback_row") as wb,
        patch.multiple(MOD, **overrides),
    ):
        result = publish(task_id, dry_run=False, settings=settings)

    assert result.error_type == "cookie_expired"
    mc.assert_not_called()
    wb.assert_not_called()
