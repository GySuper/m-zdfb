"""健康检查命令实现(M2 cookie,M4 加 NAS,M10 收尾加 warn 阈值 + 飞书 ping)。

`record_cookie_check` 是写入 cookie 状态的唯一入口,被 `wxsp login` 和
`refresh_cookie_status` 共用。与 `db.transition_task` 一致:**不 commit**,
让调用方决定事务边界(login 成功后回写 + doctor 批量刷新都受益)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from sqlmodel import Session, select

from wxsp.config import Settings
from wxsp.models import (
    COOKIE_STATUS_EXPIRED,
    COOKIE_STATUS_OK,
    COOKIE_STATUS_UNKNOWN,
    COOKIE_STATUS_WARN,
    Account,
)


def record_cookie_check(
    session: Session,
    account_id: str,
    *,
    is_logged_in: bool | None,
    now: datetime,
    warn_threshold: timedelta | None = None,
) -> None:
    """更新一个 Account 的 cookie 状态字段。**调用方负责 commit**。

    `is_logged_in`:
      - `True`  → 默认 status='ok',`cookie_last_active_at` 更新为 `now`;
        若 `warn_threshold is not None` 且距上次成功 active 已超过该阈值
        → status='warn'(仍能登录但 idle 太久,提醒人工注意)
      - `False` → status='expired',`cookie_last_active_at` 不动
      - `None`  → status='unknown'(浏览器启动失败等异常路径),`cookie_last_active_at` 不动

    `cookie_last_checked_at` 任何情况都更新为 `now`。

    `warn_threshold=None`:跳过 warn 判定(login 命令场景,刚扫完码当然是 ok)。

    `account_id` 不存在 → `LookupError`。
    """
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"Account id={account_id!r} not found")

    if is_logged_in is True:
        prior_active = account.cookie_last_active_at
        if (
            warn_threshold is not None
            and prior_active is not None
            and (now - prior_active) > warn_threshold
        ):
            account.cookie_status = COOKIE_STATUS_WARN
        else:
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
    warn_threshold: timedelta | None = None,
) -> list[CookieStatusRow]:
    """对所有账号跑一次 cookie 检查,回写状态。**调用方负责 commit**。

    `cookie_checker` 是注入点:传一个 `Path -> bool` 的回调。生产代码传
    `wxsp.browser.check_cookie`(真打开浏览器);测试传一个 stub。

    `cookie_checker` 抛异常 → 该账号被标 `unknown`,不影响其它账号继续检查。

    `warn_threshold` 转发给 `record_cookie_check`,触发"idle 太久"warn 语义。

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
        record_cookie_check(
            session,
            account.id,
            is_logged_in=is_logged_in,
            now=now,
            warn_threshold=warn_threshold,
        )
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
    label: str  # "<account_id>.video_search_root" | "<account_id>.cover_search_root"
    ok: bool
    detail: str


# ============== 飞书 API 健康检查 ==============


class FeishuCheckRow(NamedTuple):
    """`check_feishu` 返回行,CLI 输出层用。"""

    label: str
    ok: bool
    detail: str


FeishuProber = Callable[[Settings], None]


def _default_feishu_prober(config: Settings) -> None:
    """生产实现:拉 1 条 record 验证 app_id/secret/app_token/table_id 全对。

    任何异常(网络 / 401 / 404 / app_token 错)向上冒泡给 `check_feishu` 翻译。
    """
    import lark_oapi as lark  # type: ignore[import-untyped]
    from lark_oapi.api.bitable.v1 import (  # type: ignore[import-untyped]
        SearchAppTableRecordRequest,
        SearchAppTableRecordRequestBody,
    )

    from wxsp.feishu import FeishuApiError

    client = (
        lark.Client.builder()
        .app_id(config.feishu.app_id)
        .app_secret(config.feishu.app_secret)
        .build()
    )
    req = (
        SearchAppTableRecordRequest.builder()
        .app_token(config.feishu.bitable.app_token)
        .table_id(config.feishu.bitable.table_id)
        .page_size(1)
        .request_body(SearchAppTableRecordRequestBody.builder().build())
        .build()
    )
    response = client.bitable.v1.app_table_record.search(req)
    if not response.success():
        raise FeishuApiError(f"飞书 API 错误 code={response.code} msg={response.msg}")


def check_feishu(config: Settings, *, prober: FeishuProber | None = None) -> FeishuCheckRow:
    """飞书 API 探测:验证 app_id/secret/app_token/table_id 任一是否配错。

    `prober` 默认走真飞书(拉 1 条 record);测试传 stub 避免网络。
    `feishu.enabled=False` 时直接返回"已跳过"且 ok=True,不走 prober。
    """
    if not config.feishu.enabled:
        return FeishuCheckRow(label="feishu.api", ok=True, detail="飞书未启用,跳过")
    if prober is None:
        prober = _default_feishu_prober
    try:
        prober(config)
    except Exception as exc:
        return FeishuCheckRow(label="feishu.api", ok=False, detail=f"飞书 API 探测失败: {exc}")
    return FeishuCheckRow(
        label="feishu.api",
        ok=True,
        detail=(
            f"OK (app_token={config.feishu.bitable.app_token} "
            f"table_id={config.feishu.bitable.table_id})"
        ),
    )


def check_nas(config: Settings) -> list[NasCheckRow]:
    """按账号检查 video_search_root + cover_search_root。

    无 IO 副作用,只读 stat。每个账号 2 行(video + cover),按账号 ID 排序。
    任何路径都不抛异常,失败信息塞 NasCheckRow.detail。
    """
    rows: list[NasCheckRow] = []
    for aid in sorted(config.accounts.keys()):
        ac = config.accounts[aid]
        for kind, path in (
            ("video_search_root", ac.video_search_root),
            ("cover_search_root", ac.cover_search_root),
        ):
            label = f"{aid}.{kind}"
            if path.is_dir():
                rows.append(NasCheckRow(path=path, label=label, ok=True, detail=f"OK ({path})"))
            elif path.exists():
                rows.append(
                    NasCheckRow(path=path, label=label, ok=False, detail=f"不是目录: {path}")
                )
            else:
                rows.append(NasCheckRow(path=path, label=label, ok=False, detail=f"不存在: {path}"))
    return rows
