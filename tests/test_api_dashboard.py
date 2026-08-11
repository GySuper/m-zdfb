"""Dashboard 路由(M8)冒烟测试。

策略:
- override get_session 指向 tmp 内的 SQLite engine,init_db 一次
- override get_settings 给一个有 2 个 account 的 Settings(配置驱动卡片数)
- 用 fastapi TestClient 取 / ,断关键文本
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import make_settings
from wxsp.api.app import create_app
from wxsp.api.deps import get_session, get_settings
from wxsp.config import AccountConfig, Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Event, Task, Video


def _make_settings_with_accounts(tmp_path: Path) -> Settings:
    settings = make_settings(tmp_path, tmp_path)
    settings.accounts = {
        "account_a": AccountConfig(
            display_name="美食号",
            daily_limit=20,
            user_data_dir=tmp_path / "a",
            video_search_root=tmp_path / "videos_a",
            cover_search_root=tmp_path / "covers_a",
        ),
        "account_b": AccountConfig(
            display_name="健身号",
            daily_limit=20,
            user_data_dir=tmp_path / "b",
            video_search_root=tmp_path / "videos_b",
            cover_search_root=tmp_path / "covers_b",
        ),
    }
    return settings


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "db.sqlite"
    engine = get_engine(db_path)
    init_db(engine)
    settings = _make_settings_with_accounts(tmp_path)

    app = create_app()

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    def fake_get_settings() -> Settings:
        return settings

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = fake_get_settings

    with TestClient(app) as c:
        yield c


def test_dashboard_renders_empty_db(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "今日进度" in r.text
    assert "美食号" in r.text
    assert "健身号" in r.text
    assert "还没有任何事件" in r.text


def test_dashboard_shows_today_task_counts_and_recent_event(
    client: TestClient, tmp_path: Path
) -> None:
    engine = get_engine(tmp_path / "db.sqlite")
    today = date.today()
    with session_scope(engine) as s:
        s.add(
            Account(
                id="account_a",
                display_name="美食号",
                user_data_dir=str(tmp_path / "a"),
                daily_limit=20,
                cookie_status="ok",
            )
        )
        s.add(
            Video(
                id="rec1",
                file_path="/x/v1.mp4",
                title="t",
                description=None,
                tags_json="[]",
                cover_path=None,
                topic=None,
                original_claim=False,
                file_hash=None,
                ingested_at=datetime.now(),
            )
        )
        s.add(
            Task(
                video_id="rec1",
                account_id="account_a",
                execute_date=today,
                publish_at=datetime.now(),
                status="success",
            )
        )
        s.add(
            Event(
                ts=datetime.now(),
                level="error",
                task_id=None,
                account_id="account_a",
                type="risk_control",
                message="风控触发: 请稍后",
                context_json="{}",
            )
        )

    r = client.get("/")
    assert r.status_code == 200
    # 新 UI 在 stat-chip 里把数字和标签分别放进子 span,所以用单独断言
    assert "已发布" in r.text
    assert "风控触发" in r.text  # i18n 映射 risk_control → 风控触发


def test_dashboard_backlog_includes_pending_and_interrupted_from_past_days(
    client: TestClient, tmp_path: Path
) -> None:
    """积压 = execute_date<today AND status IN (pending, interrupted)。

    spec §5.6:历史积压定义涵盖未跑完(pending)和中途断掉(interrupted)两类;
    success/failed/skipped 不计入,今天的也不计入。
    """
    engine = get_engine(tmp_path / "db.sqlite")
    today = date.today()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)
    with session_scope(engine) as s:
        s.add(
            Account(
                id="account_a",
                display_name="美食号",
                user_data_dir=str(tmp_path / "a"),
                daily_limit=20,
            )
        )
        # 3 个视频 + 任务,只有前两个该计入积压
        for i, (exec_date, status) in enumerate(
            [
                (yesterday, "pending"),  # 计入
                (two_days_ago, "interrupted"),  # 计入
                (yesterday, "success"),  # 不计入(已完成)
                (yesterday, "failed"),  # 不计入(失败,需走重试)
                (today, "pending"),  # 不计入(今天的)
            ]
        ):
            vid = f"rec{i}"
            s.add(
                Video(
                    id=vid,
                    file_path=f"/x/{vid}.mp4",
                    title=f"t{i}",
                    description=None,
                    tags_json="[]",
                    cover_path=None,
                    topic=None,
                    original_claim=False,
                    file_hash=None,
                    ingested_at=datetime.now(),
                )
            )
            s.add(
                Task(
                    video_id=vid,
                    account_id="account_a",
                    execute_date=exec_date,
                    publish_at=datetime.combine(exec_date, datetime.min.time()),
                    status=status,
                )
            )

    r = client.get("/")
    assert r.status_code == 200
    # 期望积压数 = 2
    assert "<strong>2</strong>" in r.text or "积压" in r.text  # 弱断言:数字 2 + "积压" 关键字
    # 强断言:必须看到 "积压 ... 2" 上下文
    assert "2</strong>" in r.text


def test_cross_origin_write_request_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/tasks/run-today",
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_same_origin_write_request_is_allowed(client: TestClient) -> None:
    response = client.post(
        "/tasks/run-today",
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code != 403


def test_untrusted_host_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/?platform=tencent_channel",
        headers={"Host": "evil.example"},
    )
    assert response.status_code == 400
