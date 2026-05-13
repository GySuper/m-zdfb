"""09:00 cron + 手动 fire(无 polling)(M6)。"""

from __future__ import annotations

from datetime import date as _date

from sqlmodel import Session, col, select

from wxsp.models import TASK_STATUS_PENDING, Task


def queue_today(session: Session, *, today: _date | None = None) -> list[int]:
    """返回今天的 pending task id 列表,按 publish_at 升序。

    调用方负责开 session;本函数只读。
    """
    if today is None:
        today = _date.today()
    stmt = (
        select(Task)
        .where(Task.execute_date == today)
        .where(Task.status == TASK_STATUS_PENDING)
        .order_by(col(Task.publish_at).asc())
    )
    return [task.id for task in session.exec(stmt) if task.id is not None]
