"""Accounts:列表 + 扫码登录 + 暂停/恢复 + 立即同步飞书。

设计要点:
- "扫码登录":POST 触发后台线程跑 check_cookie(),浏览器弹窗显示视频号二维码,
  用户在 patchright 窗口里扫;Web 不嵌入二维码(CDP 抓 canvas 复杂且不稳),
  接口立即返回"已弹出窗口,请扫码"提示,完成后 DB.cookie_status 自动刷新。
- "立即同步飞书":HTMX 同步触发 + 内联状态;失败用 HX-Trigger 推 opError 弹窗。
  原本走后台线程 + 跳转 + flash,但运营反馈"飞书挂了从 UI 看不出",改成在路由内
  阻塞跑 sync_now(),把结果直接渲染成片段 + 关键错误推弹窗。
- "暂停/恢复":同步改 DB.paused_until,直接 redirect 回列表。
- 大部分 POST 还是 303 redirect 回 /accounts;只有 sync 例外用 HTMX 片段。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from sqlmodel import Session, select

from wxsp.api.deps import get_session, get_settings, templates
from wxsp.config import Settings
from wxsp.models import Account

# 并发触发 sync 没必要(浪费 API quota + 写库冲突)。HTMX 同步阻塞 + lock 即可,
# 抢不到锁返回'正在同步中'片段;sync_now 本身 1-5 秒。
_sync_lock = threading.Lock()

# 进程级 login 在飞 tracker:account_id → 仍在跑的 login Thread。
# 解决"运营点两次扫码登录"刷出两个 chromium 进程抢同一个 user_data_dir,导致后开的
# 那个抛 'Target page, context or browser has been closed' + DB cookie_status 被
# 覆盖成 unknown。Lock 保护 dict 读写;Thread.is_alive() 判断真实存活态。
# 「打开浏览器」手动操作也注册进同一个 dict —— 同一 user_data_dir 只能被一个 chromium
# 打开,故 login 与手动开浏览器互斥(谁先占用谁赢,另一个被提示去用已开的窗口)。
_login_in_flight: dict[str, threading.Thread] = {}
_login_lock = threading.Lock()

# 「打开浏览器」手动操作的封顶时长(防用户忘了关窗口导致线程 + profile 锁泄漏)。
# 到点强制关浏览器释放 profile;手动操作账号一般远不到 2 小时。
_OPEN_BROWSER_MAX_SECONDS = 2 * 60 * 60

router = APIRouter()


def _spawn(name: str, fn: Any, *args: Any, **kwargs: Any) -> None:
    """启动 daemon 线程跑阻塞任务;异常 log 不抛。"""

    def runner() -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            logger.exception(f"[web/{name}] {exc}")

    threading.Thread(target=runner, daemon=True, name=f"web-{name}").start()


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    flash: str | None = None,
    platform: str | None = None,
) -> HTMLResponse:
    db_rows = {a.id: a for a in session.exec(select(Account)).all()}
    # Collect available platforms for filter dropdown
    platforms_seen: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for aid, cfg in settings.accounts.items():
        a = db_rows.get(aid)
        p = getattr(cfg, "platform", "tencent_channel") or "tencent_channel"
        platforms_seen[p] = p
        if platform and p != platform:
            continue
        rows.append(
            {
                "id": aid,
                "display_name": cfg.display_name,
                "enabled": cfg.enabled,
                "daily_limit": cfg.daily_limit,
                "platform": p,
                "is_active": a.is_active if a else False,
                "cookie_status": a.cookie_status if a else "unknown",
                "cookie_last_active_at": a.cookie_last_active_at if a else None,
                "community_status": a.community_status if a else "unknown",
                "community_last_active_at": a.community_last_active_at if a else None,
                "paused_until": a.paused_until if a else None,
                "in_db": a is not None,
            }
        )
    platform_options = sorted(platforms_seen.keys())
    return templates.TemplateResponse(
        request,
        "accounts.html",
        {
            "active": "accounts",
            "rows": rows,
            "flash": flash,
            "filter_platform": platform or "",
            "platform_options": platform_options,
        },
    )


def _redirect(flash: str, *, platform: str = "") -> RedirectResponse:
    qs = f"flash={flash}"
    if platform:
        qs += f"&platform={platform}"
    return RedirectResponse(url=f"/accounts?{qs}", status_code=303)


@router.post("/accounts/{account_id}/pause")
def pause_account(
    account_id: str,
    request: Request,
    hours: int = Form(24),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    row = session.get(Account, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    row.paused_until = datetime.now() + timedelta(hours=hours)
    session.add(row)
    plat = getattr(request.state, "current_platform", "") or ""
    return _redirect(f"已暂停 {account_id} {hours} 小时", platform=plat)


@router.post("/accounts/{account_id}/resume")
def resume_account(
    account_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    row = session.get(Account, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    row.paused_until = None
    session.add(row)
    plat = getattr(request.state, "current_platform", "") or ""
    return _redirect(f"已恢复 {account_id}", platform=plat)


@router.post("/accounts/{account_id}/login")
def login_account(
    account_id: str,
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """触发扫码登录:后台线程开 patchright,弹窗里显示平台二维码。

    去重:若该账号已有 login 线程在跑,**不再 spawn 第二个**,直接 redirect 提示
    去已弹出的窗口扫。原因:同一个 user_data_dir 不能被两个 chromium 同时打开,
    第二次 spawn 必然抛 'Target page, context or browser has been closed',且
    DB cookie_status 会被覆盖成 unknown,运营会困惑。
    """
    cfg = settings.accounts.get(account_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 未配置")
    if session.get(Account, account_id) is None:
        session.add(
            Account(
                id=account_id,
                display_name=cfg.display_name,
                user_data_dir=str(cfg.user_data_dir),
                daily_limit=cfg.daily_limit,
            )
        )
    plat = getattr(request.state, "current_platform", "") or ""
    # plat(请求上下文)优先于 AccountConfig.platform 的 Pydantic 默认值
    # (默认值 "tencent_channel" 对淘宝账号是错的)
    account_platform = plat or getattr(cfg, "platform", "") or "tencent_channel"
    with _login_lock:
        existing = _login_in_flight.get(account_id)
        if existing is not None and existing.is_alive():
            return _redirect(
                f"账号 {account_id} 已在扫码中,请到已弹出的浏览器窗口扫码;"
                "想取消可手动关掉那个窗口再重试",
                platform=plat,
            )
        thread = threading.Thread(
            target=_login_runner,
            args=(account_id, Path(cfg.user_data_dir), account_platform),
            daemon=True,
            name=f"web-login-{account_id}",
        )
        _login_in_flight[account_id] = thread
        thread.start()
    return _redirect(
        f"已弹出浏览器,请在窗口中扫码登录 {account_id}(完成后状态自动刷新)", platform=plat
    )


def _login_runner(account_id: str, user_data_dir: Path, platform: str = "tencent_channel") -> None:
    """_run_login 的薄包装:无论成功失败,finally 里把 _login_in_flight 清掉,
    让下次 POST 能起新线程。
    """
    try:
        _run_login(account_id, user_data_dir, platform=platform)
    finally:
        with _login_lock:
            _login_in_flight.pop(account_id, None)


def _is_browser_process_alive(user_data_dir: str) -> bool:
    """检查是否有 chromium 进程正占用该 user_data_dir。

    macOS 上用户关窗口后 page.is_closed()/close 事件长时间不触发,导致 _run_open_browser
    线程卡在等待循环里。用进程级检查替代线程级检查来判断"浏览器是否真的还在":
    若进程已退出,即使线程还卡着也允许重开。
    """
    import subprocess
    import sys

    needle = str(Path(user_data_dir).resolve())
    if sys.platform == "win32":
        cmd = ["wmic", "process", "where", "name='chrome.exe'", "get", "CommandLine"]
    else:
        cmd = ["ps", "-e", "-o", "command="]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True  # 查询失败时保守认为还活着,避免误杀运行中的浏览器
    # 只认 Chromium/Chrome 主进程(含 --user-data-dir),不匹配无关进程
    for line in out.splitlines():
        if needle in line and "--user-data-dir" in line and "Chrom" in line:
            return True
    return False


@router.post("/accounts/{account_id}/open-browser")
def open_browser_account(
    account_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """打开账号浏览器停在平台主页,**不做任何自动操作**,供运营手动操作自己的账号。

    与扫码登录共用 _login_in_flight 去重:同一 user_data_dir 不能被两个 chromium
    同时打开(否则后开的崩 + 互相踢登录),故 login 与手动开浏览器互斥。

    macOS 上用户关窗口后 patchright 的 close 事件延迟触发(~60s),线程卡在等待循环
    里不会立即退出 → 纯 is_alive() 检查会误判"还有窗口打开"。故额外检查 chromium 进程
    是否真的还在:进程没了就清掉旧的 in-flight 记录,允许立即重开。
    """
    cfg = settings.accounts.get(account_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 未配置")
    plat = getattr(request.state, "current_platform", "") or ""
    # plat(请求上下文)优先于 AccountConfig.platform 的 Pydantic 默认值(对淘宝/抖音是错的)
    account_platform = plat or getattr(cfg, "platform", "") or "tencent_channel"
    with _login_lock:
        existing = _login_in_flight.get(account_id)
        if existing is not None and existing.is_alive():
            # 线程还活着,但可能卡在关窗口后的等待循环里 —— 检查浏览器进程是否真还在
            if _is_browser_process_alive(str(cfg.user_data_dir)):
                return _redirect(
                    f"账号 {account_id} 已有浏览器窗口打开(扫码或手动),直接用那个窗口;"
                    "想重开先关掉它再点",
                    platform=plat,
                )
            # 浏览器进程已退出但线程还在清理中:清掉旧记录,允许立即重开
            logger.info(f"[web/open-browser] {account_id} 旧线程仍在但浏览器进程已退出,允许重开")
            _login_in_flight.pop(account_id, None)
        thread = threading.Thread(
            target=_open_browser_runner,
            args=(account_id, Path(cfg.user_data_dir), account_platform),
            daemon=True,
            name=f"web-open-browser-{account_id}",
        )
        _login_in_flight[account_id] = thread
        thread.start()
    return _redirect(
        f"已打开 {account_id} 的浏览器,请在窗口里手动操作;操作完直接关掉窗口即可",
        platform=plat,
    )


@router.post("/accounts/{account_id}/community-login")
def community_login_account(
    account_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """小红书社区站(www.xiaohongshu.com)登录:打开浏览器到发现页,等用户手动登录。

    社区站和创作者中心(creator.xiaohongshu.com)不同域名,cookie 不共享。预热浏览 /
    发布后浏览需要社区站登录态。与扫码登录共用 _login_in_flight 去重(同一 user_data_dir)。
    """
    cfg = settings.accounts.get(account_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 未配置")
    plat = getattr(request.state, "current_platform", "") or ""
    with _login_lock:
        existing = _login_in_flight.get(account_id)
        if existing is not None and existing.is_alive():
            if _is_browser_process_alive(str(cfg.user_data_dir)):
                return _redirect(
                    f"账号 {account_id} 已有浏览器窗口打开,直接用那个窗口登录社区;"
                    "想重开先关掉它再点",
                    platform=plat,
                )
            _login_in_flight.pop(account_id, None)
        thread = threading.Thread(
            target=_community_login_runner,
            args=(account_id, Path(cfg.user_data_dir)),
            daemon=True,
            name=f"web-community-login-{account_id}",
        )
        _login_in_flight[account_id] = thread
        thread.start()
    return _redirect(
        f"已打开浏览器,请在窗口中登录小红书社区站 {account_id}(完成后状态自动刷新)",
        platform=plat,
    )


def _community_login_runner(account_id: str, user_data_dir: Path) -> None:
    """_run_community_login 的薄包装:无论成功失败,finally 里清掉 _login_in_flight。

    对齐 _open_browser_runner 结构:pop 放 finally,确保关窗口/异常都能重开。
    """
    try:
        _run_community_login(account_id, user_data_dir)
    finally:
        with _login_lock:
            _login_in_flight.pop(account_id, None)


def _run_community_login(account_id: str, user_data_dir: Path) -> None:
    """开浏览器到 www.xiaohongshu.com/explore,等用户手动登录,关窗口后写 DB。

    结构对齐 _run_open_browser:_skip_cleanup=True 让 browser_context 跳过 finally 的
    cleanup(goto about:blank + browser.close + terminate),避免关窗口后 Chrome 进程
    残留导致 _is_browser_process_alive 误判。pop 由 _community_login_runner 的 finally 负责。

    检测逻辑:轮询 page.url 含 /explore = 已登录(未登录会被重定向到登录页)。
    """
    import threading as _th
    from datetime import datetime as _dt

    from wxsp.browser import browser_context
    from wxsp.db import get_engine, session_scope
    from wxsp.models import COOKIE_STATUS_EXPIRED, COOKIE_STATUS_OK, Account
    from wxsp.platforms import xiaohongshu_selectors as sel

    cookie_file = user_data_dir / "cookies.json"
    is_logged_in = False
    try:
        with browser_context(
            user_data_dir,
            headless=False,
            account_id=account_id,
            platform="xiaohongshu",
            _skip_cleanup=True,
        ) as page:
            try:
                page.goto(sel.EXPLORE_URL, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                logger.warning(f"[web/community-login] {account_id} 打开发现页失败: {exc}")

            closed = _th.Event()
            page.on("close", lambda *_: closed.set())

            deadline = time.monotonic() + 300  # 5 分钟
            last_flush = 0.0
            while time.monotonic() < deadline:
                if closed.wait(timeout=5):
                    break
                # 检测社区站登录态:URL 含 /explore = 已登录(未登录被重定向走)
                try:
                    if "/explore" in page.url:
                        is_logged_in = True
                except Exception:
                    pass
                # 周期性 flush cookie(同 _run_open_browser)
                if time.monotonic() - last_flush >= 30:
                    last_flush = time.monotonic()
                    try:
                        cookies = page.context.cookies()
                        with open(cookie_file, "w") as f:
                            json.dump(cookies, f, ensure_ascii=False)
                    except Exception:
                        pass
            # 退出前最后一次 flush(context 仍活着,确保最新 cookie 落盘)
            try:
                cookies = page.context.cookies()
                with open(cookie_file, "w") as f:
                    json.dump(cookies, f, ensure_ascii=False)
            except Exception:
                pass
    except Exception as exc:
        logger.exception(f"[web/community-login] {account_id} 浏览器异常: {exc}")
        return

    # browser_context 已退出(_skip_cleanup=True 不 terminate Chrome,进程由 patchright 自行回收)
    # DB 写入登录态
    engine = get_engine()
    with session_scope(engine) as s:
        acc = s.get(Account, account_id)
        if acc is not None:
            acc.community_status = COOKIE_STATUS_OK if is_logged_in else COOKIE_STATUS_EXPIRED
            if is_logged_in:
                acc.community_last_active_at = _dt.now()
            s.add(acc)
            logger.info(f"[web/community-login] {account_id} 社区登录态={acc.community_status}")


def _open_browser_runner(
    account_id: str, user_data_dir: Path, platform: str = "tencent_channel"
) -> None:
    """_run_open_browser 的薄包装:无论成功失败,finally 清掉 _login_in_flight。"""
    try:
        _run_open_browser(account_id, user_data_dir, platform=platform)
    finally:
        with _login_lock:
            _login_in_flight.pop(account_id, None)


@router.post("/accounts/sync", response_class=HTMLResponse)
def trigger_sync(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    """同步执行飞书 Bitable sync;返回 HTML 片段(HTMX 用)。

    设计:
    - 飞书未启用 → 200 + 灰色片段提示。
    - 锁占用 → 200 + 提示'正在同步中'(不重复触发)。
    - 成功 → 200 + 绿色片段 + 入库/拒绝计数。
    - 失败 → 200 + 红色片段 + HX-Trigger 头 opError 让前端弹 modal(因为 HTMX
      默认遇到非 2xx 会走 onResponseError 路径,我们用 200 + header 控更稳)。

    并发:_sync_lock 串行。抢不到 = 已有同步在跑,返回提示不重做。
    """
    platform = getattr(request.state, "current_platform", "tencent_channel") or "tencent_channel"
    if not settings.feishu.enabled:
        return HTMLResponse(_fragment("warn", "飞书未启用,跳过同步"))
    if not _sync_lock.acquire(blocking=False):
        return HTMLResponse(_fragment("warn", "正在同步中,请等当前同步完成"))
    try:
        from wxsp.sync import sync_now

        result = sync_now(settings, platform=platform)
    except Exception as exc:
        logger.exception(f"[web/feishu-sync] {exc}")
        msg = f"飞书同步失败:{exc}"
        return HTMLResponse(
            _fragment("error", msg),
            headers={
                "HX-Trigger": json.dumps({"opError": {"title": "飞书同步失败", "detail": str(exc)}})
            },
        )
    finally:
        _sync_lock.release()
    parts = [f"新入库 {result.accepted} 条"]
    if result.updated:
        parts.append(f"覆盖更新 {result.updated} 条")
    if result.rejected:
        wb_note = ""
        if result.writeback_failed > 0:
            wb_note = f"(回写失败 {result.writeback_failed} 行,未同步到飞书)"
        else:
            wb_note = "(已回写飞书)"
        parts.append(f"校验失败 {result.rejected} 条{wb_note}")
    if result.skipped_existing:
        parts.append(f"已存在 {result.skipped_existing} 条跳过")
    if result.skipped_incomplete:
        parts.append(f"未填完 {result.skipped_incomplete} 条等下次")
    if result.writeback_failed > 0 and result.rejected == 0:
        parts.append(f"⚠ 回写失败 {result.writeback_failed} 行")
    level = "warn" if result.writeback_failed > 0 else "ok"
    return HTMLResponse(_fragment(level, "飞书同步完成:" + "、".join(parts)))


def _fragment(level: str, text: str) -> str:
    """渲染一个 flash 片段。level: ok | warn | error。"""
    safe = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    return f'<div class="flash {level}">{safe}</div>'


# ---------- 后台 worker(不持任何 request 资源) ----------


def _run_login(account_id: str, user_data_dir: Path, *, platform: str = "tencent_channel") -> None:
    """后台跑扫码:check_cookie 同步阻塞最长 5 分钟,完成后写 DB。"""
    from datetime import datetime as _dt

    from wxsp.browser import check_cookie
    from wxsp.db import get_engine, session_scope
    from wxsp.doctor import record_cookie_check

    try:
        is_logged_in: bool | None = check_cookie(
            user_data_dir, timeout_ms=300_000, account_id=account_id, platform=platform
        )
    except Exception as exc:
        logger.exception(f"[web/login] {account_id} 浏览器异常: {exc}")
        is_logged_in = None
    engine = get_engine()
    with session_scope(engine) as s:
        record_cookie_check(s, account_id, is_logged_in=is_logged_in, now=_dt.now())


def _run_open_browser(
    account_id: str, user_data_dir: Path, *, platform: str = "tencent_channel"
) -> None:
    """开浏览器停在平台主页,不做任何操作,等用户手动关窗口(封顶 _OPEN_BROWSER_MAX_SECONDS)。

    退出信号用事件驱动(page.on("close"))而非轮询 page.is_closed()——后者在 macOS 上
    用户关窗口后长时间不返回 True,导致 while 循环不退出、线程卡在 _login_in_flight 里,
    用户紧接着点"打开浏览器"被去重拦截。

    非指纹平台(淘宝/抖音/快手/小红书)用 cookies.json 显式持久化登录态。用户手动关
    窗口时 context 已死,browser_context 的 finally 里 context.cookies() 会抛"Target
    closed"导致 cookie 存不上 → 下次发布恢复旧 cookie 跳登录页。故在循环里每 30s
    在 context 仍活着时主动 flush cookie,关窗口时最近一次已落盘。
    """
    import threading

    from wxsp.browser import browser_context, login_meta_for
    from wxsp.platform_meta import get_meta

    home_url = login_meta_for(platform).get("home_url", "about:blank")
    cookie_file = user_data_dir / "cookies.json"
    use_explicit_cookies = not get_meta(platform).needs_fingerprint
    try:
        with browser_context(
            user_data_dir,
            headless=False,
            account_id=account_id,
            platform=platform,
            _skip_cleanup=True,
        ) as page:
            try:
                page.goto(home_url, wait_until="domcontentloaded")
            except Exception as exc:
                logger.warning(f"[web/open-browser] {account_id} 打开主页失败: {exc}")

            closed = threading.Event()
            page.on("close", lambda *_: closed.set())

            deadline = time.monotonic() + _OPEN_BROWSER_MAX_SECONDS
            last_flush = 0.0
            while time.monotonic() < deadline:
                if closed.wait(timeout=5):
                    break
                # 周期性 flush cookie:context 活着时存,关窗口后 finally 存不上也不怕
                if use_explicit_cookies and time.monotonic() - last_flush >= 30:
                    last_flush = time.monotonic()
                    try:
                        cookies = page.context.cookies()
                        with open(cookie_file, "w") as f:
                            json.dump(cookies, f, ensure_ascii=False)
                    except Exception:
                        pass  # context 刚死 / 页面跳转中,下次轮询再试
            # 退出前最后一次 flush(context 仍活着,确保最新 cookie 落盘)
            if use_explicit_cookies:
                try:
                    cookies = page.context.cookies()
                    with open(cookie_file, "w") as f:
                        json.dump(cookies, f, ensure_ascii=False)
                except Exception:
                    pass
    except Exception as exc:
        logger.exception(f"[web/open-browser] {account_id} 浏览器异常: {exc}")
