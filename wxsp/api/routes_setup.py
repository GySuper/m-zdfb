"""向导后端:6 页表单 → 写 config.yaml → 注册自启 → 跳 /accounts(M11)。

向导数据存在 app.state.wizard_data(进程内 dict),重启 daemon 数据丢失,运营要重来。
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response as StarletteResponse

from wxsp import config as wxsp_config
from wxsp.api.deps import templates

router = APIRouter(prefix="/setup")

_ACCOUNT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TOTAL_STEPS = 6
_STEP_KEYS = {
    2: "feishu",
    3: "nas",
    4: "accounts",
    5: "notify",
}


def _get_wizard_data(request: Request) -> dict[str, Any]:
    if not hasattr(request.app.state, "wizard_data"):
        request.app.state.wizard_data = {}
    result: dict[str, Any] = request.app.state.wizard_data
    return result


def _last_completed_step(data: dict[str, Any]) -> int:
    completed = 1
    for step, key in sorted(_STEP_KEYS.items()):
        if key in data:
            completed = step
        else:
            break
    return completed


def _required_step_or_redirect(request: Request, target_step: int) -> RedirectResponse | None:
    """提交 step N 时,前置 step 数据缺失 → 重定向到下一个该填的 step。"""
    data = _get_wizard_data(request)
    last = _last_completed_step(data)
    if target_step > last + 1:
        return RedirectResponse(url=f"/setup/step/{last + 1}", status_code=302)
    return None


@router.get("/step/{step}", response_class=HTMLResponse)
def render_step(step: int, request: Request) -> HTMLResponse:
    if step < 1 or step > _TOTAL_STEPS:
        raise HTTPException(status_code=404)
    data = _get_wizard_data(request)
    template_name = {
        1: "setup/welcome.html",
        2: "setup/feishu.html",
        3: "setup/nas.html",
        4: "setup/accounts.html",
        5: "setup/notify.html",
        6: "setup/complete.html",
    }[step]

    ctx: dict[str, Any] = {
        "step": step,
        "total_steps": _TOTAL_STEPS,
        "data": data,
    }
    if step == 1:
        ctx["data_dir"] = str(wxsp_config.get_user_data_dir())
        ctx["logs_dir"] = str(wxsp_config.get_user_logs_dir())
        ctx["self_check"] = _run_self_check()
    if step == 6:
        ctx["summary"] = _build_summary(data)
    return templates.TemplateResponse(request, template_name, ctx)


def _run_self_check() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    data_dir = wxsp_config.get_user_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append({"name": "用户数据目录可写", "ok": True, "detail": str(data_dir)})
    except Exception as exc:
        checks.append({"name": "用户数据目录可写", "ok": False, "detail": str(exc)})

    from wxsp.browser import _chromium_root

    chromium = _chromium_root()
    if chromium is None:
        checks.append({"name": "Chromium", "ok": True, "detail": "开发模式由 patchright 自动管理"})
    elif chromium.exists():
        checks.append({"name": "Chromium", "ok": True, "detail": str(chromium)})
    else:
        checks.append({"name": "Chromium", "ok": False, "detail": f"未找到: {chromium}"})
    return checks


def _build_summary(data: dict[str, Any]) -> dict[str, Any]:
    feishu = data.get("feishu", {})
    accounts = data.get("accounts", [])
    notify = data.get("notify", {})
    return {
        "feishu_app_id_masked": feishu.get("app_id", "")[:6] + "..."
        if feishu.get("app_id")
        else "(未填)",
        "nas_root": data.get("nas", {}).get("nas_root", "(未填)"),
        "account_count": len(accounts),
        "wecom_enabled": bool(notify.get("webhook")),
    }


@router.post("/step/2", response_model=None)
def submit_feishu(
    request: Request,
    app_id: str = Form(...),
    app_secret: str = Form(...),
    app_token: str = Form(...),
    table_id: str = Form(...),
) -> StarletteResponse:
    redirect = _required_step_or_redirect(request, 2)
    if redirect:
        return redirect
    if not all([app_id.strip(), app_secret.strip(), app_token.strip(), table_id.strip()]):
        return templates.TemplateResponse(
            request,
            "setup/feishu.html",
            {"step": 2, "total_steps": _TOTAL_STEPS, "error": "所有字段均必填"},
            status_code=200,
        )
    _get_wizard_data(request)["feishu"] = {
        "app_id": app_id.strip(),
        "app_secret": app_secret.strip(),
        "app_token": app_token.strip(),
        "table_id": table_id.strip(),
    }
    return RedirectResponse(url="/setup/step/3", status_code=302)


@router.post("/step/3", response_model=None)
def submit_nas(request: Request, nas_root: str = Form(...)) -> StarletteResponse:
    nas_path = Path(nas_root.strip())
    if not nas_path.exists():
        # 路径不存在时,检查先决步骤,引导用户回填缺失的步骤
        redirect = _required_step_or_redirect(request, 3)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request,
            "setup/nas.html",
            {
                "step": 3,
                "total_steps": _TOTAL_STEPS,
                "error": f"路径不存在: {nas_path}",
                "data": _get_wizard_data(request),
            },
            status_code=200,
        )
    _get_wizard_data(request)["nas"] = {"nas_root": str(nas_path)}
    return RedirectResponse(url="/setup/step/4", status_code=302)


@router.post("/step/4", response_model=None)
async def submit_accounts(request: Request) -> StarletteResponse:
    form = await request.form()
    ids = [str(v) for v in form.getlist("account_id[]")]
    names = [str(v) for v in form.getlist("display_name[]")]
    limits = [str(v) for v in form.getlist("daily_limit[]")]

    def _render_error(msg: str) -> StarletteResponse:
        return templates.TemplateResponse(
            request,
            "setup/accounts.html",
            {"step": 4, "total_steps": _TOTAL_STEPS, "error": msg},
            status_code=200,
        )

    if not ids or len(ids) != len(names) or len(ids) != len(limits):
        return _render_error("至少 1 个账号,字段数对不齐")

    accounts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for acc_id, name, limit in zip(ids, names, limits, strict=False):
        acc_id = acc_id.strip()
        if not _ACCOUNT_ID_RE.match(acc_id):
            return _render_error(f"account_id 格式非法: {acc_id}(需小写字母开头,英文蛇形)")
        if acc_id in seen_ids:
            return _render_error(f"account_id 重复: {acc_id}")
        seen_ids.add(acc_id)
        try:
            limit_int = int(limit)
            if limit_int < 1:
                raise ValueError("daily_limit must be >= 1")
        except ValueError:
            return _render_error(f"daily_limit 必须 ≥ 1: {limit}")
        accounts.append({"id": acc_id, "display_name": name.strip(), "daily_limit": limit_int})

    # Validation passed — now enforce wizard step ordering
    redirect = _required_step_or_redirect(request, 4)
    if redirect:
        return redirect

    _get_wizard_data(request)["accounts"] = accounts
    return RedirectResponse(url="/setup/step/5", status_code=302)


@router.post("/step/5")
def submit_notify(request: Request, webhook: str = Form("")) -> RedirectResponse:
    redirect = _required_step_or_redirect(request, 5)
    if redirect:
        return redirect
    _get_wizard_data(request)["notify"] = {"webhook": webhook.strip()}
    return RedirectResponse(url="/setup/step/6", status_code=302)


@router.post("/complete")
def complete(request: Request) -> RedirectResponse:
    data = _get_wizard_data(request)
    if _last_completed_step(data) < 5:
        return RedirectResponse(
            url=f"/setup/step/{_last_completed_step(data) + 1}", status_code=302
        )

    config = _render_config(data)
    config_path = wxsp_config.get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    request.app.state.wizard_data = {}
    return RedirectResponse(url="/accounts", status_code=302)


def _render_config(data: dict[str, Any]) -> dict[str, Any]:
    feishu = data["feishu"]
    nas_root = data["nas"]["nas_root"]
    accounts = data["accounts"]
    webhook = data.get("notify", {}).get("webhook", "")

    data_dir = wxsp_config.get_user_data_dir()
    logs_dir = wxsp_config.get_user_logs_dir()

    accounts_yaml: dict[str, Any] = {}
    for acc in accounts:
        acc_id = acc["id"]
        accounts_yaml[acc_id] = {
            "display_name": acc["display_name"],
            "enabled": True,
            "daily_limit": acc["daily_limit"],
            "user_data_dir": str(data_dir / "chrome-profiles" / acc_id),
            "video_search_root": f"{{nas_root}}/videos/{acc_id}",
            "cover_search_root": f"{{nas_root}}/covers/{acc_id}",
        }

    return {
        "app": {
            "data_dir": str(data_dir),
            "logs_dir": str(logs_dir),
            "timezone": "Asia/Shanghai",
        },
        "paths": {"nas_root": nas_root},
        "accounts": accounts_yaml,
        "scheduler": {"daily_cron_hour": 9, "daily_cron_minute": 0, "strategy": "round-robin"},
        "publisher": {
            "headless": False,
            "upload_timeout_seconds": 600,
            "step_pause_seconds": [1, 3],
            "screenshot_on_error": True,
            "max_concurrent_accounts": 1,
        },
        "feishu": {
            "enabled": True,
            "app_id": feishu["app_id"],
            "app_secret": feishu["app_secret"],
            "bitable": {"app_token": feishu["app_token"], "table_id": feishu["table_id"]},
            "sync": {"write_back_enabled": True},
        },
        "monitoring": {
            "cookie_warn_days": 1.5,
            "notifiers": {
                "wecom": {
                    "enabled": bool(webhook),
                    "webhook": webhook or "(留空)",
                }
            },
            "notify_on": [
                "cookie_expired",
                "cookie_warning",
                "risk_control",
                "task_failed",
                "element_not_found",
                "nas_unreachable",
                "backlog_high",
                "run_summary",
            ],
            "log_retention_days": 30,
            "screenshot_retention_days": 90,
            "backlog_warn_threshold": 20,
        },
        "webui": {"host": "127.0.0.1", "port": 8765, "open_browser_on_start": True},
    }


@router.post("/test-feishu")
def test_feishu(
    app_id: str = Form(...),
    app_secret: str = Form(...),
    app_token: str = Form(...),
    table_id: str = Form(...),
) -> HTMLResponse:
    """点"测试连接":拿表单值实例化 LarkClient 拉一条记录。"""
    from wxsp.feishu import fetch_pending_rows, make_client

    try:
        client = make_client(app_id.strip(), app_secret.strip())
        rows = fetch_pending_rows(
            client,
            app_token=app_token.strip(),
            table_id=table_id.strip(),
            status_field="状态",
        )
        sample_fields = list(rows[0].fields.keys()) if rows else []
        escaped_fields = html.escape(", ".join(sample_fields[:8]))
        return HTMLResponse(
            f'<div class="check-ok">✓ 连接成功,拉到 {len(rows)} 行。字段名: {escaped_fields}</div>'
        )
    except Exception as exc:
        return HTMLResponse(
            f'<div class="check-fail">✗ {html.escape(str(exc))}</div>', status_code=200
        )


@router.post("/probe-path")
def probe_path(path: str = Form(...)) -> HTMLResponse:
    """NAS 路径实时检测:存在 / 不存在。"""
    p = Path(path.strip())
    if p.exists() and p.is_dir():
        return HTMLResponse(f'<div class="check-ok">✓ 路径存在: {html.escape(str(p))}</div>')
    return HTMLResponse(f'<div class="check-fail">✗ 不可达: {html.escape(str(p))}</div>')


@router.post("/test-wecom")
def test_wecom(webhook: str = Form(...)) -> HTMLResponse:
    """企微告警按钮:发一条测试消息。"""
    from wxsp.notify import NotifyEvent, WecomNotifier

    if not webhook.strip():
        return HTMLResponse('<div class="check-fail">✗ webhook 为空</div>')
    notifier = WecomNotifier(webhook=webhook.strip())
    try:
        ok = notifier.send(
            NotifyEvent(
                type="setup_test",
                level="info",
                title="wxsp 安装测试",
                content="如果你看到这条消息,说明 webhook 配置正确。",
            )
        )
        return HTMLResponse(
            '<div class="check-ok">✓ 消息已发,看下群</div>'
            if ok
            else '<div class="check-fail">✗ 发送失败,看后端日志</div>'
        )
    except Exception as exc:
        return HTMLResponse(f'<div class="check-fail">✗ {html.escape(str(exc))}</div>')
