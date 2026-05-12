"""健康检查命令实现(M2)。

`record_cookie_check` 是写入 cookie 状态的唯一入口,被 `wxsp login` 和
`refresh_cookie_status` 共用。与 `db.transition_task` 一致:**不 commit**,
让调用方决定事务边界(login 成功后回写 + doctor 批量刷新都受益)。
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    Account,
)


def record_cookie_check(
    session: Session,
    account_id: str,
    *,
    is_logged_in: bool | None,
    now: datetime,
) -> None:
    """更新一个 Account 的 cookie 状态字段。**调用方负责 commit**。

    `is_logged_in`:
      - `True`  → status='ok',`cookie_last_active_at` 更新为 `now`
      - `False` → status='expired',`cookie_last_active_at` 不动
      - `None`  → status='unknown'(浏览器启动失败等异常路径),`cookie_last_active_at` 不动

    `cookie_last_checked_at` 任何情况都更新为 `now`。

    `account_id` 不存在 → `LookupError`。
    """
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"Account id={account_id!r} not found")

    if is_logged_in is True:
        account.cookie_status = COOKIE_STATUS_OK
        account.cookie_last_active_at = now
    elif is_logged_in is False:
        account.cookie_status = COOKIE_STATUS_EXPIRED
    else:
        account.cookie_status = COOKIE_STATUS_UNKNOWN

    account.cookie_last_checked_at = now
    session.add(account)
