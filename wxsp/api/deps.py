"""FastAPI 共享依赖:settings / DB engine / Jinja2 templates。

设计要点:
- Settings 模块级缓存(load_settings 已自带 lru_cache),路由直接调
- engine 用 wxsp.db.get_engine(),按需 init_db 一次
- Jinja2 templates 单例(渲染没有 IO 副作用)
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from wxsp import __version__ as wxsp_version
from wxsp.api.i18n import cookie_cn, enabled_cn, error_type_cn, level_cn, platform_cn, status_cn
from wxsp.config import Settings, load_settings
from wxsp.db import get_engine, init_db, session_scope

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# 中文化:模板里直接 {{ status_cn(s) }} 即可,无需每个变量做 if/elif 链
# 新版界面(Auto):平台图标的底色与特效,只属于展示层
PLATFORM_BRAND: dict[str, dict[str, str]] = {
    "tencent_channel": {
        "bg": "linear-gradient(135deg,#ffb11b,#fa7d00)",
        "bar": "#fa8c16",
        "fx": "",
    },
    "taobao_guanghe": {"bg": "linear-gradient(135deg,#ff8a00,#ff4400)", "bar": "#ff5000", "fx": ""},
    "douyin": {
        "bg": "#161823",
        "bar": "#fe2c55",
        "fx": "filter:drop-shadow(-1px -1px 0 #25f4ee) drop-shadow(1px 1px 0 #fe2c55)",
    },
    "kuaishou": {"bg": "linear-gradient(135deg,#ff7a1a,#ff3d00)", "bar": "#ff4906", "fx": ""},
    "xiaohongshu": {"bg": "#ff2442", "bar": "#ff2442", "fx": ""},
    "pinduoduo": {"bg": "linear-gradient(135deg,#f0473c,#d0211a)", "bar": "#e02e24", "fx": ""},
}


def platform_overview() -> dict[str, dict[str, int]]:
    """平台切换面板用:每个平台今日已发 / 今日总数 / 账号数 / 需扫码账号数。"""
    from datetime import date

    from sqlmodel import func, select

    from wxsp.models import Account, Task

    out: dict[str, dict[str, int]] = {}

    def slot(pf: str) -> dict[str, int]:
        return out.setdefault(pf, {"done": 0, "total": 0, "accounts": 0, "issues": 0})

    with session_scope(get_engine()) as s:
        rows = s.exec(
            select(Task.platform, Task.status, func.count())
            .where(Task.execute_date == date.today())
            .group_by(Task.platform, Task.status)
        ).all()
        for pf, st, n in rows:
            slot(pf)["total"] += n
            if st == "success":
                slot(pf)["done"] += n
        for pf, cs, n in s.exec(
            select(Account.platform, Account.cookie_status, func.count()).group_by(
                Account.platform, Account.cookie_status
            )
        ).all():
            slot(pf)["accounts"] += n
            if cs in ("expired", "warn"):
                slot(pf)["issues"] += n
    return out


def today_label() -> str:
    """顶栏日期:「11 月 18 日 · 星期三」(不用 %-m,Windows 的 strftime 不支持)。"""
    from datetime import date

    d = date.today()
    return f"{d.month} 月 {d.day} 日 · 星期{'一二三四五六日'[d.weekday()]}"


templates.env.globals.update(
    platform_brand=PLATFORM_BRAND,
    today_label=today_label,
    platform_overview=platform_overview,
    status_cn=status_cn,
    cookie_cn=cookie_cn,
    level_cn=level_cn,
    enabled_cn=enabled_cn,
    error_type_cn=error_type_cn,
    platform_cn=platform_cn,
    wxsp_version=wxsp_version,
)

_db_initialized = False


def get_settings(request: Request) -> Settings:
    platform = getattr(request.state, "current_platform", "tencent_channel") or "tencent_channel"
    return load_settings(platform=platform)


def get_session() -> Iterator[Session]:
    """请求级 SQLModel session;首请求懒初始化 DB schema。"""
    global _db_initialized
    engine = get_engine()
    if not _db_initialized:
        init_db(engine)
        _db_initialized = True
    with session_scope(engine) as session:
        yield session
