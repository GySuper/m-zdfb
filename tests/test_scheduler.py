"""scheduler.py(M6)单元测试。"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from tests.conftest import make_settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Event, Task, Video
from wxsp.publisher import PublishResult
from wxsp.scheduler import (
    RunSummary,
    count_backlog,
    make_scheduler,
    mark_stale_running_as_interrupted,
    maybe_warn_backlog,
    queue_today,
    run_today_pending,
)


def _seed_account_video(session: Session, *, account_id: str = "a", video_id: str) -> None:
    # cookie_status='ok':default 'unknown' 会被 scheduler 入口的 pre-flight 跳过
    if session.get(Account, account_id) is None:
        session.add(
            Account(
                id=account_id,
                display_name="A",
                user_data_dir="/tmp",
                daily_limit=20,
                cookie_status="ok",
            )
        )
    session.add(
        Video(id=video_id, file_path="/tmp/v.mp4", title="x" * 16, ingested_at=datetime.now())
    )


def test_queue_today_returns_today_pending_ordered_by_publish_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    base = datetime.now()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v1")
        _seed_account_video(session, video_id="v2")
        _seed_account_video(session, video_id="v3")
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=base + timedelta(hours=5),
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v2",
                account_id="a",
                execute_date=today,
                publish_at=base + timedelta(hours=2),
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v3",
                account_id="a",
                execute_date=today,
                publish_at=base + timedelta(hours=3),
                status="pending",
            )
        )

    with Session(engine) as session:
        ids = queue_today(session)
        ordered_video_ids = [session.get(Task, i).video_id for i in ids]

    assert ordered_video_ids == ["v2", "v3", "v1"]


def test_queue_today_excludes_other_dates_and_non_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v_yesterday")
        _seed_account_video(session, video_id="v_today_pending")
        _seed_account_video(session, video_id="v_today_running")
        _seed_account_video(session, video_id="v_tomorrow")
        for vid, edate, status in [
            ("v_yesterday", yesterday, "pending"),
            ("v_today_pending", today, "pending"),
            ("v_today_running", today, "running"),
            ("v_tomorrow", tomorrow, "pending"),
        ]:
            session.add(
                Task(
                    video_id=vid,
                    account_id="a",
                    execute_date=edate,
                    publish_at=datetime.now(),
                    status=status,
                )
            )

    with Session(engine) as session:
        ids = queue_today(session)
        videos = [session.get(Task, i).video_id for i in ids]

    assert videos == ["v_today_pending"]


def test_mark_stale_running_as_interrupted_only_touches_expired_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    now = datetime(2026, 5, 13, 12, 0, 0)
    today = now.date()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v_running_expired")
        _seed_account_video(session, video_id="v_running_fresh")
        _seed_account_video(session, video_id="v_pending")
        _seed_account_video(session, video_id="v_success")
        # 1) running + lease 已过期 → 应被标 interrupted
        session.add(
            Task(
                video_id="v_running_expired",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="running",
                lease_expires_at=now - timedelta(minutes=5),
            )
        )
        # 2) running + lease 没过期 → 不动
        session.add(
            Task(
                video_id="v_running_fresh",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="running",
                lease_expires_at=now + timedelta(minutes=15),
            )
        )
        # 3) pending → 不动
        session.add(
            Task(
                video_id="v_pending",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )
        # 4) success → 不动
        session.add(
            Task(
                video_id="v_success",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="success",
            )
        )

    with session_scope(engine) as session:
        touched = mark_stale_running_as_interrupted(session, now=now)

    assert touched == 1

    with Session(engine) as session:
        statuses = {t.video_id: t.status for t in session.exec(select(Task)).all()}
    assert statuses["v_running_expired"] == "interrupted"
    assert statuses["v_running_fresh"] == "running"
    assert statuses["v_pending"] == "pending"
    assert statuses["v_success"] == "success"


def test_run_today_pending_calls_publish_for_each_today_task_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v1")
        _seed_account_video(session, video_id="v2")
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=now + timedelta(hours=3),
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v2",
                account_id="a",
                execute_date=today,
                publish_at=now + timedelta(hours=1),  # 更早,应先跑
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)

    call_order: list[int] = []

    def fake_publish(task_id, *, dry_run, settings):
        call_order.append(task_id)
        return PublishResult(task_id=task_id, ok=True, dry_run=False)

    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)
    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary: RunSummary = run_today_pending(settings)

    with Session(engine) as session:
        v2_task = session.exec(select(Task).where(Task.video_id == "v2")).first()
        v1_task = session.exec(select(Task).where(Task.video_id == "v1")).first()
        assert v2_task is not None and v1_task is not None
    assert call_order == [v2_task.id, v1_task.id]
    assert summary.attempted == 2
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert summary.skipped_paused == 0


def test_run_today_pending_skips_tasks_when_account_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        session.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir="/tmp",
                daily_limit=20,
                paused_until=now + timedelta(hours=24),
            )
        )
        session.add(Video(id="v1", file_path="/tmp/v.mp4", title="x" * 16, ingested_at=now))
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)

    publish_calls: list[int] = []

    def fake_publish(task_id, *, dry_run, settings):
        publish_calls.append(task_id)
        return PublishResult(task_id=task_id, ok=True, dry_run=False)

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary = run_today_pending(settings)

    assert publish_calls == []
    assert summary.attempted == 0
    assert summary.skipped_paused == 1


@pytest.mark.parametrize(
    "cookie_status,expected_reason_keyword",
    [
        ("unknown", "未完成扫码登录"),
        ("expired", "登录态已失效"),
    ],
)
def test_run_today_pending_skips_tasks_when_account_cookie_not_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cookie_status: str,
    expected_reason_keyword: str,
) -> None:
    """回归:cookie_status != ok 的账号在 run-today 入口处直接 skip,不开浏览器。

    场景:用户扫码登录窗口被关 / 浏览器异常 → record_cookie_check(is_logged_in=None)
    → cookie_status='unknown' → DB 行存在但 user_data_dir 没拿到 cookie。预期 publisher
    一开始 verify_logged_in 必败且会推一条误导性"登录态失效",所以在 scheduler 提前拦下。
    """
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        session.add(
            Account(
                id="a",
                display_name="A",
                user_data_dir="/tmp",
                daily_limit=20,
                cookie_status=cookie_status,
            )
        )
        session.add(Video(id="v1", file_path="/tmp/v.mp4", title="x" * 16, ingested_at=now))
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)

    publish_calls: list[int] = []

    def fake_publish(task_id, *, dry_run, settings):
        publish_calls.append(task_id)
        return PublishResult(task_id=task_id, ok=True, dry_run=False)

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary = run_today_pending(settings)

    assert publish_calls == [], "cookie 非 ok 的账号不该开 publisher"
    assert summary.attempted == 0
    assert summary.skipped_paused == 1
    stat = summary.per_account["a"]
    assert stat.skipped == 1
    assert stat.halt_reason is not None
    assert expected_reason_keyword in stat.halt_reason


def test_run_today_pending_emits_run_summary_with_failure_breakdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑完后推一条 run_summary,失败按 error_type 聚合 + 列 task_id/title。"""
    from wxsp import scheduler as sched_mod
    from wxsp.db import transition_task

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v_ok")
        _seed_account_video(session, video_id="v_fail")
        session.add(
            Task(
                video_id="v_ok",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v_fail",
                account_id="a",
                execute_date=today,
                publish_at=now + timedelta(hours=1),
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)

    def fake_publish(task_id, *, dry_run, settings):
        # 模拟 publish 把 last_error_type 写回 DB(真实路径走 transition_task)
        with session_scope(engine) as session:
            task = session.get(Task, task_id)
            if task and task.video_id == "v_fail":
                transition_task(
                    session,
                    task_id=task_id,
                    status="failed",
                    last_error_type="risk_control",
                    last_error_msg="boom",
                )
                return PublishResult(
                    task_id=task_id,
                    ok=False,
                    dry_run=False,
                    error_type="risk_control",
                    error_msg="boom",
                )
        return PublishResult(task_id=task_id, ok=True, dry_run=False)

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)
    captured: list = []
    monkeypatch.setattr(
        sched_mod, "notify", lambda event, *, session, settings: captured.append(event)
    )

    summary = run_today_pending(settings)

    assert summary.attempted == 2
    assert summary.failed == 1
    assert summary.succeeded == 1
    run_summary_events = [e for e in captured if e.type == "run_summary"]
    assert len(run_summary_events) == 1
    ev = run_summary_events[0]
    assert ev.level == "warn"  # 失败明细用中文 error_type(风控触发),不再是英文 risk_control
    assert "风控触发" in ev.content
    assert "risk_control" not in ev.content
    # 各账号明细 section + 简化文案("新增 X,成功 Y,失败 Z" + 缩进失败原因)
    assert "各账号明细:" in ev.content
    assert "[a] 新增 2,成功 1,失败 1" in ev.content
    assert "失败原因:风控触发" in ev.content
    # title 也按新模板,失败 > 0 用 ⚠
    assert ev.title.startswith("⚠")
    # context 也中文化
    assert ev.context == {
        "尝试": 2,
        "成功": 1,
        "失败": 1,
        "暂停跳过": 0,
    }


