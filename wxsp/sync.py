"""按需调用的飞书 sync(M6):被 CLI 和 scheduler 共用。

把 M3 时落在 cli.py 里的"拉飞书 → 校验 → 写 DB → 回写"流程抽到这里,
打印交给调用方;sync_now 只负责返回 SyncResult。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from wxsp.config import Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.errors import NasUnreachable
from wxsp.feishu import FeishuApiError, fetch_pending_rows, make_client, writeback_row
from wxsp.models import Account, Task, Video
from wxsp.nas import find_cover, find_video
from wxsp.validator import FieldError, NasFinder, validate

# 计入 daily_limit 的 task 状态(已占用当天名额的:待跑/在跑/已发/中断;failed/skipped 不算)。
_DAILY_LIMIT_STATUSES = ("pending", "running", "success", "interrupted")


@dataclass
class SyncResult:
    pulled: int = 0
    accepted: int = 0
    updated: int = 0  # 已存在行被改正后覆盖重入库(重置为 pending)的行数
    rejected: int = 0
    skipped_existing: int = 0
    skipped_incomplete: int = 0  # 业务还没填完(4 核心字段任一空)→ 跳过 + 不回写
    rejected_details: list[tuple[str, list[FieldError]]] = field(default_factory=list)
    writeback_failed: int = 0  # 飞书回写失败的行数


class _NasFinderImpl:
    """生产用的 NAS 检索:search_root 由 validator 按账号传入,
    path_aliases 透传给底层用于 UNC ↔ POSIX 前缀互译(路径模式)。"""

    def find_video(
        self,
        filename: str,
        *,
        search_root: Path,
        path_aliases: dict[str, str] | None = None,
    ) -> Path:
        return find_video(filename, search_root=search_root, path_aliases=path_aliases)

    def find_cover(
        self,
        filename: str,
        *,
        search_root: Path,
        path_aliases: dict[str, str] | None = None,
    ) -> Path:
        return find_cover(filename, search_root=search_root, path_aliases=path_aliases)


def sync_now(
    settings: Settings, *, dry_run: bool = False, platform: str = "tencent_channel"
) -> SyncResult:
    """拉飞书一次,入库 + 回写。

    - 飞书 disabled → 直接返回空 SyncResult,不抛异常
    - 飞书 API 持续失败 → 抛 FeishuApiError(由调用方决定 exit code)
    - 单行回写失败 → 静默吞掉(写到 logger),不打断整体 sync
    """
    result = SyncResult()
    feishu_cfg = settings.feishu
    if feishu_cfg is None or not feishu_cfg.enabled:
        return result

    client = make_client(feishu_cfg.app_id, feishu_cfg.app_secret)
    rows = fetch_pending_rows(
        client,
        app_token=feishu_cfg.bitable.app_token,
        table_id=feishu_cfg.bitable.table_id,
        status_field=feishu_cfg.field_map.status,
    )
    result.pulled = len(rows)

    nas_finder: NasFinder = _NasFinderImpl()
    now = datetime.now()
    accepted: list[str] = []
    updated: list[str] = []
    rejected: list[tuple[str, list[FieldError]]] = []
    skipped: list[str] = []  # 孤儿 Video / 写库竞态 → 计 skipped_existing,不回写
    published_refused: list[str] = []  # 本地已发布 → 拒绝,回写"已发布"
    running_skipped: list[str] = []  # 本地正在跑 → 跳过,不回写
    skipped_incomplete: list[str] = []  # 4 核心字段空 → 跳过且不回写,等下次拉

    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        active_account_ids: set[str] = {
            a.id
            for a in session.exec(select(Account).where(Account.is_active == True))  # noqa: E712
        }
        # 给 validator 反查用:account_id → display_name。运营在飞书侧可能选了
        # display_name(平台同步过去的中文标签)或留旧的 account_id 都得能识别。
        active_accounts: dict[str, str] = {
            aid: settings.accounts[aid].display_name
            for aid in active_account_ids
            if aid in settings.accounts
        }
        for row in rows:
            existing_video = session.get(Video, row.record_id)
            try:
                v_result = validate(
                    row,
                    config=settings,
                    now=now,
                    nas_finder=nas_finder,
                    active_accounts=active_accounts,
                    platform=platform,
                )
            except NasUnreachable as exc:
                # NAS 掉线:本轮没法可靠校验文件,且 NAS 不会在循环内自愈 → 中止本轮 sync。
                # 已处理的行照常入库/回写;这行不回写"失败"(留待 NAS 恢复后下轮重试)。CLAUDE.md §5
                logger.warning(
                    f"[sync] NAS 不可达,中止本轮 sync"
                    f"(已处理 {len(accepted) + len(updated)} 行): {exc}"
                )
                break
            if v_result.incomplete:
                skipped_incomplete.append(row.record_id)
                continue
            if not v_result.ok:
                rejected.append((row.record_id, v_result.errors))
                continue

            if existing_video is None:
                # 全新行:先卡 daily_limit(超额拒绝入库 + 回写错误),再新建 Video + Task。
                aid = v_result.account_id or ""
                acc_cfg = settings.accounts.get(aid)
                if acc_cfg is not None:
                    used = len(
                        session.exec(
                            select(Task).where(
                                Task.account_id == aid,
                                Task.execute_date == v_result.execute_date,
                                Task.platform == platform,
                                col(Task.status).in_(_DAILY_LIMIT_STATUSES),
                            )
                        ).all()
                    )
                    if used >= acc_cfg.daily_limit:
                        rejected.append(
                            (
                                row.record_id,
                                [
                                    FieldError(
                                        field=feishu_cfg.field_map.account,
                                        message=(
                                            f"账号 {aid} 当天({v_result.execute_date})任务数"
                                            f"已达上限 {acc_cfg.daily_limit}"
                                        ),
                                    )
                                ],
                            )
                        )
                        continue
                if dry_run:
                    accepted.append(row.record_id)
                    continue
                video = Video(
                    id=row.record_id,
                    source="feishu",
                    file_path=str(v_result.video_path),
                    title=v_result.title or "",
                    description=v_result.description,
                    tags_json=json.dumps(v_result.tags, ensure_ascii=False),
                    cover_path=str(v_result.cover_path) if v_result.cover_path else None,
                    topic=v_result.topic,
                    original_claim=v_result.original_claim,
                    declaration=v_result.declaration,
                    ai_optimize=v_result.ai_optimize,
                    product_ids_json=json.dumps(v_result.product_ids, ensure_ascii=False),
                    ingested_at=now,
                )
                task = Task(
                    video_id=row.record_id,
                    account_id=v_result.account_id or "",
                    platform=platform,
                    execute_date=v_result.execute_date,
                    publish_at=v_result.publish_at,
                    status="pending",
                )
                try:
                    with session.begin_nested():
                        session.add(video)
                        session.add(task)
                except IntegrityError:
                    skipped.append(row.record_id)
                    continue
                accepted.append(row.record_id)
                continue

            # 已存在:按本地 Task 状态决定覆盖 / 拒绝 / 跳过
            existing_task = session.exec(select(Task).where(Task.video_id == row.record_id)).first()
            if existing_task is None:
                # Video 无对应 Task(异常半状态)→ 防呆跳过,不覆盖不回写
                skipped.append(row.record_id)
                continue
            if existing_task.status == "running":
                running_skipped.append(row.record_id)
                continue
            if existing_task.status == "success":
                published_refused.append(row.record_id)
                continue
            # pending / failed / skipped / interrupted → 覆盖
            if dry_run:
                updated.append(row.record_id)
                continue
            _apply_overwrite(session, existing_video, existing_task, v_result, platform, now)
            updated.append(row.record_id)

    result.accepted = len(accepted)
    result.updated = len(updated)
    result.rejected = len(rejected)
    result.skipped_existing = len(skipped) + len(published_refused) + len(running_skipped)
    result.skipped_incomplete = len(skipped_incomplete)
    result.rejected_details = rejected

    if not dry_run and feishu_cfg.sync.write_back_enabled:
        fm = feishu_cfg.field_map
        wb_failed = 0
        for record_id in accepted:
            if not _safe_writeback(client, feishu_cfg, record_id, {fm.status: "已计划"}):
                wb_failed += 1
        for record_id in updated:
            # 覆盖成功:状态回"已计划"并清空旧错误信息
            if not _safe_writeback(
                client, feishu_cfg, record_id, {fm.status: "已计划", fm.error_message: ""}
            ):
                wb_failed += 1
        for record_id, errs in rejected:
            if not _safe_writeback(
                client,
                feishu_cfg,
                record_id,
                {fm.status: "失败", fm.error_message: _format_errors(errs)},
            ):
                wb_failed += 1
        for record_id in published_refused:
            # 已发布:移出"待入库"过滤,避免每次同步重复拒绝刷屏
            if not _safe_writeback(
                client,
                feishu_cfg,
                record_id,
                {fm.status: "已发布", fm.error_message: _PUBLISHED_REFUSE_MSG},
            ):
                wb_failed += 1
        # running_skipped / skipped:不回写(留待入库,下轮自然收敛)
        result.writeback_failed = wb_failed

    return result


def _safe_writeback(client: Any, feishu_cfg: Any, record_id: str, fields: dict[str, Any]) -> bool:
    try:
        writeback_row(
            client,
            app_token=feishu_cfg.bitable.app_token,
            table_id=feishu_cfg.bitable.table_id,
            record_id=record_id,
            fields=fields,
        )
        return True
    except FeishuApiError as exc:
        logger.warning(f"[sync] 飞书回写失败 record={record_id} fields={fields}: {exc}")
        return False


def _format_errors(errs: list[FieldError]) -> str:
    bullet_lines = "\n".join(f"· {e.field}: {e.message}" for e in errs)
    return f'校验失败,请修复后将"状态"改回"待入库":\n{bullet_lines}'


_PUBLISHED_REFUSE_MSG = "该任务已发布成功,不能改这一行重发;如需重新发布,请在飞书新建一行任务。"


def _apply_overwrite(
    session: Any,
    video: Video,
    task: Task,
    v_result: Any,
    platform: str,
    now: datetime,
) -> None:
    """已存在且未发布的任务:原地刷新 Video 内容 + 把 Task 重置为干净 pending。"""
    video.file_path = str(v_result.video_path)
    video.title = v_result.title or ""
    video.description = v_result.description
    video.tags_json = json.dumps(v_result.tags, ensure_ascii=False)
    video.cover_path = str(v_result.cover_path) if v_result.cover_path else None
    video.topic = v_result.topic
    video.original_claim = v_result.original_claim
    video.declaration = v_result.declaration
    video.ai_optimize = v_result.ai_optimize
    video.product_ids_json = json.dumps(v_result.product_ids, ensure_ascii=False)
    video.ingested_at = now

    task.account_id = v_result.account_id or ""
    task.execute_date = v_result.execute_date
    task.publish_at = v_result.publish_at
    task.platform = platform
    task.status = "pending"
    task.attempts = 0
    task.lease_token = None
    task.lease_expires_at = None
    task.last_error_type = None
    task.last_error_msg = None
    task.started_at = None
    task.finished_at = None
    task.remote_video_id = None
    task.remote_url = None
    task.screenshots_json = "[]"

    session.add(video)
    session.add(task)
