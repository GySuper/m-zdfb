"""Typer CLI 入口(M0 骨架 + M1 accounts 子命令,后续 milestone 逐步实现其它命令)。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import typer
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account

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
    """扫码登录指定账号,刷新 Cookie(M2 实现)。"""
    _not_implemented(f"login {account_id}")


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
    """健康检查:账号 / Cookie / NAS / 飞书 API(M2-M4 实现)。"""
    _not_implemented("doctor")


@app.command("sync")
def sync() -> None:
    """立即拉一次飞书 Bitable,不跑任务(M3 实现)。"""
    _not_implemented("sync")


@app.command("run")
def run(
    daemon: bool = typer.Option(False, "--daemon", help="启动 daemon(09:00 cron + FastAPI)"),
    today: bool = typer.Option(False, "--today", help="立即跑今天所有 pending 任务"),
    task_id: int | None = typer.Option(None, "--task-id", help="跑指定单条任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="发布步骤跑到点'发布'前停下"),
) -> None:
    """执行任务(M5-M6 实现)。"""
    _not_implemented(
        f"run --daemon={daemon} --today={today} --task-id={task_id} --dry-run={dry_run}"
    )


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
