"""Typer CLI 入口(M0 骨架 + M1 accounts 子命令,后续 milestone 逐步实现其它命令)。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from wxsp.browser import check_cookie
from wxsp.config import Settings, load_settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.doctor import check_nas, record_cookie_check, refresh_cookie_status
from wxsp.feishu import FeishuApiError, fetch_pending_rows, make_client, writeback_row
from wxsp.models import Account, Task, Video
from wxsp.nas import find_cover, find_video
from wxsp.publisher import AlreadyClaimed, publish
from wxsp.validator import FieldError, NasFinder, validate

app = typer.Typer(
    name="wxsp",
    help="微信视频号自动发布工具",
    no_args_is_help=True,
    add_completion=False,
)

accounts_app = typer.Typer(help="账号管理", no_args_is_help=True)
app.add_typer(accounts_app, name="accounts")


def _not_implemented(name: str) -> None:
    typer.echo(f"[wxsp] 命令 `{name}` 还未实现(M0 骨架阶段)。")


@contextmanager
def _open_session() -> Iterator[Session]:
    """CLI 共用:取 engine → 建表 → 开 session(成功 commit,异常 rollback)。"""
    engine = get_engine()
    init_db(engine)
    with session_scope(engine) as session:
        yield session


@app.command("login")
def login(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    """扫码登录指定账号,刷新 Cookie。打开浏览器后扫描页面上的二维码即可。"""
    # 1. 拿 user_data_dir,session 立刻关闭(浏览器扫码可能开 5 分钟,不能持 session)
    with _open_session() as session:
        account = session.get(Account, account_id)
        if account is None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 不存在。先 `wxsp accounts add`。")
            raise typer.Exit(code=1)
        user_data_dir = Path(account.user_data_dir)

    # 2. 启浏览器,等扫码 / 等已登录标记可见(最长 5 分钟)
    typer.echo(f"[wxsp] 打开浏览器,请在弹出窗口中扫码登录 {account_id}(最长 5 分钟)...")
    try:
        is_logged_in: bool | None = check_cookie(user_data_dir, timeout_ms=300_000)
    except Exception as exc:
        typer.echo(f"[wxsp] 浏览器启动失败:{exc}")
        is_logged_in = None

    # 3. 回写 DB
    now = datetime.now()
    with _open_session() as session:
        record_cookie_check(session, account_id, is_logged_in=is_logged_in, now=now)

    if is_logged_in is True:
        typer.echo(f"[wxsp] ✓ 账号 {account_id} 登录成功,cookie 已持久化。")
    elif is_logged_in is False:
        typer.echo("[wxsp] ✗ 登录超时:未在 5 分钟内完成扫码,cookie 标记为 expired。")
        raise typer.Exit(code=1)
    else:
        typer.echo("[wxsp] ✗ 浏览器异常,cookie 状态标记为 unknown。")
        raise typer.Exit(code=1)


@accounts_app.command("add")
def accounts_add(
    account_id: str = typer.Argument(..., help="账号 ID,如 account_a"),
    display_name: str = typer.Option(..., "--display-name", help="账号显示名,如 美食号"),
    user_data_dir: str = typer.Option(
        ..., "--user-data-dir", help="Chrome profile 目录,每账号独立"
    ),
    daily_limit: int = typer.Option(20, "--daily-limit", help="每日发布上限"),
) -> None:
    """新增账号到 DB。"""
    with _open_session() as session:
        existing = session.get(Account, account_id)
        if existing is not None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 已存在。")
            raise typer.Exit(code=1)
        session.add(
            Account(
                id=account_id,
                display_name=display_name,
                user_data_dir=user_data_dir,
                daily_limit=daily_limit,
            )
        )
        try:
            session.flush()
        except IntegrityError as exc:
            typer.echo(f"[wxsp] 写入账号 {account_id!r} 失败:{exc}")
            raise typer.Exit(code=1) from exc
    typer.echo(f"[wxsp] 已新增账号 {account_id} ({display_name})。")


@accounts_app.command("list")
def accounts_list() -> None:
    """列出所有账号及其 Cookie 状态。"""
    with _open_session() as session:
        rows = session.exec(select(Account).order_by(Account.id)).all()
        if not rows:
            typer.echo("[wxsp] 无账号。")
            return
        typer.echo(f"{'ID':<14} {'显示名':<12} {'状态':<10} {'Cookie':<10} {'暂停至':<20}")
        for row in rows:
            active = "active" if row.is_active else "inactive"
            paused = row.paused_until.strftime("%Y-%m-%d %H:%M") if row.paused_until else "-"
            typer.echo(
                f"{row.id:<14} {row.display_name:<12} {active:<10} "
                f"{row.cookie_status:<10} {paused:<20}"
            )


@accounts_app.command("pause")
def accounts_pause(
    account_id: str = typer.Argument(..., help="账号 ID"),
    hours: int = typer.Option(24, "--hours", "-h", help="暂停小时数"),
) -> None:
    """暂停指定账号 N 小时。"""
    with _open_session() as session:
        row = session.get(Account, account_id)
        if row is None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 不存在。")
            raise typer.Exit(code=1)
        row.paused_until = datetime.now() + timedelta(hours=hours)
        session.add(row)
    typer.echo(f"[wxsp] 已暂停账号 {account_id} {hours} 小时。")


@accounts_app.command("resume")
def accounts_resume(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    """恢复指定账号(清空 paused_until)。"""
    with _open_session() as session:
        row = session.get(Account, account_id)
        if row is None:
            typer.echo(f"[wxsp] 账号 {account_id!r} 不存在。")
            raise typer.Exit(code=1)
        row.paused_until = None
        session.add(row)
    typer.echo(f"[wxsp] 已恢复账号 {account_id}。")


@app.command("doctor")
def doctor() -> None:
    """健康检查:账号 / Cookie + NAS(M2 cookie,M4 NAS)。"""

    # cookie_checker 注入点:生产用 wxsp.browser.check_cookie(打开浏览器);测试可 monkeypatch
    def cookie_checker(user_data_dir: Path) -> bool:
        return check_cookie(user_data_dir, timeout_ms=15_000)

    cookie_failed = False

    with _open_session() as session:
        # 先看有没有账号 —— 没有就提示,但不 return,继续跑 NAS section
        if not session.exec(select(Account)).first():
            typer.echo("[wxsp] 无账号。先 `wxsp accounts add`,再 `wxsp login <id>` 扫码。")
        else:
            rows = refresh_cookie_status(session, cookie_checker=cookie_checker)
            typer.echo(f"{'ID':<14} {'Cookie':<10} {'最后活跃':<20}")
            for row in rows:
                last_active = (
                    row.last_active_at.strftime("%Y-%m-%d %H:%M") if row.last_active_at else "-"
                )
                typer.echo(f"{row.account_id:<14} {row.status:<10} {last_active:<20}")
                if row.status != "ok":
                    cookie_failed = True

    # NAS section
    typer.echo("")  # 空行分隔
    typer.echo("NAS:")
    settings = load_settings()
    nas_rows = check_nas(settings)
    nas_failed = False
    for nas_row in nas_rows:
        mark = "✅" if nas_row.ok else "❌"
        typer.echo(f"  {mark} {nas_row.label:<20} {nas_row.detail}")
        if not nas_row.ok:
            nas_failed = True

    if cookie_failed or nas_failed:
        raise typer.Exit(code=1)


class _NasFinderImpl:
    """生产 NasFinder:接 config 的 search root。"""

    def __init__(self, video_root: Path, cover_root: Path) -> None:
        self._video_root = video_root
        self._cover_root = cover_root

    def find_video(self, filename: str) -> Path:
        return find_video(filename, search_root=self._video_root)

    def find_cover(self, filename: str) -> Path:
        return find_cover(filename, search_root=self._cover_root)


@app.command("sync")
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="走完流程但不写 DB 不回写飞书"),
) -> None:
    """立即拉一次飞书 Bitable,执行入库 / 错误回写。"""
    settings = load_settings()
    if not settings.feishu.enabled:
        typer.echo("[wxsp] 飞书未启用,跳过 sync。")
        return

    typer.echo(
        f"[wxsp] 飞书同步开始: app_token={settings.feishu.bitable.app_token} "
        f"table_id={settings.feishu.bitable.table_id}"
    )

    client = make_client(settings.feishu.app_id, settings.feishu.app_secret)
    try:
        rows = fetch_pending_rows(
            client,
            app_token=settings.feishu.bitable.app_token,
            table_id=settings.feishu.bitable.table_id,
            status_field=settings.feishu.field_map.status,
        )
    except FeishuApiError as exc:
        typer.echo(f"[wxsp] 飞书 API 持续失败: {exc}")
        raise typer.Exit(code=70) from exc

    typer.echo(f"[wxsp] 拉取待入库行: {len(rows)} 条")

    nas_finder: NasFinder = _NasFinderImpl(
        video_root=settings.paths.video_search_root,
        cover_root=settings.paths.cover_search_root,
    )
    now = datetime.now()
    accepted: list[tuple[str, int]] = []
    rejected: list[tuple[str, list[FieldError]]] = []
    skipped_existing: list[str] = []

    with _open_session() as session:
        active_account_ids: set[str] = {
            a.id
            for a in session.exec(select(Account).where(Account.is_active == True))  # noqa: E712
        }
        for row in rows:
            if session.get(Video, row.record_id) is not None:
                skipped_existing.append(row.record_id)
                continue
            result = validate(
                row,
                config=settings,
                now=now,
                nas_finder=nas_finder,
                active_account_ids=active_account_ids,
            )
            if not result.ok:
                rejected.append((row.record_id, result.errors))
                continue
            if dry_run:
                accepted.append((row.record_id, -1))
                continue
            video = Video(
                id=row.record_id,
                source="feishu",
                file_path=str(result.video_path),
                title=result.title or "",
                description=result.description,
                tags_json=json.dumps(result.tags, ensure_ascii=False),
                cover_path=str(result.cover_path) if result.cover_path else None,
                topic=result.topic,
                original_claim=result.original_claim,
                ingested_at=now,
            )
            task = Task(
                video_id=row.record_id,
                account_id=result.account_id or "",
                execute_date=result.execute_date,
                publish_at=result.publish_at,
                status="pending",
            )
            try:
                with session.begin_nested():
                    session.add(video)
                    session.add(task)
            except IntegrityError:
                skipped_existing.append(row.record_id)
                continue
            accepted.append((row.record_id, task.id or -1))

    # 回写飞书(--dry-run 跳过;write_back_enabled=False 也跳过)
    if not dry_run and settings.feishu.sync.write_back_enabled:
        fm = settings.feishu.field_map
        for record_id, _task_id in accepted:
            _safe_writeback(client, settings, record_id, {fm.status: "已计划"})
        for record_id, errs in rejected:
            _safe_writeback(
                client,
                settings,
                record_id,
                {fm.status: "失败", fm.error_message: _format_errors(errs)},
            )
        for record_id in skipped_existing:
            _safe_writeback(
                client,
                settings,
                record_id,
                {fm.error_message: "已有历史任务,请在 Web UI 重试"},
            )

    typer.echo("[wxsp] 飞书同步完成")
    typer.echo(f"  拉取: {len(rows)}")
    typer.echo(f"  入库: {len(accepted)}{' (dry-run)' if dry_run else ''}")
    typer.echo(f"  拒绝: {len(rejected)}{' (已回写)' if not dry_run else ''}")
    typer.echo(f"  已存在跳过: {len(skipped_existing)}")


def _safe_writeback(
    client: Any, settings: Settings, record_id: str, fields: dict[str, Any]
) -> None:
    """writeback 单行失败不抛,打印告警继续。"""
    try:
        writeback_row(
            client,
            app_token=settings.feishu.bitable.app_token,
            table_id=settings.feishu.bitable.table_id,
            record_id=record_id,
            fields=fields,
        )
    except FeishuApiError as exc:
        typer.echo(f"[wxsp] 回写 {record_id} 失败(已跳过): {exc}")


def _format_errors(errs: list[FieldError]) -> str:
    bullet_lines = "\n".join(f"· {e.field}: {e.message}" for e in errs)
    return f'校验失败,请修复后将"状态"改回"待入库":\n{bullet_lines}'


@app.command("run")
def run(
    daemon: bool = typer.Option(False, "--daemon", help="启动 daemon(09:00 cron + FastAPI)"),
    today: bool = typer.Option(False, "--today", help="立即跑今天所有 pending 任务"),
    task_id: int | None = typer.Option(None, "--task-id", help="跑指定单条任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="发布步骤跑到点'发布'前停下"),
) -> None:
    """执行任务(M5: --task-id;M6 实现 --daemon/--today)。"""
    if task_id is None:
        _not_implemented(
            f"run --daemon={daemon} --today={today} --task-id={task_id} --dry-run={dry_run}"
        )
        return

    settings = load_settings()
    typer.echo(f"[wxsp] 跑 task {task_id}{' (dry-run)' if dry_run else ''}...")
    try:
        result = publish(task_id, dry_run=dry_run, settings=settings)
    except AlreadyClaimed as exc:
        typer.echo(f"[wxsp] ✗ {exc}")
        raise typer.Exit(code=1) from exc

    if result.ok:
        typer.echo(f"[wxsp] ✓ task {task_id} {'dry-run 完成' if dry_run else '发布成功'}")
        if result.remote_url:
            typer.echo(f"        remote_url: {result.remote_url}")
        if result.screenshots:
            typer.echo(f"        screenshots: {', '.join(result.screenshots)}")
    else:
        typer.echo(f"[wxsp] ✗ task {task_id} 失败: {result.error_type}")
        typer.echo(f"        {result.error_msg}")
        raise typer.Exit(code=1)


@app.command("status")
def status(
    date: str | None = typer.Option(None, "--date", help="日期 YYYY-MM-DD,默认今天"),
) -> None:
    """查看任务状态汇总(M1 实现)。"""
    _not_implemented(f"status --date {date}")


@app.command("logs")
def logs(
    task_id: int | None = typer.Option(None, "--task-id", help="按 task 过滤"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续 tail"),
) -> None:
    """查看日志(M7 实现)。"""
    _not_implemented(f"logs --task-id {task_id} --follow {follow}")


@app.command("web")
def web(port: int = typer.Option(8765, "--port", "-p", help="Web UI 端口")) -> None:
    """启动 Web UI(M8 实现)。"""
    _not_implemented(f"web --port {port}")


if __name__ == "__main__":
    app()
