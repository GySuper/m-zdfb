"""CLI `wxsp doctor` tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from tests.conftest import make_settings
from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.models import Account


@pytest.fixture
def db_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    return db_path


def _add_account(db_path: Path, account_id: str) -> None:
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            Account(
                id=account_id,
                display_name=f"d-{account_id}",
                user_data_dir=f"/tmp/profiles/{account_id}",
            )
        )
        session.commit()


def test_doctor_no_accounts_shows_hint(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "无账号" in result.output


def test_doctor_lists_each_account_with_status(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    calls: list[Path] = []

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        calls.append(path)
        assert timeout_ms <= 30_000, "doctor should use a short timeout (already-logged-in path)"
        return path.name == "account_a"  # only A is logged in

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    # cookie account_b 是 expired,新 doctor 因为 cookie_failed 退 1。
    # 这个测试关心的是输出内容,不是退码,所以放宽 exit_code 断言。
    assert "account_a" in result.output
    assert "account_b" in result.output
    assert "ok" in result.output
    assert "expired" in result.output

    assert sorted(str(p) for p in calls) == [
        "/tmp/profiles/account_a",
        "/tmp/profiles/account_b",
    ]


def test_doctor_persists_cookie_status_to_db(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        return True

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0  # cookie OK + NAS OK → 退 0

    engine = get_engine(db_env)
    with Session(engine) as session:
        account = session.get(Account, "account_a")
        assert account is not None
        assert account.cookie_status == "ok"
        assert account.cookie_last_active_at is not None


def test_doctor_continues_after_one_account_browser_crash(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")
    _add_account(db_env, "account_b")

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        if path.name == "account_a":
            raise RuntimeError("simulated crash")
        return True

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    # account_a 是 unknown,会触发 cookie_failed=True 退 1。
    # 这个测试关心的是 DB 里的状态而不是退码。
    _ = result.exit_code

    engine = get_engine(db_env)
    with Session(engine) as session:
        a = session.get(Account, "account_a")
        b = session.get(Account, "account_b")
        assert a is not None and a.cookie_status == "unknown"
        assert b is not None and b.cookie_status == "ok"


def test_doctor_only_checks_target_platform_accounts(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """doctor --platform X 只检查该平台账号,不碰其它平台账号
    (否则会用错平台的登录检测去开别的平台的 profile)。"""
    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as s:
        s.add(
            Account(
                id="tc_a",
                display_name="视频号A",
                user_data_dir="/tmp/profiles/tc_a",
                platform="tencent_channel",
            )
        )
        s.add(
            Account(
                id="tb_a",
                display_name="淘宝A",
                user_data_dir="/tmp/profiles/tb_a",
                platform="taobao_guanghe",
            )
        )
        s.commit()

    checked: list[str | None] = []

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        checked.append(account_id)
        return True

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    runner.invoke(app, ["doctor", "--platform", "taobao_guanghe"])

    assert checked == ["tb_a"]  # 只检查淘宝账号,视频号账号不碰


def test_doctor_cookie_warning_tagged_with_target_platform(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cookie_warning 告警必须按 doctor 的目标平台打标,而不是硬编码视频号。"""
    from datetime import datetime, timedelta

    engine = get_engine(db_env)
    init_db(engine)
    with Session(engine) as s:
        s.add(
            Account(
                id="tb_a",
                display_name="淘宝A",
                user_data_dir="/tmp/profiles/tb_a",
                platform="taobao_guanghe",
                cookie_last_active_at=datetime.now() - timedelta(days=5),  # idle 久 → warn
            )
        )
        s.commit()

    notify_platforms: list[str | None] = []

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(
        cli_module,
        "check_cookie",
        lambda path, *, timeout_ms, account_id=None, platform="tencent_channel": True,
    )
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)
    monkeypatch.setattr(
        cli_module, "notify", lambda event, **kw: notify_platforms.append(kw.get("platform"))
    )

    runner = CliRunner()
    runner.invoke(app, ["doctor", "--platform", "taobao_guanghe"])

    assert notify_platforms == ["taobao_guanghe"]


# ============== NAS section ==============


def _settings_with_account_paths(tmp_path: Path, video_root: Path, cover_root: Path):  # type: ignore[no-untyped-def]
    """构造 settings + 1 个账号(账号自带 video/cover_search_root)。"""
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


def test_doctor_prints_nas_section_when_all_ok(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        return True

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _settings_with_account_paths(tmp_path, video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "NAS" in result.output
    assert "video_search_root" in result.output
    assert "cover_search_root" in result.output


def test_doctor_exits_1_when_nas_path_missing(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _add_account(db_env, "account_a")

    def fake_check(
        path: Path,
        *,
        timeout_ms: int,
        account_id: str | None = None,
        platform: str = "tencent_channel",
    ) -> bool:
        return True

    video_root = tmp_path / "missing_videos"  # 故意不 mkdir
    cover_root = tmp_path / "covers"
    cover_root.mkdir()
    settings = _settings_with_account_paths(tmp_path, video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "check_cookie", fake_check)
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_doctor_nas_section_runs_even_without_accounts(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没账号时早 return 不应该跳过 NAS 检查 —— NAS 是独立诊断项。"""
    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    # 即便没账号,NAS section 仍然要出现
    assert "无账号" in result.output
    assert "NAS" in result.output


def test_doctor_prints_feishu_section_when_disabled(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """飞书未启用时 doctor 也要打 '飞书:' section,内容显示已跳过。"""
    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = make_settings(video_root, cover_root)  # feishu.enabled 默认 False

    from wxsp import cli as cli_module

    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert "飞书" in result.output
    assert "跳过" in result.output or "未启用" in result.output
    assert result.exit_code == 0  # 飞书禁用不算诊断失败


def test_doctor_cookie_warn_does_not_fail_exit_code(
    db_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cookie 标 warn(idle 太久但仍能登录)→ doctor exit 0,不当成失败。"""
    from datetime import datetime, timedelta

    from sqlmodel import Session as _Session

    # 先建账号,把 last_active 拨到 5 天前(远超默认 cookie_warn_days=1.5)
    _add_account(db_env, "account_a")
    engine = get_engine(db_env)
    with _Session(engine) as s:
        a = s.get(Account, "account_a")
        assert a is not None
        a.cookie_last_active_at = datetime.now() - timedelta(days=5)
        s.add(a)
        s.commit()

    video_root = tmp_path / "videos"
    cover_root = tmp_path / "covers"
    video_root.mkdir()
    cover_root.mkdir()
    settings = _settings_with_account_paths(tmp_path, video_root, cover_root)

    from wxsp import cli as cli_module

    # cookie 仍能登录 → 触发 warn 而不是 expired
    monkeypatch.setattr(
        cli_module,
        "check_cookie",
        lambda path, *, timeout_ms, account_id=None, platform="tencent_channel": True,
    )
    monkeypatch.setattr(cli_module, "load_settings", lambda platform=None: settings)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output  # warn 不算失败
    assert "warn" in result.output