def test_run_today_pending_silent_run_summary_when_nothing_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 任务时不发 run_summary,避免空跑刷屏。"""
    from wxsp import scheduler as sched_mod

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)
    captured: list = []
    monkeypatch.setattr(
        sched_mod, "notify", lambda event, *, session, settings: captured.append(event)
    )

    summary = run_today_pending(settings)

    assert summary.attempted == 0
    assert captured == []


def test_run_today_pending_halts_account_after_cookie_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一账号第一条 task 出 cookie_expired → 该账号本轮剩余 task 全跳过(去重 + 省时间)。

    新语义(v0.5.3+):cookie_expired 是软失败,publisher 已把 task 回退 pending。
    scheduler 这边不计 failed,把 3 条都算成 skipped + halt_reason="登录态失效"。
    扫码续命后下轮 queue_today 自动重跑这 3 条。
    """
    from wxsp import scheduler as sched_mod

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        for vid in ("v1", "v2", "v3"):
            _seed_account_video(session, video_id=vid)
        for vid in ("v1", "v2", "v3"):
            session.add(
                Task(
                    video_id=vid,
                    account_id="a",
                    execute_date=today,
                    publish_at=now,
                    status="pending",
                )
            )

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)

    publish_calls: list[int] = []

    def fake_publish(task_id, *, dry_run, settings):
        publish_calls.append(task_id)
        # 第一次调用就 cookie_expired,后续应该根本不会被调到
        return PublishResult(
            task_id=task_id,
            ok=False,
            dry_run=False,
            error_type="cookie_expired",
            error_msg="扫码框出现",
        )

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)
    captured: list = []
    monkeypatch.setattr(
        sched_mod, "notify", lambda event, *, session, settings: captured.append(event)
    )

    summary = run_today_pending(settings)

    # 只跑了第一条,后两条直接 skip,不再调 publish
    assert len(publish_calls) == 1
    assert summary.attempted == 1
    assert summary.failed == 0  # cookie_expired 软失败,不计 failed
    assert summary.skipped_paused == 3  # 第一条(回退 pending)+ 后两条(halt 跳)

    # per_account 应有 halt_reason
    stat = summary.per_account["a"]
    assert stat.failed == 0
    assert stat.skipped == 3
    assert stat.halt_reason == "登录态失效"

    # run_summary 文案里要展示原因 + ⚠ 标题(全跳过虽 failed=0 但需要运营干预)
    run_summary_events = [e for e in captured if e.type == "run_summary"]
    assert len(run_summary_events) == 1
    ev = run_summary_events[0]
    assert ev.title.startswith("⚠")
    assert "[a] 新增 0,成功 0,失败 0,跳过 3" in ev.content
    assert "跳过原因:登录态失效" in ev.content


