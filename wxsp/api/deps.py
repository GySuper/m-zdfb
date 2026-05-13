"""FastAPI 共享依赖:settings / DB engine / Jinja2 templates。

设计要点:
- Settings 模块级缓存(load_settings 已自带 lru_cache),路由直接调
- engine 用 wxsp.db.get_engine(),按需 init_db 一次
- Jinja2 templates 单例(渲染没有 IO 副作用)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from wxsp.config import Settings, load_settings
from wxsp.db import get_engine, init_db, session_scope

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_db_initialized = False


def get_settings() -> Settings:
    return load_settings()


def get_session() -> Iterator[Session]:
    """请求级 SQLModel session;首请求懒初始化 DB schema。"""
    global _db_initialized
    engine = get_engine()
    if not _db_initialized:
        init_db(engine)
        _db_initialized = True
    with session_scope(engine) as session:
        yield session
