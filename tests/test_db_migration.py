"""老库升级:init_db 给缺 platform 列的旧表补列(SQLite ALTER ADD COLUMN)。

多平台改造给 account/task/event 加了 platform 列,但 create_all 不会给已存在的
表加列。老库(多平台之前建的)缺 platform 列 → 所有按 platform 过滤的查询炸
'no such column: account.platform'。init_db 必须在 create_all 之后补这一列。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlmodel import create_engine

from wxsp.db import init_db


def _legacy_engine(tmp_path: Path):
    """造一个"多平台之前"的库:account/task/event 都没有 platform 列,各塞一行。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE account (id VARCHAR PRIMARY KEY, display_name VARCHAR, "
                "daily_limit INTEGER, is_active BOOLEAN)"
            )
        )
        conn.execute(text("INSERT INTO account (id, display_name) VALUES ('a1', '老号')"))
        conn.execute(
            text(
                "CREATE TABLE task (id INTEGER PRIMARY KEY, video_id VARCHAR, "
                "account_id VARCHAR, status VARCHAR)"
            )
        )
        conn.execute(
            text("INSERT INTO task (video_id, account_id, status) VALUES ('v1','a1','pending')")
        )
        conn.execute(
            text(
                "CREATE TABLE event (id INTEGER PRIMARY KEY, ts DATETIME, level VARCHAR, "
                "type VARCHAR, message VARCHAR)"
            )
        )
        conn.execute(text("INSERT INTO event (level, type, message) VALUES ('info','x','y')"))
    return engine


def test_init_db_adds_platform_to_legacy_tables(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path)
    init_db(engine)
    with engine.begin() as conn:
        for tbl in ("account", "task", "event"):
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({tbl})"))]
            assert "platform" in cols, f"{tbl} 仍缺 platform 列"
        # 旧行回填 tencent_channel(多平台之前的数据都是视频号)
        assert (
            conn.execute(text("SELECT platform FROM account WHERE id='a1'")).scalar()
            == "tencent_channel"
        )
        assert (
            conn.execute(text("SELECT platform FROM task WHERE video_id='v1'")).scalar()
            == "tencent_channel"
        )
        # 数据没丢
        assert conn.execute(text("SELECT count(*) FROM account")).scalar() == 1


def test_init_db_migration_idempotent(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path)
    init_db(engine)
    init_db(engine)  # 第二次不应报错(列已存在则跳过)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM account")).scalar() == 1


def test_init_db_fresh_db_has_platform(tmp_path: Path) -> None:
    """全新库(create_all 直接建表)本就带 platform 列,迁移是 no-op。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite'}")
    init_db(engine)
    with engine.begin() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(account)"))]
        assert "platform" in cols
