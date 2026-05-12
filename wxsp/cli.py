"""Typer CLI 入口(M0 骨架,后续 milestone 逐步实现命令体)。"""

from __future__ import annotations

import typer

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


@app.command("login")
def login(account_id: str = typer.Argument(..., help="账号 ID")) -> None:
    """扫码登录指定账号,刷新 Cookie(M2 实现)。"""
    _not_implemented(f"login {account_id}")


@accounts_app.command("list")
def accounts_list() -> None:
    """列出所有账号及其 Cookie 状态(M1 实现)。"""
    _not_implemented("accounts list")


@accounts_app.command("pause")
def accounts_pause(
    account_id: str = typer.Argument(...),
    hours: int = typer.Option(24, "--hours", "-h", help="暂停小时数"),
) -> None:
    """暂停指定账号(M1 实现)。"""
    _not_implemented(f"accounts pause {account_id} --hours {hours}")


@accounts_app.command("resume")
def accounts_resume(account_id: str = typer.Argument(...)) -> None:
    """恢复指定账号(M1 实现)。"""
    _not_implemented(f"accounts resume {account_id}")


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
