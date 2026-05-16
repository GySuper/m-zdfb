"""Typer CLI 入口(M0 骨架 + M1 accounts 子命令,后续 milestone 逐步实现其它命令)。"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import typer
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from wxsp.archive import cleanup_old_files, install_file_sink
from wxsp.browser import check_cookie
from wxsp.config import load_settings
from wxsp.db import get_engine, init_db, session_scope
from wxsp.doctor import check_feishu, check_nas, record_cookie_check, refresh_cookie_status
from wxsp.feishu import FeishuApiError
from wxsp.models import Account
from wxsp.notify import NotifyEvent, notify
from wxsp.publisher import AlreadyClaimed, publish
from wxsp.scheduler import run_today_pending, start_daemon
from wxsp.sync import sync_now


def _force_utf8_stdout() -> None:
    """让 stdout/stderr 强制走 UTF-8 + errors='replace'。

    Windows 中文版 Python 默认 stdout 是 cp936/gbk,遇到 emoji(✅ ❌ ✓ ✗)
    会抛 UnicodeEncodeError 把命令搞挂。mac/Linux 默认就是 UTF-8,reconfigure
    是无操作。CliRunner 把 stdout 换成 StringIO 没有 reconfigure 方法 →
    AttributeError 被 try 兜住,不影响测试。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 流被替换 / 已关闭 / 不支持 → 跳过,绝不让 cli 启动失败


_force_utf8_stdout()


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
    """健康检查:账号 / Cookie + NAS + 飞书 API。"""

    # cookie_checker 注入点:生产用 wxsp.browser.check_cookie(打开浏览器);测试可 monkeypatch
    def cookie_checker(user_data_dir: Path) -> bool:
        return check_cookie(user_data_dir, timeout_ms=15_000)

    settings = load_settings()
    warn_threshold = timedelta(days=settings.monitoring.cookie_warn_days)
    cookie_failed = False

    with _open_session() as session:
        # 先看有没有账号 —— 没有就提示,但不 return,继续跑 NAS section
        if not session.exec(select(Account)).first():
            typer.echo("[wxsp] 无账号。先 `wxsp accounts add`,再 `wxsp login <id>` 扫码。")
        else:
            rows = refresh_cookie_status(
                session,
                cookie_checker=cookie_checker,
                warn_threshold=warn_threshold,
            )
            typer.echo(f"{'ID':<14} {'Cookie':<10} {'最后活跃':<20}")
            for row in rows:
                last_active = (
                    row.last_active_at.strftime("%Y-%m-%d %H:%M") if row.last_active_at else "-"
                )
                typer.echo(f"{row.account_id:<14} {row.status:<10} {last_active:<20}")
                if row.status == "warn":
                    # 推 cookie_warning 告警(notify_on 里启用时才真发到企微)
                    notify(
                        NotifyEvent(
                            type="cookie_warning",
                            level="warn",
                            title=f"Cookie 即将过期: {row.account_id}",
                            content=(
                                f"账号 {row.account_id} 距上次成功活跃已超过 "
                                f"{settings.monitoring.cookie_warn_days} 天,"
                                f"cookie 可能不稳定,建议主动 `wxsp login {row.account_id}` 续命。"
                            ),
                            account_id=row.account_id,
                        ),
                        session=session,
                        settings=settings,
                    )
                elif row.status != "ok":
                    cookie_failed = True

    # NAS section
    typer.echo("")
    typer.echo("NAS:")
    nas_rows = check_nas(settings)
    nas_failed = False
    for nas_row in nas_rows:
        mark = "✅" if nas_row.ok else "❌"
        typer.echo(f"  {mark} {nas_row.label:<20} {nas_row.detail}")
        if not nas_row.ok:
            nas_failed = True

    # 飞书 API section
    typer.echo("")
    typer.echo("飞书:")
    feishu_failed = False
    feishu_row = check_feishu(settings)
    mark = "✅" if feishu_row.ok else "❌"
    typer.echo(f"  {mark} {feishu_row.label:<20} {feishu_row.detail}")
    if not feishu_row.ok:
        feishu_failed = True

    # APC section(只在打包模式跑,dev 模式 apc_config.py 是占位符,跑也只会报错)
    typer.echo("")
    typer.echo("APC:")
    apc_failed = False
    from wxsp.config import is_packaged

    if not is_packaged():
        typer.echo("  ⏭  dev 模式,跳过(打包后才走 APC)")
    else:
        from wxsp.apc_config import APC_APP_ID, APC_ENDPOINT

        typer.echo(f"  endpoint={APC_ENDPOINT[:40]}{'...' if len(APC_ENDPOINT) > 40 else ''}")
        typer.echo(f"  app_id={APC_APP_ID[:12]}{'...' if len(APC_APP_ID) > 12 else ''}")
        try:
            import wxsp.apc as wxsp_apc

            client = wxsp_apc._client()  # 构造时校验 ApcConfig
            from apc_sdk._http import fetch_session

            cache = client._cache.load_or_bootstrap()
            license_jwt = fetch_session(
                client._get_http(), client._cfg, device_id=cache.get("device_id")
            )
            typer.echo(f"  ✅ APC check OK(license 长度={len(license_jwt)})")
            typer.echo(f"  session.json: device_id={cache.get('device_id')!r}")
        except Exception as exc:
            apc_failed = True
            typer.echo(f"  ❌ APC check 失败: {type(exc).__name__}: {exc}")
            typer.echo(f"     完整 repr: {exc!r}")

    if cookie_failed or nas_failed or feishu_failed or apc_failed:
        raise typer.Exit(code=1)


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
    try:
        result = sync_now(settings, dry_run=dry_run)
    except FeishuApiError as exc:
        typer.echo(f"[wxsp] 飞书 API 持续失败: {exc}")
        raise typer.Exit(code=70) from exc

    typer.echo("[wxsp] 飞书同步完成")
    typer.echo(f"  拉取: {result.pulled}")
    typer.echo(f"  入库: {result.accepted}{' (dry-run)' if dry_run else ''}")
    typer.echo(f"  拒绝: {result.rejected}{' (已回写)' if not dry_run else ''}")
    typer.echo(f"  已存在跳过: {result.skipped_existing}")


