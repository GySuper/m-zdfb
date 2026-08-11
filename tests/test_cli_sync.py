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

# 二次同步测试用:改正后的新标题(18~30 字,合规)
_RESYNC_NEW_TITLE = "改过的新标题视频内容编号十八个字以上凑数用"


def _edited_row(record_id: str, base: BitableRow, *, title: str | None = None) -> BitableRow:
    """复制一行飞书记录,可改标题,record_id 保持不变(模拟运营改同一行)。"""
    fields = dict(base.fields)
    if title is not None:
        fields["标题"] = title
    return BitableRow(record_id=record_id, fields=fields)


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
                },
                "accounts": {
                    "account_a": {
                        "display_name": "测试号",
                        "daily_limit": 20,
                        "user_data_dir": str(tmp_path / "p" / "a"),
                        "video_search_root": str(video_root),
                        "cover_search_root": str(cover_root),
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
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr(
        "wxsp.sync.fetch_pending_rows",
        lambda client, **kw: list(rows),
    )

    def fake_writeback(client: Any, *, record_id: str, fields: dict[str, Any], **kw: Any) -> None:
        fake.writebacks.append((record_id, fields))

    monkeypatch.setattr("wxsp.sync.writeback_row", fake_writeback)
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "app_token=tok" not in result.output

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


def test_sync_second_run_overwrites_pending(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(5)]

    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))

    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    r1 = runner.invoke(app, ["sync"])
    assert r1.exit_code == 0
    assert len([w for w in writebacks if w[1].get("状态") == "已计划"]) == 5

    writebacks.clear()
    r2 = runner.invoke(app, ["sync"])
    assert r2.exit_code == 0
    assert "覆盖更新: 5" in r2.output
    assert len([w for w in writebacks if w[1].get("状态") == "已计划"]) == 5

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
    assert len(tasks) == 5


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
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: object())
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    called = {"writeback": 0}
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda *a, **kw: called.__setitem__("writeback", called["writeback"] + 1),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

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

    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: object())

    def boom(client: Any, **kw: Any) -> list[BitableRow]:
        raise FeishuApiError("simulated")

    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", boom)

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 70


def test_resync_overwrites_pending_row(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0

    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title=_RESYNC_NEW_TITLE)]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0
    assert "覆盖更新: 1" in out.output

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        videos = session.exec(select(Video)).all()
        tasks = session.exec(select(Task)).all()
    assert len(videos) == 1 and len(tasks) == 1
    assert videos[0].title == _RESYNC_NEW_TITLE
    assert tasks[0].status == "pending"
    assert tasks[0].attempts == 0
    assert any(f.get("状态") == "已计划" and f.get("错误信息") == "" for _, f in writebacks)


def test_resync_overwrites_failed_row_and_resets(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    monkeypatch.setattr("wxsp.sync.writeback_row", lambda client, *, record_id, fields, **kw: None)
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        task = session.exec(select(Task)).one()
        task.status = "failed"
        task.attempts = 3
        task.last_error_type = "upload_failed"
        task.last_error_msg = "boom"
        task.finished_at = now
        session.add(task)
        session.commit()

    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0
    assert "覆盖更新: 1" in out.output

    with Session(engine) as session:
        task = session.exec(select(Task)).one()
    assert task.status == "pending"
    assert task.attempts == 0
    assert task.last_error_type is None
    assert task.last_error_msg is None
    assert task.finished_at is None


def test_resync_refuses_published_row(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0
    original_title = _happy_row(0, now).fields["标题"]

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        task = session.exec(select(Task)).one()
        task.status = "success"
        task.remote_url = "http://example.com/v1"
        session.add(task)
        session.commit()

    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title=_RESYNC_NEW_TITLE)]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0
    assert "覆盖更新: 0" in out.output

    with Session(engine) as session:
        video = session.exec(select(Video)).one()
        task = session.exec(select(Task)).one()
    assert video.title == original_title
    assert task.status == "success"
    assert any(
        f.get("状态") == "已发布" and "已发布成功" in (f.get("错误信息") or "")
        for _, f in writebacks
    )