def test_run_today_pending_continues_after_a_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一条失败不应阻断后面的任务。"""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    now = datetime.now()

    with session_scope(engine) as session:
        _seed_account_video(session, video_id="v1")
        _seed_account_video(session, video_id="v2")
        session.add(
            Task(
                video_id="v1",
                account_id="a",
                execute_date=today,
                publish_at=now,
                status="pending",
            )
        )
        session.add(
            Task(
                video_id="v2",
                account_id="a",
                execute_date=today,
                publish_at=now + timedelta(hours=1),
                status="pending",
            )
        )

    settings = make_settings(tmp_path, tmp_path)
    monkeypatch.setattr("wxsp.scheduler.sync_now", lambda settings: None)

    calls: list[int] = []

    def fake_publish(task_id, *, dry_run, settings):
        calls.append(task_id)
        first_id = calls[0]
        is_first = task_id == first_id
        return PublishResult(
            task_id=task_id,
            ok=not is_first,
            dry_run=False,
            error_type="network" if is_first else None,
            error_msg="boom" if is_first else None,
        )

    monkeypatch.setattr("wxsp.scheduler.publish", fake_publish)

    summary = run_today_pending(settings)

    assert len(calls) == 2
    assert summary.attempted == 2
    assert summary.succeeded == 1
    assert summary.failed == 1


def test_make_scheduler_creates_scheduler_with_correct_timezone(
    tmp_path: Path,
) -> None:
    """make_scheduler() no longer registers cron jobs (that moved to start_daemon).
    It should still create a properly configured scheduler with the right timezone.
    """
    settings = make_settings(tmp_path, tmp_path)
    settings.app.timezone = "Asia/Shanghai"

    sched = make_scheduler(settings)
    # 构造后未 start,APScheduler 不让 shutdown 一个 stopped scheduler;
    # 测试只断言调度器配置。
    jobs = sched.get_jobs()
    assert len(jobs) == 0  # jobs now registered in start_daemon()
    assert str(sched.timezone) == "Asia/Shanghai"


def test_make_scheduler_no_jobs_when_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path)
    settings.scheduler.enabled = False

    sched = make_scheduler(settings)
    assert sched.get_jobs() == []


def test_register_daily_crons_skips_disabled_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_daemon 的 per-platform cron 注册必须尊重各平台 scheduler.enabled
    —— 关掉某平台不应再给它注册每日 cron。"""
    from apscheduler.schedulers.background import BackgroundScheduler

    import wxsp.config as cfg
    from wxsp.scheduler import _register_daily_crons

    existing = tmp_path / "exists.yaml"
    existing.write_text("x")
    monkeypatch.setattr(cfg, "get_config_path", lambda p="tencent_channel": existing)

    def fake_load(platform: str = "tencent_channel"):  # type: ignore[no-untyped-def]
        s = make_settings(tmp_path, tmp_path)
        s.scheduler.enabled = platform == "tencent_channel"  # 淘宝关掉
        return s

    monkeypatch.setattr(cfg, "load_settings", fake_load)

    sched = BackgroundScheduler()
    registered = _register_daily_crons(sched, make_settings(tmp_path, tmp_path))

    assert registered == ["tencent_channel"]
    assert {j.id for j in sched.get_jobs()} == {"daily_tencent_channel"}