@app.command("run")
def run(
    daemon: bool = typer.Option(False, "--daemon", help="启动 daemon(09:00 cron)"),
    today: bool = typer.Option(False, "--today", help="立即跑今天所有 pending 任务"),
    task_id: int | None = typer.Option(None, "--task-id", help="跑指定单条任务"),
    dry_run: bool = typer.Option(False, "--dry-run", help="发布步骤跑到点'发布'前停下"),
) -> None:
    """执行任务。三选一:--task-id 单条 / --today 跑今天 / --daemon 起 cron。"""
    settings = load_settings()

    if task_id is not None:
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
            return
        typer.echo(f"[wxsp] ✗ task {task_id} 失败: {result.error_type}")
        typer.echo(f"        {result.error_msg}")
        raise typer.Exit(code=1)

    if today:
        typer.echo("[wxsp] 跑今天所有 pending 任务...")
        summary = run_today_pending(settings)
        typer.echo(
            f"[wxsp] 完成: attempted={summary.attempted} succeeded={summary.succeeded} "
            f"failed={summary.failed} skipped_paused={summary.skipped_paused}"
        )
        if summary.failed > 0:
            raise typer.Exit(code=1)
        return

    if daemon:
        from wxsp.config import is_packaged

        if is_packaged():
            import threading

            import uvicorn

            def _serve_web() -> None:
                uvicorn.run(
                    "wxsp.api.app:app",
                    host=settings.webui.host,
                    port=settings.webui.port,
                    log_level="info",
                )

            threading.Thread(target=_serve_web, daemon=True, name="web-ui").start()
            if settings.webui.open_browser_on_start:
                import time
                import webbrowser

                def _open() -> None:
                    time.sleep(1.0)
                    try:
                        webbrowser.open(f"http://{settings.webui.host}:{settings.webui.port}/")
                    except Exception:
                        pass

                threading.Thread(target=_open, daemon=True, name="open-browser").start()

        typer.echo("[wxsp] 启动 daemon(按 Ctrl-C 退出)...")
        try:
            start_daemon(settings)
        except (KeyboardInterrupt, SystemExit):
            typer.echo("[wxsp] daemon 退出")
        return

    typer.echo("[wxsp] 请指定 --task-id N / --today / --daemon 之一")
    raise typer.Exit(code=2)


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


