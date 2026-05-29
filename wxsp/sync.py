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

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from wxsp.config import Settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.feishu import FeishuApiError, fetch_pending_rows, make_client, writeback_row
from wxsp.models import Account, Task, Video
from wxsp.nas import find_cover, find_video
from wxsp.validator import FieldError, NasFinder, validate


@dataclass
class SyncResult:
    pulled: int = 0
    accepted: int = 0
    rejected: int = 0
    skipped_existing: int = 0
    skipped_incomplete: int = 0  # 业务还没填完(4 核心字段任一空)→ 跳过 + 不回写
    rejected_details: list[tuple[str, list[FieldError]]] = field(default_factory=list)


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
    rejected: list[tuple[str, list[FieldError]]] = []
    skipped: list[str] = []
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
            if session.get(Video, row.record_id) is not None:
                skipped.append(row.record_id)
                continue
            v_result = validate(
                row,
                config=settings,
                now=now,
                nas_finder=nas_finder,
                active_accounts=active_accounts,
            )
            if v_result.incomplete:
                skipped_incomplete.append(row.record_id)
                continue
            if not v_result.ok:
                rejected.append((row.record_id, v_result.errors))
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

    result.accepted = len(accepted)
    result.rejected = len(rejected)
    result.skipped_existing = len(skipped)
    result.skipped_incomplete = len(skipped_incomplete)
    result.rejected_details = rejected

    if not dry_run and feishu_cfg.sync.write_back_enabled:
        fm = feishu_cfg.field_map
        for record_id in accepted:
            _safe_writeback(client, feishu_cfg, record_id, {fm.status: "已计划"})
        for record_id, errs in rejected:
            _safe_writeback(
                client,
                feishu_cfg,
                record_id,
                {fm.status: "失败", fm.error_message: _format_errors(errs)},
            )
        for record_id in skipped:
            _safe_writeback(
                client,
                feishu_cfg,
                record_id,
                {fm.error_message: "已有历史任务,请在 Web UI 重试"},
            )

    return result


def _safe_writeback(client: Any, feishu_cfg: Any, record_id: str, fields: dict[str, Any]) -> None:
    try:
        writeback_row(
            client,
            app_token=feishu_cfg.bitable.app_token,
            table_id=feishu_cfg.bitable.table_id,
            record_id=record_id,
            fields=fields,
        )
    except FeishuApiError:
        pass  # 单行回写失败不打断 sync;由调用方决定要不要 echo


def _format_errors(errs: list[FieldError]) -> str:
    bullet_lines = "\n".join(f"· {e.field}: {e.message}" for e in errs)
    return f'校验失败,请修复后将"状态"改回"待入库":\n{bullet_lines}'