# ============== M9 count_backlog + maybe_warn_backlog ==============


def _seed_task_with_status(
    session: Session, *, video_id: str, exec_date: date, status: str
) -> None:
    _seed_account_video(session, video_id=video_id)
    session.add(
        Task(
            video_id=video_id,
            account_id="a",
            execute_date=exec_date,
            publish_at=datetime.combine(exec_date, datetime.min.time()),
            status=status,
        )
    )


def test_count_backlog_only_counts_past_pending_and_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)

    with session_scope(engine) as session:
        # 计入
        _seed_task_with_status(session, video_id="v1", exec_date=yesterday, status="pending")
        _seed_task_with_status(session, video_id="v2", exec_date=two_days_ago, status="interrupted")
        # 不计入
        _seed_task_with_status(session, video_id="v3", exec_date=yesterday, status="success")
        _seed_task_with_status(session, video_id="v4", exec_date=yesterday, status="failed")
        _seed_task_with_status(session, video_id="v5", exec_date=today, status="pending")

    with Session(engine) as session:
        assert count_backlog(session, today=today) == 2


def test_maybe_warn_backlog_pushes_notification_when_over_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """积压 > 阈值时调用 notify();==阈值不触发。"""
    from wxsp import scheduler as sched_mod

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    yesterday = today - timedelta(days=1)

    with session_scope(engine) as session:
        for i in range(3):
            _seed_task_with_status(session, video_id=f"v{i}", exec_date=yesterday, status="pending")

    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.backlog_warn_threshold = 2  # 3 > 2 → 应触发

    captured: list = []
    monkeypatch.setattr(
        sched_mod, "notify", lambda event, *, session, settings: captured.append(event)
    )

    with Session(engine) as session:
        backlog = maybe_warn_backlog(settings, session=session)

    assert backlog == 3
    assert len(captured) == 1
    assert captured[0].type == "backlog_high"
    assert captured[0].level == "warn"
    assert captured[0].context["积压条数"] == 3