def test_resync_skips_running_row_without_writeback(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0
    original_title = _happy_row(0, now).fields["标题"]

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        task = session.exec(select(Task)).one()
        task.status = "running"
        session.add(task)
        session.commit()

    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title=_RESYNC_NEW_TITLE)]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0

    with Session(engine) as session:
        video = session.exec(select(Video)).one()
        task = session.exec(select(Task)).one()
    assert video.title == original_title
    assert task.status == "running"
    assert all(rid != "rec_ok_0" for rid, _ in writebacks)


def test_resync_rejects_now_invalid_row(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0
    original_title = _happy_row(0, now).fields["标题"]

    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title="短")]
    writebacks.clear()
    out = runner.invoke(app, ["sync"])
    assert out.exit_code == 0

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        video = session.exec(select(Video)).one()
        task = session.exec(select(Task)).one()
    assert video.title == original_title
    assert task.status == "pending"
    assert any(f.get("状态") == "失败" and (f.get("错误信息") or "") for _, f in writebacks)


def test_resync_dry_run_overwrite_writes_nothing(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(0, now)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    writebacks: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    assert runner.invoke(app, ["sync"]).exit_code == 0
    original_title = _happy_row(0, now).fields["标题"]

    # 改标题后 dry-run 再同步:应报"覆盖更新"但不落库、不回写
    rows[:] = [_edited_row("rec_ok_0", _happy_row(0, now), title=_RESYNC_NEW_TITLE)]
    writebacks.clear()
    out = runner.invoke(app, ["sync", "--dry-run"])
    assert out.exit_code == 0
    assert "覆盖更新: 1 (dry-run)" in out.output

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        video = session.exec(select(Video)).one()
    assert video.title == original_title  # DB 未被改动
    assert writebacks == []  # dry-run 不回写


def test_sync_rejects_rows_over_daily_limit(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同账号同日任务数超过 daily_limit → 多出来的行拒绝入库 + 回写"失败"。
    CLAUDE.md「若某账号当日任务数 > daily_limit → validator 拒绝入库 + 飞书回写错误」。
    """
    # 把上限调到 2,给 3 行合规(同账号同执行日期)→ 只入 2 行,第 3 行超额被拒。
    cfg_path = sync_env["tmp_path"] / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    data["accounts"]["account_a"]["daily_limit"] = 2
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(3)]  # video_0/1/2 都存在
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: fake.writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
    assert len(tasks) == 2  # 只入了 daily_limit 个

    rejected = [w for w in fake.writebacks if w[1].get("状态") == "失败"]
    assert len(rejected) == 1
    assert "上限" in rejected[0][1].get("错误信息", "")


def test_sync_aborts_gracefully_when_nas_unreachable(
    sync_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """校验阶段 NAS 掉线(NasUnreachable)→ 中止本轮 sync,不崩、不入库、不回写"失败"。
    CLAUDE.md §5:NAS 不可达走重试,不能误判成"文件不存在"回写飞书。
    """
    from wxsp.errors import NasUnreachable

    now = datetime(2026, 5, 12, 9, 0)
    rows = [_happy_row(i, now) for i in range(3)]
    fake = _FakeClient(rows)
    monkeypatch.setattr("wxsp.sync.make_client", lambda app_id, app_secret: fake)
    monkeypatch.setattr("wxsp.sync.fetch_pending_rows", lambda client, **kw: list(rows))
    monkeypatch.setattr(
        "wxsp.sync.writeback_row",
        lambda client, *, record_id, fields, **kw: fake.writebacks.append((record_id, fields)),
    )
    monkeypatch.setattr("wxsp.sync.datetime", _FrozenDatetime(now))

    def _boom(*a: Any, **k: Any) -> None:
        raise NasUnreachable("NAS down")

    monkeypatch.setattr("wxsp.sync.validate", _boom)

    runner = CliRunner()
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output  # 没崩

    engine = get_engine(sync_env["db_path"])
    with Session(engine) as session:
        tasks = session.exec(select(Task)).all()
    assert len(tasks) == 0  # NAS 掉线 → 中止,没入库
    # 不把任何行回写"失败"(留待 NAS 恢复后下轮重试)
    assert not [w for w in fake.writebacks if w[1].get("状态") == "失败"]
