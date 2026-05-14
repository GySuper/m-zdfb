"""配置:UI 表单编辑 config.yaml。

按 section 拆成一组组表单字段(input / checkbox / select),提交后:
1. 把表单字段拼成完整 dict(嵌套对齐 Settings 模型)
2. 敏感字段(app_secret / webhook 等)若 UI 提交空 → 自动从磁盘旧文件回填(用户没动的不被吹掉)
3. 飞书 / 企微的 ${ENV_VAR} 形式引用允许保留,只要展开后非空
4. Pydantic Settings 完整校验
5. 任一步失败 → 不写盘 + 红色错误展示;通过 → cp config.yaml.bak + 原子写
6. config.yaml 每个请求都重读,改完即时生效,无需重启
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from wxsp.api.deps import templates
from wxsp.config import Settings, _expand_env_vars

router = APIRouter()

_CONFIG_PATH = Path("config.yaml")
_BACKUP_PATH = Path("config.yaml.bak")

# 5 类告警 + cookie_warning(monitoring 配置里有)
NOTIFY_ON_OPTIONS: list[tuple[str, str]] = [
    ("cookie_expired", "Cookie 失效"),
    ("cookie_warning", "Cookie 即将过期"),
    ("risk_control", "风控触发"),
    ("task_failed", "任务失败"),
    ("element_not_found", "元素未找到(可能改版)"),
    ("nas_unreachable", "NAS 不可达"),
]

# 飞书字段映射(用户在飞书表里的列名)
FIELD_MAP_KEYS: list[tuple[str, str]] = [
    ("video_file", "视频文件"),
    ("title", "标题"),
    ("description", "描述"),
    ("tags", "标签"),
    ("cover", "封面文件"),
    ("topic", "合集"),
    ("original_claim", "原创"),
    ("account", "账号"),
    ("execute_date", "执行日期"),
    ("publish_at", "定时发布时间"),
    ("status", "状态"),
    ("remote_url", "已发布链接"),
    ("error_message", "错误信息"),
]


def _load_raw_yaml() -> dict[str, Any]:
    """读磁盘 config.yaml 为 dict;**不**展开 ${ENV_VAR}(那是给 load_settings 用的)。"""
    if not _CONFIG_PATH.exists():
        return {}
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _validate_dict(data: dict[str, Any]) -> list[str]:
    """对完整 config dict 跑一次 Settings 校验(含 ${ENV_VAR} 展开)。返回错误列表。"""
    errors: list[str] = []
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    try:
        expanded = _expand_env_vars(text)
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    try:
        Settings.model_validate(yaml.safe_load(expanded))
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(f"{loc}: {err.get('msg', '')}")
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    return errors


def _view_model(data: dict[str, Any]) -> dict[str, Any]:
    """把磁盘 dict 摊平成模板里好取的扁平字段(嵌套用 . 取太啰嗦)。

    敏感字段(app_secret / webhook)如果是 ${ENV_VAR} 引用,原样塞给模板,
    让用户能看出"这是 env 引用";如果是明文,模板渲染时占位 ***(用户改了才会被读)。
    """
    app = data.get("app", {})
    paths = data.get("paths", {})
    sched = data.get("scheduler", {})
    pub = data.get("publisher", {})
    feishu = data.get("feishu", {})
    bitable = feishu.get("bitable", {})
    fm = feishu.get("field_map", {})
    sync = feishu.get("sync", {})
    mon = data.get("monitoring", {})
    wecom = mon.get("notifiers", {}).get("wecom", {})
    webui = data.get("webui", {})

    step_pause = pub.get("step_pause_seconds", [1.0, 3.0]) or [1.0, 3.0]
    return {
        # app
        "app_data_dir": app.get("data_dir", ""),
        "app_logs_dir": app.get("logs_dir", ""),
        "app_timezone": app.get("timezone", "Asia/Shanghai"),
        # paths
        "paths_nas_root": paths.get("nas_root", ""),
        "paths_video_search_root": paths.get("video_search_root", ""),
        "paths_cover_search_root": paths.get("cover_search_root", ""),
        # scheduler
        "sched_hour": sched.get("daily_cron_hour", 9),
        "sched_minute": sched.get("daily_cron_minute", 0),
        "sched_strategy": sched.get("strategy", "round-robin"),
        # publisher
        "pub_headless": bool(pub.get("headless", False)),
        "pub_upload_timeout": pub.get("upload_timeout_seconds", 600),
        "pub_step_pause_min": step_pause[0],
        "pub_step_pause_max": step_pause[1],
        "pub_screenshot_on_error": bool(pub.get("screenshot_on_error", True)),
        "pub_max_concurrent": pub.get("max_concurrent_accounts", 1),
        # feishu
        "feishu_enabled": bool(feishu.get("enabled", True)),
        "feishu_app_id": feishu.get("app_id", ""),
        "feishu_app_secret_display": _display_secret(feishu.get("app_secret", "")),
        "feishu_bitable_app_token": bitable.get("app_token", ""),
        "feishu_bitable_table_id": bitable.get("table_id", ""),
        "feishu_fm": {k: fm.get(k, label) for k, label in FIELD_MAP_KEYS},
        "feishu_sync_writeback": bool(sync.get("write_back_enabled", True)),
        # monitoring
        "mon_cookie_warn_days": mon.get("cookie_warn_days", 1.5),
        "mon_wecom_enabled": bool(wecom.get("enabled", True)),
        "mon_wecom_webhook_display": _display_secret(wecom.get("webhook", "")),
        "notify_on": mon.get("notify_on", []) or [],
        # webui
        "webui_host": webui.get("host", "127.0.0.1"),
        "webui_port": webui.get("port", 8765),
        "webui_open_browser": bool(webui.get("open_browser_on_start", True)),
        # accounts
        "accounts": data.get("accounts", {}) or {},
    }


def _display_secret(value: str) -> str:
    """敏感字段在表单 input 里如何呈现:env 引用原样,明文显示空(让 placeholder 提示用户)。"""
    if not value:
        return ""
    if value.startswith("${") and value.endswith("}"):
        return value
    return ""  # 明文不回显,改了才覆盖


def _merge_secret(submitted: str, fallback: str) -> str:
    """敏感字段提交逻辑:
    - 空字符串 → 用磁盘旧值(用户没改)
    - ${ENV_VAR} → 保留为 env 引用
    - 其它 → 当明文新值
    """
    s = (submitted or "").strip()
    if not s:
        return fallback
    return s


def _render_config(
    request: Request,
    *,
    data: dict[str, Any],
    flash: str | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    vm = _view_model(data)
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "active": "config",
            "abs_path": str(_CONFIG_PATH.resolve()),
            "exists": _CONFIG_PATH.exists(),
            "flash": flash,
            "errors": errors or [],
            "vm": vm,
            "notify_on_options": NOTIFY_ON_OPTIONS,
            "field_map_keys": FIELD_MAP_KEYS,
        },
        status_code=status_code,
    )


@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request, flash: str | None = None) -> HTMLResponse:
    data = _load_raw_yaml()
    return _render_config(request, data=data, flash=flash)


@router.post("/config", response_class=HTMLResponse)
def config_save(
    request: Request,
    # app
    app_data_dir: str = Form(...),
    app_logs_dir: str = Form(...),
    app_timezone: str = Form(...),
    # paths
    paths_nas_root: str = Form(...),
    paths_video_search_root: str = Form(...),
    paths_cover_search_root: str = Form(...),
    # scheduler
    sched_hour: int = Form(...),
    sched_minute: int = Form(...),
    sched_strategy: str = Form(...),
    # publisher
    pub_headless: bool = Form(False),
    pub_upload_timeout: int = Form(...),
    pub_step_pause_min: float = Form(...),
    pub_step_pause_max: float = Form(...),
    pub_screenshot_on_error: bool = Form(False),
    pub_max_concurrent: int = Form(...),
    # feishu(field_map 13 字段)
    feishu_enabled: bool = Form(False),
    feishu_app_id: str = Form(...),
    feishu_app_secret: str = Form(""),
    feishu_bitable_app_token: str = Form(...),
    feishu_bitable_table_id: str = Form(...),
    feishu_fm_video_file: str = Form(...),
    feishu_fm_title: str = Form(...),
    feishu_fm_description: str = Form(...),
    feishu_fm_tags: str = Form(...),
    feishu_fm_cover: str = Form(...),
    feishu_fm_topic: str = Form(...),
    feishu_fm_original_claim: str = Form(...),
    feishu_fm_account: str = Form(...),
    feishu_fm_execute_date: str = Form(...),
    feishu_fm_publish_at: str = Form(...),
    feishu_fm_status: str = Form(...),
    feishu_fm_remote_url: str = Form(...),
    feishu_fm_error_message: str = Form(...),
    feishu_sync_writeback: bool = Form(False),
    # monitoring
    mon_cookie_warn_days: float = Form(...),
    mon_wecom_enabled: bool = Form(False),
    mon_wecom_webhook: str = Form(""),
    notify_on: list[str] = Form(default_factory=list),
    # webui
    webui_host: str = Form(...),
    webui_port: int = Form(...),
    webui_open_browser: bool = Form(False),
) -> HTMLResponse:
    old = _load_raw_yaml()

    # 敏感字段:UI 留空 → 用旧值
    old_feishu = old.get("feishu", {})
    old_wecom = old.get("monitoring", {}).get("notifiers", {}).get("wecom", {})
    app_secret = _merge_secret(feishu_app_secret, old_feishu.get("app_secret", ""))
    wecom_webhook = _merge_secret(mon_wecom_webhook, old_wecom.get("webhook", ""))

    new_data: dict[str, Any] = {
        "app": {
            "data_dir": app_data_dir,
            "logs_dir": app_logs_dir,
            "timezone": app_timezone,
        },
        "paths": {
            "nas_root": paths_nas_root,
            "video_search_root": paths_video_search_root,
            "cover_search_root": paths_cover_search_root,
        },
        # 账号通过单独的 add/delete endpoints 改动,这里直接保留磁盘上现有的
        "accounts": old.get("accounts", {}),
        "scheduler": {
            "daily_cron_hour": sched_hour,
            "daily_cron_minute": sched_minute,
            "strategy": sched_strategy,
        },
        "publisher": {
            "headless": pub_headless,
            "upload_timeout_seconds": pub_upload_timeout,
            "step_pause_seconds": [pub_step_pause_min, pub_step_pause_max],
            "screenshot_on_error": pub_screenshot_on_error,
            "max_concurrent_accounts": pub_max_concurrent,
        },
        "feishu": {
            "enabled": feishu_enabled,
            "app_id": feishu_app_id,
            "app_secret": app_secret,
            "bitable": {
                "app_token": feishu_bitable_app_token,
                "table_id": feishu_bitable_table_id,
            },
            "field_map": {
                "video_file": feishu_fm_video_file,
                "title": feishu_fm_title,
                "description": feishu_fm_description,
                "tags": feishu_fm_tags,
                "cover": feishu_fm_cover,
                "topic": feishu_fm_topic,
                "original_claim": feishu_fm_original_claim,
                "account": feishu_fm_account,
                "execute_date": feishu_fm_execute_date,
                "publish_at": feishu_fm_publish_at,
                "status": feishu_fm_status,
                "remote_url": feishu_fm_remote_url,
                "error_message": feishu_fm_error_message,
            },
            "sync": {"write_back_enabled": feishu_sync_writeback},
        },
        "monitoring": {
            "cookie_warn_days": mon_cookie_warn_days,
            "notifiers": {
                "wecom": {"enabled": mon_wecom_enabled, "webhook": wecom_webhook},
            },
            "notify_on": notify_on,
        },
        "webui": {
            "host": webui_host,
            "port": webui_port,
            "open_browser_on_start": webui_open_browser,
        },
    }

    errors = _validate_dict(new_data)
    if errors:
        return _render_config(request, data=new_data, errors=errors, status_code=400)

    if _CONFIG_PATH.exists():
        shutil.copy2(_CONFIG_PATH, _BACKUP_PATH)
    text = yaml.safe_dump(new_data, sort_keys=False, allow_unicode=True)
    _atomic_write(_CONFIG_PATH, text)
    return _render_config(request, data=new_data, flash=f"已保存,旧版本备份在 {_BACKUP_PATH}")


# ---------- 账号 CRUD(独立小路由,避免账号编辑塞进大表单)----------


@router.post("/config/accounts/add")
def add_account(
    account_id: str = Form(...),
    display_name: str = Form(...),
    daily_limit: int = Form(20),
    user_data_dir: str = Form(...),
    enabled: bool = Form(True),
) -> RedirectResponse:
    data = _load_raw_yaml()
    accounts = data.setdefault("accounts", {}) or {}
    if account_id in accounts:
        return RedirectResponse(f"/config?flash=账号 {account_id} 已存在,未添加", status_code=303)
    accounts[account_id] = {
        "display_name": display_name,
        "enabled": enabled,
        "daily_limit": daily_limit,
        "user_data_dir": user_data_dir,
    }
    data["accounts"] = accounts
    errors = _validate_dict(data)
    if errors:
        # 不写盘,把错误带回 /config(简单起见用 flash;表单细节用户重输)
        return RedirectResponse(f"/config?flash=新增失败: {errors[0]}", status_code=303)
    if _CONFIG_PATH.exists():
        shutil.copy2(_CONFIG_PATH, _BACKUP_PATH)
    _atomic_write(_CONFIG_PATH, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return RedirectResponse(f"/config?flash=已添加账号 {account_id}", status_code=303)


@router.post("/config/accounts/{account_id}/delete")
def delete_account(account_id: str) -> RedirectResponse:
    data = _load_raw_yaml()
    accounts = data.get("accounts", {}) or {}
    if account_id not in accounts:
        return RedirectResponse(f"/config?flash=账号 {account_id} 不存在,未删除", status_code=303)
    accounts.pop(account_id)
    data["accounts"] = accounts
    errors = _validate_dict(data)
    if errors:
        return RedirectResponse(f"/config?flash=删除失败: {errors[0]}", status_code=303)
    if _CONFIG_PATH.exists():
        shutil.copy2(_CONFIG_PATH, _BACKUP_PATH)
    _atomic_write(_CONFIG_PATH, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return RedirectResponse(f"/config?flash=已删除账号 {account_id}", status_code=303)
