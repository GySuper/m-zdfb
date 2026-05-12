"""SQLModel 表定义:Account / Video / Task / Event(M1)。"""

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


class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    display_name: str
    user_data_dir: str
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