@app.command("cleanup")
def cleanup() -> None:
    """清理过保留期的日志 / 失败截图(M9)。

    保留期来自 config.yaml/monitoring.log_retention_days (默认 30) +
    monitoring.screenshot_retention_days (默认 90)。daemon 启动也会自动跑一次。
    """
    settings = load_settings()
    report = cleanup_old_files(
        logs_dir=settings.app.logs_dir,
        log_retention_days=settings.monitoring.log_retention_days,
        screenshot_retention_days=settings.monitoring.screenshot_retention_days,
    )
    typer.echo(
        f"[wxsp] 清理完成: 日志 {report.logs_removed} 个 / 截图 {report.screenshots_removed} 个 "
        f"/ 释放 {report.bytes_freed} bytes"
    )


@app.command("web")
def web(
    port: int | None = typer.Option(None, "--port", "-p", help="覆盖 config.yaml 的端口"),
    host: str | None = typer.Option(None, "--host", help="覆盖 config.yaml 的 host"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
) -> None:
    """启动 Web UI(FastAPI + Jinja2 + HTMX,SSE 日志流)。

    单进程:uvicorn + 直接渲染模板。9 个路由 + 1 个 SSE 端点。任务由飞书 Bitable
    创建,Web UI 只做查看 / 触发 / 扫码 / 配置(运维控制台)。
    """
    import threading
    import time
    import webbrowser

    import uvicorn
    from loguru import logger as _logger

    # frozen + Windows 下 loguru 默认 stderr sink 不工作(包了 NullWriter 之类),
    # 这里显式再 add 一个 sys.stderr sink 保底,确保 logger.info/warning 能输出。
    # 重复 add 不会冲突(各自 sink id),最多多一行重复输出,无害。
    _logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level: <5} | {message}")

    # M11 setup mode: config.yaml 不存在时,用默认参数起 Web UI,让向导接管。
    try:
        settings = load_settings()
        typer.echo(f"[wxsp] logs_dir = {settings.app.logs_dir}")
        try:
            install_file_sink(
                logs_dir=settings.app.logs_dir,
                retention_days=settings.monitoring.log_retention_days,
            )
            # 装完 sink 立刻写一条横幅,验证文件 sink 是否真在工作(frozen 模式诊断用)
            _logger.info(f"[wxsp] sink ready: file={settings.app.logs_dir} + stderr + sse")
            typer.echo(f"[wxsp] 日志已就绪,文件:{settings.app.logs_dir}/wxsp.<日期>.log")
        except Exception as exc:
            typer.echo(f"[wxsp] 装日志 sink 失败(继续 stderr): {exc}")
        bind_host = host or settings.webui.host
        bind_port = port or settings.webui.port
        open_browser = settings.webui.open_browser_on_start and not no_browser
    except FileNotFoundError:
        typer.echo("[wxsp] 未发现 config.yaml,以 setup 模式启动(浏览器会跳向导)")
        bind_host = host or "127.0.0.1"
        bind_port = port or 8765
        open_browser = not no_browser

    if open_browser:

        def _open_later() -> None:
            time.sleep(1.0)  # 给 uvicorn 一点点起服务的时间;失败也无所谓,只是没自动开
            try:
                webbrowser.open(f"http://{bind_host}:{bind_port}/")
            except Exception:
                pass

        threading.Thread(target=_open_later, daemon=True, name="open-browser").start()

    typer.echo(f"[wxsp] Web UI 启动:http://{bind_host}:{bind_port}/  (Ctrl-C 退出)")
    uvicorn.run("wxsp.api.app:app", host=bind_host, port=bind_port, log_level="info")


if __name__ == "__main__":
    app()