def test_maybe_warn_backlog_silent_when_at_or_under_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wxsp import scheduler as sched_mod

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    yesterday = today - timedelta(days=1)

    with session_scope(engine) as session:
        for i in range(2):
            _seed_task_with_status(session, video_id=f"v{i}", exec_date=yesterday, status="pending")

    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.backlog_warn_threshold = 2  # 2 == 阈值,不触发(spec: > 阈值)

    captured: list = []
    monkeypatch.setattr(
        sched_mod, "notify", lambda event, *, session, settings: captured.append(event)
    )

    with Session(engine) as session:
        backlog = maybe_warn_backlog(settings, session=session)

    assert backlog == 2
    assert captured == []


def test_maybe_warn_backlog_serializes_concurrent_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同进程并发 (09:00 cron 与 Web UI 撞车) 时,Lock 应让"查冷却 + insert"原子化,
    只产出一条 backlog_high(而非两条)。

    没有 Lock 的旧实现:两个线程都查不到 recent → 都进入 notify → 两条 Event。
    """
    from wxsp import scheduler as sched_mod
    from wxsp.notify import notify as real_notify

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("WXSP_DB_PATH", str(db_path))
    engine = get_engine(db_path)
    init_db(engine)
    today = date.today()
    yesterday = today - timedelta(days=1)

    with session_scope(engine) as session:
        for i in range(5):
            _seed_task_with_status(session, video_id=f"v{i}", exec_date=yesterday, status="pending")

    settings = make_settings(tmp_path, tmp_path)
    settings.monitoring.backlog_warn_threshold = 2  # 5 > 2 → 触发
    settings.monitoring.notify_on = ["backlog_high"]  # 进 notify_on 才会进外部 notifier

    # 用真 notify(它会写 Event),但禁用外部 notifier(避免起企微 HTTP)
    monkeypatch.setattr(sched_mod, "notify", real_notify)
    monkeypatch.setattr(
        "wxsp.notify.build_notifiers_from_settings", lambda s, *, platform="tencent_channel": []
    )

    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        # 用 session_scope 跟生产一致,退出时 commit Event 落库
        with session_scope(engine) as session:
            maybe_warn_backlog(settings, session=session)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # 应只产出 1 条 backlog_high Event(Lock 保证第二次进来时查到 recent → 跳过)
    with Session(engine) as session:
        rows = session.exec(select(Event).where(Event.type == "backlog_high")).all()
    assert len(rows) == 1, f"并发 backlog_high 写了 {len(rows)} 条,Lock 没生效"
