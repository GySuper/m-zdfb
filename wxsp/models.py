"""SQLModel 表定义:Account / Video / Task / Event(M1)。

时间约定(M1 暂定,M6 接 APScheduler 时一并升级):
  所有 datetime 字段存 **naive local time (Asia/Shanghai)**。Asia/Shanghai 无 DST、
  等同固定偏移 UTC+8,naive 与 tz-aware 互转无风险。M6 加 APScheduler 时配
  `timezone="Asia/Shanghai"`,两边语义对齐即可。

路径约定:
  路径字段(`user_data_dir` / `file_path` / `cover_path`)为 `str`(SQLite 不直接存
  `pathlib.Path`)。**业务代码取用时请 `pathlib.Path(value)` 包一层**,符合项目
  "禁止字符串拼 `/`" 的跨平台约定。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

# Task.status 状态机
TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_SKIPPED = "skipped"
TASK_STATUS_INTERRUPTED = "interrupted"

# Account.cookie_status 状态机:从 wxsp login / wxsp doctor 写入
COOKIE_STATUS_OK = "ok"
COOKIE_STATUS_WARN = "warn"  # 预留:M7 接 cookie_warn_days 阈值后启用
COOKIE_STATUS_EXPIRED = "expired"
COOKIE_STATUS_UNKNOWN = "unknown"

# 平台标识
PLATFORM_TENCENT_CHANNEL = "tencent_channel"
PLATFORM_TAOBAO_GUANGHE = "taobao_guanghe"


class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: str
    platform: str = "tencent_channel"
    user_data_dir: str  # 存为 str(SQLite 限制);M2 browser.py 取用时 Path(value) 包一层
    daily_limit: int = 20
    is_active: bool = True
    paused_until: datetime | None = None
    cookie_status: str = "unknown"
    cookie_last_checked_at: datetime | None = None
    cookie_last_active_at: datetime | None = None


class Video(SQLModel, table=True):
    id: str = Field(primary_key=True)
    source: str = "feishu"
    file_path: str
    title: str
    description: str | None = None
    tags_json: str = "[]"
    cover_path: str | None = None
    topic: str | None = None
    original_claim: bool = False
    file_hash: str | None = None
    ingested_at: datetime


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    video_id: str = Field(foreign_key="video.id", index=True)
    account_id: str = Field(foreign_key="account.id", index=True)
    platform: str = Field(default="tencent_channel", index=True)
    execute_date: date = Field(index=True)
    publish_at: datetime
    status: str = Field(index=True)
    attempts: int = 0
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    last_error_type: str | None = None
    last_error_msg: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    remote_video_id: str | None = None
    remote_url: str | None = None
    screenshots_json: str = "[]"

    __table_args__ = (
        UniqueConstraint("video_id", name="uq_one_task_per_video"),
        Index("ix_status_execute_date", "status", "execute_date"),
    )


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ts: datetime
    level: str
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    account_id: str | None = Field(default=None, foreign_key="account.id", index=True)
    type: str
    message: str
    context_json: str = "{}"
