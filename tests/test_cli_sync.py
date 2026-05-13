"""CLI `wxsp sync` integration tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlmodel import Session, select
from typer.testing import CliRunner

from wxsp.cli import app
from wxsp.db import get_engine, init_db
from wxsp.feishu import BitableRow
from wxsp.models import Account, Task, Video


def _datetime_to_feishu_ms(dt: datetime) -> int:
    utc_dt = dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    return int(utc_dt.timestamp() * 1000)


def _date_to_feishu_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture
def sync_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """搭一个完整的 sync 测试环境:config.yaml + DB + NAS 目录 + 一个 active account。"""
    nas_root = tmp_path / "nas"
    video_root = nas_root / "videos"
    cover_root = nas_root / "covers"
    video_root.mkdir(parents=True)
    cover_root.mkdir(parents=True)
    # 准备 5 个合规视频
    for i in range(5):
        (video_root / f"video_{i}.mp4").write_bytes(b"x" * 100)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "app": {
                    "data_dir": str(tmp_path / "data"),
                    "logs_dir": str(tmp_path / "logs"),
                    "timezone": "Asia/Shanghai",
                },
                "paths": {
                    "nas_root": str(nas_root),
                    "video_search_root": str(video_root),
                    "cover_search_root": str(cover_root),
                },
                "accounts": {
                    "account_a": {
                        "display_name": "测试号",
                        "daily_limit": 20,
                        "user_data_dir": str(tmp_path / "p" / "a"),
                    },
                },
                "scheduler": {},
                "publisher": {},
                "feishu": {
                    "enabled": True,
                    "app_id": "cli_x",
                    "app_secret": "secret",
                    "bitable": {"app_token": "tok", "table_id": "tbl"},
                },
                "monitoring": {
                    "notifiers": {"wecom": {"webhook": "http://x"}},
                },
                "webui": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    db_path = tmp_path / "data" / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            Account(
                id="account_a",
                display_name="测试号",
                user_data_dir=str(tmp_path / "p" / "a"),
            )
        )
        session.commit()

    return {"db_path": db_path, "video_root": video_root, "tmp_path": tmp_path}


def _happy_row(i: int, now: datetime) -> BitableRow:
    return BitableRow(
        record_id=f"rec_ok_{i}",
        fields={
            "标题": f"测试标题视频内容编号第零{i:02d}号共十八字",  # 18 字以上
            "描述": "desc",
            "标签": [{"text": "t1"}],
            "封面文件": "",
            "合集": None,
            "原创": True,
            "账号": "account_a",
            "执行日期": _date_to_feishu_ms((now + timedelta(days=1)).date()),
            "定时发布时间": _datetime_to_feishu_ms(now + timedelta(days=1, hours=5)),
            "视频文件": f"video_{i}.mp4",
            "状态": "待入库",
        },
    )


def _bad_row(i: int, problem: str, now: datetime) -> BitableRow:
    """造一行不合规数据,problem 决定哪种错。"""
    base = dict(_happy_row(i, now).fields)
    if problem == "title":
        base["标题"] = "短"
    elif problem == "video_missing":
        base["视频文件"] = "no_such.mp4"
    elif problem == "publish_too_close":
        base["定时发布时间"] = _datetime_to_feishu_ms(now + timedelta(minutes=10))
    elif problem == "account_empty":
        base["账号"] = ""
    elif problem == "multi_error":
        base["标题"] = "短"
        base["账号"] = ""
    return BitableRow(record_id=f"rec_bad_{i}_{problem}", fields=base)


class _FakeClient:
    """模拟 feishu 模块的 lark.Client + fetch/writeback 行为。"""

    def __init__(self, rows: list[BitableRow]) -> None:
        self._rows = rows
        self.writebacks: list[tuple[str, dict[str, Any]]] = []  # (record_id, fields)


class _FrozenDatetime:
    """让 cli 模块里的 datetime.now() 返回固定时间。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self, tz: Any = None) -> datetime:
        return self._now

    def __getattr__(self, name: str) -> Any:
        import datetime as _dt

        return getattr(_dt.datetime, name)


def test_sync_happy_pipeline(sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(5)] + [
        _bad_row(0, "title", now),
        _bad_row(1, "video_missing", now),
        _bad_row(2, "publish_too_close", now),
        _bad_row(3, "account_empty", now),
        _bad_row(4, "multi_error", now),
    ]

    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr(
        "wxsp.cli.fetch_pending_rows",
        lambda client, **kw: list(rows),
    )

    def fake_writeback(client: Any, *, record_id: str, fields: dict[str, Any], **kw: Any) -> None:
        fake.writebacks.append((record_id, fields))

    monkeypatch.setattr("wxsp.cli.writeback_row", fake_writeback)
    monkeypatch.setattr("wxsp.cli.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        videos = session.exec(select(Video)).all()
        tasks = session.exec(select(Task)).all()
    assert len(videos) == 5
    assert len(tasks) == 5
    # 飞书回写:5 个 accepted("已计划")+ 5 个 rejected("失败" + error_message)
    accepted_writebacks = [w for w in fake.writebacks if w[1].get("状态") == "已计划"]
    rejected_writebacks = [w for w in fake.writebacks if w[1].get("状态") == "失败"]
    assert len(accepted_writebacks) == 5
    assert len(rejected_writebacks) == 5
    # 每个 rejected 应该有 error_message
    for _, fields in rejected_writebacks:
        assert "错误信息" in fields
        assert fields["错误信息"]  # 非空


def test_sync_second_run_skips_existing(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(5)]

    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.cli.fetch_pending_rows", lambda client, **kw: list(rows))

    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.cli.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.cli.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    # 第一遍
    r1 = runner.invoke(app, ["sync"])
    assert r1.exit_code == 0
    accepted_first = [w for w in writebacks if w[1].get("状态") == "已计划"]
    assert len(accepted_first) == 5

    # 第二遍:DB 已有 5 条 → 全部 skip,回写"已有历史任务"
    writebacks.clear()
    r2 = runner.invoke(app, ["sync"])
    assert r2.exit_code == 0
    assert len(writebacks) == 5
    for _, fields in writebacks:
        assert "已有历史任务" in (fields.get("错误信息") or "")
        assert "状态" not in fields  # 不动 status

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
    assert len(tasks) == 5  # 没新增


def test_sync_disabled_exits_zero(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 把 config.yaml 改成 feishu.enabled=false
    cfg = sync_env["tmp_path"] / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["feishu"]["enabled"] = False
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "飞书未启用" in result.output


def test_sync_dry_run_writes_nothing(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: object())
    monkeypatch.setattr("wxsp.cli.fetch_pending_rows", lambda client, **kw: list(rows))
    called = {"writeback": 0}
    monkeypatch.setattr(
        "wxsp.cli.writeback_row",
        lambda *a, **kw: called.__setitem__("writeback", called["writeback"] + 1),
    )
    monkeypatch.setattr("wxsp.cli.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    result = runner.invoke(app, ["sync", "--dry-run"])
    assert result.exit_code == 0
    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        assert session.exec(select(Video)).all() == []
        assert session.exec(select(Task)).all() == []
    assert called["writeback"] == 0


def test_sync_feishu_api_error_exits_70(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wxsp.feishu import FeishuApiError

    monkeypatch.setattr("wxsp.cli.make_client", lambda app_id, app_secret: object())

    def boom(client: Any, **kw: Any) -> list[BitableRow]:
        raise FeishuApiError("simulated")

    monkeypatch.setattr("wxsp.cli.fetch_pending_rows", boom)

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 70
