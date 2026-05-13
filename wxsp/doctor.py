"""健康检查命令实现(M2 cookie,M4 加 NAS)。

`record_cookie_check` 是写入 cookie 状态的唯一入口,被 `wxsp login` 和
`refresh_cookie_status` 共用。与 `db.transition_task` 一致:**不 commit**,
让调用方决定事务边界(login 成功后回写 + doctor 批量刷新都受益)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from sqlmodel import Session, select

from wxsp.config import Settings
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


CookieChecker = Callable[[Path], bool]


class CookieStatusRow(NamedTuple):
    """`refresh_cookie_status` 返回行,CLI 输出层用。"""

    account_id: str
    status: str
    last_active_at: datetime | None


def refresh_cookie_status(
    session: Session,
    *,
    cookie_checker: CookieChecker,
    now_fn: Callable[[], datetime] = datetime.now,
) -> list[CookieStatusRow]:
    """对所有账号跑一次 cookie 检查,回写状态。**调用方负责 commit**。

    `cookie_checker` 是注入点:传一个 `Path -> bool` 的回调。生产代码传
    `wxsp.browser.check_cookie`(真打开浏览器);测试传一个 stub。

    `cookie_checker` 抛异常 → 该账号被标 `unknown`,不影响其它账号继续检查。

    返回每账号一行 `CookieStatusRow`,顺序与 `Account.id` 字典序一致。
    """
    accounts = session.exec(select(Account).order_by(Account.id)).all()
    rows: list[CookieStatusRow] = []
    for account in accounts:
        now = now_fn()
        try:
            is_logged_in: bool | None = cookie_checker(Path(account.user_data_dir))
        except Exception:
            is_logged_in = None
        record_cookie_check(session, account.id, is_logged_in=is_logged_in, now=now)
        rows.append(
            CookieStatusRow(
                account_id=account.id,
                status=account.cookie_status,
                last_active_at=account.cookie_last_active_at,
            )
        )
    return rows


# ============== NAS 健康检查(M4)==============


class NasCheckRow(NamedTuple):
    """`check_nas` 返回行,CLI 输出层用。"""

    path: Path
    label: str  # "video_search_root" | "cover_search_root"
    ok: bool
    detail: str


def check_nas(config: Settings) -> list[NasCheckRow]:
    """检查 video_search_root + cover_search_root 是否存在且为目录。

    无 IO 副作用,只读 stat。返回固定 2 行,按 video → cover 顺序。
    任何路径都不抛异常,失败信息塞 NasCheckRow.detail。
    """
    targets: list[tuple[str, Path]] = [
        ("video_search_root", config.paths.video_search_root),
        ("cover_search_root", config.paths.cover_search_root),
    ]
    rows: list[NasCheckRow] = []
    for label, path in targets:
        if path.is_dir():
            rows.append(NasCheckRow(path=path, label=label, ok=True, detail=f"OK ({path})"))
        elif path.exists():
            rows.append(NasCheckRow(path=path, label=label, ok=False, detail=f"不是目录: {path}"))
        else:
            rows.append(NasCheckRow(path=path, label=label, ok=False, detail=f"不存在: {path}"))
    return rows
