"""doctor.record_cookie_check + doctor.refresh_cookie_status unit tests.

`refresh_cookie_status` accepts an injected `cookie_checker` callable so we
exercise it without ever launching a real browser.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine

from tests.conftest import make_settings
from wxsp.config import Settings
from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    COOKIE_STATUS_WARN,
    Account,
)


@pytest.fixture
def engine() -> Any:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine: Any) -> Any:
    with Session(engine) as s:
        yield s


def _add_account(session: Session, account_id: str = "account_a") -> Account:
    account = Account(
        id=account_id,
        display_name=f"display-{account_id}",
        user_data_dir=f"/tmp/profiles/{account_id}",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def test_record_cookie_check_marks_ok_and_bumps_last_active(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=True, now=now)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_OK
    assert account.cookie_last_checked_at == now
    assert account.cookie_last_active_at == now


def test_record_cookie_check_marks_expired_and_does_not_bump_last_active(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    earlier = datetime(2026, 5, 10, 12, 0, 0)
    account = _add_account(session)
    account.cookie_last_active_at = earlier
    session.add(account)
    session.commit()

    later = datetime(2026, 5, 12, 14, 30, 0)
    record_cookie_check(session, "account_a", is_logged_in=False, now=later)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_EXPIRED
    assert account.cookie_last_checked_at == later
    assert account.cookie_last_active_at == earlier  # unchanged


def test_record_cookie_check_marks_unknown_when_is_logged_in_is_none(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=None, now=now)
    session.commit()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == COOKIE_STATUS_UNKNOWN
    assert account.cookie_last_checked_at == now
    assert account.cookie_last_active_at is None


def test_record_cookie_check_missing_account_raises(session: Session) -> None:
    from wxsp.doctor import record_cookie_check

    with pytest.raises(LookupError):
        record_cookie_check(session, "does_not_exist", is_logged_in=True, now=datetime.now())


def test_record_cookie_check_does_not_commit(session: Session) -> None:
    """Mirror transition_task: caller controls the transaction."""
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 14, 30, 0)

    record_cookie_check(session, "account_a", is_logged_in=True, now=now)
    # rollback BEFORE caller commits — should undo
    session.rollback()

    account = session.get(Account, "account_a")
    assert account is not None
    assert account.cookie_status == "unknown"  # default, not "ok"
    assert account.cookie_last_active_at is None


# ============== record_cookie_check warn 阈值 ==============


def test_record_cookie_check_marks_warn_when_last_active_too_old(session: Session) -> None:
    """idle 太久(now - last_active > warn_threshold)即便本次能登录也要标 warn。"""
    from wxsp.doctor import record_cookie_check

    old = datetime(2026, 5, 10, 0, 0, 0)
    account = _add_account(session)
    account.cookie_last_active_at = old
    session.add(account)
    session.commit()

    now = datetime(2026, 5, 12, 12, 0, 0)  # 距 old 2.5 天 > 1.5 天阈值
    record_cookie_check(
        session,
        "account_a",
        is_logged_in=True,
        now=now,
        warn_threshold=timedelta(days=1.5),
    )
    session.commit()

    a = session.get(Account, "account_a")
    assert a is not None
    assert a.cookie_status == COOKIE_STATUS_WARN
    # last_active_at 仍然刷新为 now(刚验证过 cookie 活的)
    assert a.cookie_last_active_at == now


def test_record_cookie_check_stays_ok_within_warn_threshold(session: Session) -> None:
    """idle 时间 < warn_threshold 时维持 ok 不变 warn。"""
    from wxsp.doctor import record_cookie_check

    yesterday = datetime(2026, 5, 11, 12, 0, 0)
    account = _add_account(session)
    account.cookie_last_active_at = yesterday
    session.add(account)
    session.commit()

    now = datetime(2026, 5, 12, 12, 0, 0)  # 距 yesterday 1 天 < 1.5 天阈值
    record_cookie_check(
        session,
        "account_a",
        is_logged_in=True,
        now=now,
        warn_threshold=timedelta(days=1.5),
    )
    session.commit()

    a = session.get(Account, "account_a")
    assert a is not None
    assert a.cookie_status == COOKIE_STATUS_OK


def test_record_cookie_check_stays_ok_when_no_prior_last_active(session: Session) -> None:
    """从未成功过 → last_active_at is None → 不算 idle,首次成功直接 ok。"""
    from wxsp.doctor import record_cookie_check

    _add_account(session)
    now = datetime(2026, 5, 12, 12, 0, 0)

    record_cookie_check(
        session,
        "account_a",
        is_logged_in=True,
        now=now,
        warn_threshold=timedelta(days=1.5),
    )
    session.commit()

    a = session.get(Account, "account_a")
    assert a is not None
    assert a.cookie_status == COOKIE_STATUS_OK


def test_record_cookie_check_ignores_warn_when_threshold_is_none(session: Session) -> None:
    """不传 warn_threshold(login 命令场景)→ 永远不 warn,行为不变。"""
    from wxsp.doctor import record_cookie_check

    old = datetime(2026, 5, 1, 0, 0, 0)
    account = _add_account(session)
    account.cookie_last_active_at = old
    session.add(account)
    session.commit()

    now = datetime(2026, 5, 12, 12, 0, 0)  # 11 天前,任何阈值都会 warn
    record_cookie_check(session, "account_a", is_logged_in=True, now=now)  # 不传 threshold
    session.commit()

    a = session.get(Account, "account_a")
    assert a is not None
    assert a.cookie_status == COOKIE_STATUS_OK


# ============== refresh_cookie_status ==============


def test_refresh_cookie_status_uses_injected_checker(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    _add_account(session, "account_a")
    _add_account(session, "account_b")

    fixed_now = datetime(2026, 5, 12, 14, 30, 0)
    calls: list[tuple[str, Path]] = []

    def fake_checker(account_id: str, path: Path) -> bool:
        calls.append((account_id, path))
        return account_id == "account_a"  # only A is logged in

    rows = refresh_cookie_status(
        session,
        cookie_checker=fake_checker,
        now_fn=lambda: fixed_now,
    )
    session.commit()

    assert [aid for aid, _p in calls] == ["account_a", "account_b"]
    assert all(isinstance(p, Path) for _aid, p in calls)
    assert [(r.account_id, r.status) for r in rows] == [
        ("account_a", COOKIE_STATUS_OK),
        ("account_b", COOKIE_STATUS_EXPIRED),
    ]

    a = session.get(Account, "account_a")
    b = session.get(Account, "account_b")
    assert a is not None and a.cookie_status == COOKIE_STATUS_OK
    assert a.cookie_last_active_at == fixed_now
    assert b is not None and b.cookie_status == COOKIE_STATUS_EXPIRED
    assert b.cookie_last_active_at is None


def test_refresh_cookie_status_handles_checker_exception_as_unknown(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    _add_account(session, "account_a")
    fixed_now = datetime(2026, 5, 12, 14, 30, 0)

    def crashing_checker(_account_id: str, _path: Path) -> bool:
        raise RuntimeError("simulated patchright crash")

    rows = refresh_cookie_status(
        session,
        cookie_checker=crashing_checker,
        now_fn=lambda: fixed_now,
    )
    session.commit()

    assert [(r.account_id, r.status) for r in rows] == [
        ("account_a", COOKIE_STATUS_UNKNOWN),
    ]
    a = session.get(Account, "account_a")
    assert a is not None
    assert a.cookie_status == COOKIE_STATUS_UNKNOWN
    assert a.cookie_last_checked_at == fixed_now


def test_refresh_cookie_status_empty_db_returns_empty_list(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    rows = refresh_cookie_status(
        session,
        cookie_checker=lambda _a, _p: True,
        now_fn=lambda: datetime(2026, 5, 12, 14, 30, 0),
    )

    assert rows == []


def test_refresh_cookie_status_includes_inactive_accounts(session: Session) -> None:
    """Doctor is diagnostic: surface every account regardless of is_active."""
    from wxsp.doctor import refresh_cookie_status

    _add_account(session, "account_a")
    paused = _add_account(session, "account_b")
    paused.is_active = False
    paused.paused_until = datetime(2026, 6, 1)
    session.add(paused)
    session.commit()

    rows = refresh_cookie_status(
        session,
        cookie_checker=lambda _a, _p: True,
        now_fn=lambda: datetime(2026, 5, 12, 14, 30, 0),
    )

    assert {r.account_id for r in rows} == {"account_a", "account_b"}


def test_refresh_cookie_status_passes_pathlib_path_to_checker(session: Session) -> None:
    from wxsp.doctor import refresh_cookie_status

    _add_account(session, "account_a")
    received: list[tuple[str, object]] = []

    def fake_checker(account_id: str, path: Path) -> bool:
        received.append((account_id, path))
        return True

    refresh_cookie_status(
        session,
        cookie_checker=fake_checker,
        now_fn=lambda: datetime(2026, 5, 12, 14, 30, 0),
    )

    assert len(received) == 1
    assert received[0][0] == "account_a"
    assert isinstance(received[0][1], Path)
    assert str(received[0][1]) == "/tmp/profiles/account_a"


def test_refresh_cookie_status_forwards_warn_threshold(session: Session) -> None:
    """refresh 应该把 warn_threshold 透传给 record_cookie_check。"""
    from wxsp.doctor import refresh_cookie_status

    a = _add_account(session, "account_a")
    a.cookie_last_active_at = datetime(2026, 5, 10, 0, 0, 0)  # 2.5 天前
    session.add(a)
    session.commit()

    rows = refresh_cookie_status(
        session,
        cookie_checker=lambda _a, _p: True,
        now_fn=lambda: datetime(2026, 5, 12, 12, 0, 0),
        warn_threshold=timedelta(days=1.5),
    )
    session.commit()

    assert [(r.account_id, r.status) for r in rows] == [("account_a", COOKIE_STATUS_WARN)]


# ============== check_nas(按账号循环)==============


def _settings_with_account(tmp_path: Path, video_root: Path, cover_root: Path) -> Settings:
    from wxsp.config import AccountConfig

    s = make_settings(video_root, cover_root)
    s.accounts = {
        "account_a": AccountConfig(
            display_name="测试号",
            daily_limit=20,
            user_data_dir=tmp_path / "profiles" / "a",
            video_search_root=video_root,
            cover_search_root=cover_root,
        )
    }
    return s


def test_check_nas_both_paths_exist_and_are_dirs(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _settings_with_account(tmp_path, video_root, cover_root)

    rows = check_nas(settings)

    assert len(rows) == 2
    assert [(r.label, r.ok) for r in rows] == [
        ("account_a.video_search_root", True),
        ("account_a.cover_search_root", True),
    ]
    assert rows[0].path == video_root
    assert rows[1].path == cover_root


def test_check_nas_video_root_missing(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    video_root = tmp_path / "videos"  # 故意不创建
    cover_root = tmp_path / "covers"
    cover_root.mkdir()
    settings = _settings_with_account(tmp_path, video_root, cover_root)

    rows = check_nas(settings)

    assert rows[0].label == "account_a.video_search_root"
    assert rows[0].ok is False
    assert "不存在" in rows[0].detail
    assert rows[1].ok is True


def test_check_nas_path_is_file_not_dir(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    video_root = tmp_path / "videos"
    video_root.mkdir()
    cover_root = tmp_path / "covers_file"  # 故意建成文件
    cover_root.write_text("oops")
    settings = _settings_with_account(tmp_path, video_root, cover_root)

    rows = check_nas(settings)

    assert rows[0].ok is True
    assert rows[1].ok is False
    assert "不是目录" in rows[1].detail


def test_check_nas_both_missing(tmp_path: Path) -> None:
    from wxsp.doctor import check_nas

    settings = _settings_with_account(tmp_path, tmp_path / "v", tmp_path / "c")
    rows = check_nas(settings)

    assert all(not r.ok for r in rows)


# ============== check_feishu(飞书 API 探测)==============


def _settings_with_feishu(tmp_path: Path, *, enabled: bool = True) -> Settings:
    s = make_settings(tmp_path, tmp_path)
    s.feishu.enabled = enabled
    s.feishu.app_id = "cli_test"
    s.feishu.app_secret = "secret_test"
    s.feishu.bitable.app_token = "tok_test"
    s.feishu.bitable.table_id = "tbl_test"
    return s


def test_check_feishu_returns_ok_when_prober_succeeds(tmp_path: Path) -> None:
    from wxsp.doctor import check_feishu

    settings = _settings_with_feishu(tmp_path)
    called: list[Settings] = []

    def fake_prober(cfg: Settings) -> None:
        called.append(cfg)  # 不抛 = ping 成功

    row = check_feishu(settings, prober=fake_prober)

    assert called == [settings]
    assert row.ok is True
    assert "tok_test" in row.detail
    assert "tbl_test" in row.detail


def test_check_feishu_returns_failure_when_prober_raises(tmp_path: Path) -> None:
    from wxsp.doctor import check_feishu
    from wxsp.feishu import FeishuApiError

    settings = _settings_with_feishu(tmp_path)

    def crashing_prober(cfg: Settings) -> None:
        raise FeishuApiError("simulated 401 invalid token")

    row = check_feishu(settings, prober=crashing_prober)

    assert row.ok is False
    assert "simulated 401" in row.detail


def test_check_feishu_skipped_when_disabled(tmp_path: Path) -> None:
    """feishu.enabled=False 时不发起任何探测,直接返回 ok=True 说明已跳过。"""
    from wxsp.doctor import check_feishu

    settings = _settings_with_feishu(tmp_path, enabled=False)

    def must_not_call(cfg: Settings) -> None:
        raise AssertionError("prober 不应被调用")

    row = check_feishu(settings, prober=must_not_call)
    assert row.ok is True
    assert "未启用" in row.detail or "禁用" in row.detail or "跳过" in row.detail


def test_check_nas_returns_empty_when_no_accounts(tmp_path: Path) -> None:
    """没有账号时 check_nas 返回空(谁的路径都不该检查)。"""
    from wxsp.doctor import check_nas

    settings = make_settings(tmp_path / "v", tmp_path / "c")
    assert settings.accounts == {}
    assert check_nas(settings) == []
