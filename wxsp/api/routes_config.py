"""配置:UI 表单编辑 config.yaml。

按 section 拆成一组组表单字段(input / checkbox / select),提交后:
1. 把表单字段拼成完整 dict(嵌套对齐 Settings 模型)
2. 敏感字段(app_secret / webhook 等)若 UI 提交空 → 自动从磁盘旧文件回填(用户没动的不被吹掉)
3. 飞书 / 企微的 ${ENV_VAR} 形式引用允许保留,只要展开后非空
4. Pydantic Settings 完整校验
5. 任一步失败 → 不写盘 + 红色错误展示;通过 → cp config.yaml.bak + 原子写
6. config.yaml 每个请求都重读,改完即时生效,无需重启

账号:CRUD 走独立子路由(add / edit / delete),user_data_dir 由 add 时按 ID 自动生成
为 ./data/chrome-profiles/<account_id>,不暴露给用户。
"""

from __future__ import annotations

import os
import secrets
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger
from pydantic import ValidationError
from sqlmodel import Session

from wxsp.api.deps import get_session, templates
from wxsp.config import Settings, _expand_env_vars, get_config_path
from wxsp.config import platform_label as _platform_label
from wxsp.models import Account

router = APIRouter()
_config_lock = threading.Lock()


def _config_path(platform: str = "tencent_channel") -> Path:
    return get_config_path(platform)


def _backup_path(platform: str = "tencent_channel") -> Path:
    return get_config_path(platform).with_suffix(".yaml.bak")


NOTIFY_ON_OPTIONS: list[tuple[str, str]] = [
    ("cookie_expired", "Cookie 失效"),
    ("cookie_warning", "Cookie 即将过期"),
    ("risk_control", "风控触发"),
    ("task_failed", "任务失败"),
    ("element_not_found", "元素未找到(可能改版)"),
    ("nas_unreachable", "NAS 不可达"),
    ("backlog_high", "历史积压超阈值"),
    ("run_summary", "跑完汇总(每次跑完推一条 ✓N ✗M)"),
]

_SHARED_FIELD_MAP_KEYS = [
    ("video_file", "视频文件"),
    ("title", "标题"),
    ("description", "描述"),
    ("account", "账号"),
    ("execute_date", "执行日期"),
    ("publish_at", "定时发布时间"),
    ("status", "状态"),
    ("remote_url", "已发布链接"),
    ("error_message", "错误信息"),
]

_PLATFORM_FIELD_MAP_KEYS: dict[str, list[tuple[str, str]]] = {
    "tencent_channel": [
        ("tags", "标签"),
        ("cover", "封面文件"),
        ("topic", "合集"),
        ("original_claim", "原创"),
    ],
    "taobao_guanghe": [
        ("topic", "话题活动"),
        ("product_ids", "商品ID"),
        ("declaration", "创作者声明"),
        ("ai_optimize", "AI优化"),
    ],
}


def _field_map_keys(platform: str) -> list[tuple[str, str]]:
    extra = _PLATFORM_FIELD_MAP_KEYS.get(platform, [])
    return _SHARED_FIELD_MAP_KEYS + extra


def _profile_dir_for(account_id: str) -> str:
    """自动生成的 Chrome profile 目录;用户不可改。"""
    return f"./data/chrome-profiles/{account_id}"


def _generate_account_id(existing: dict[str, Any]) -> str:
    """生成 account_<8 hex> 形式的 ID,与 existing 不冲突。

    8 hex = 32-bit,在百级账号规模下碰撞概率 ~0;循环兜底防御未来扩展。
    """
    for _ in range(100):
        candidate = f"account_{secrets.token_hex(4)}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("account_id 生成 100 次都碰撞,几乎不可能;检查 secrets/RNG 是否异常")


def _load_raw_yaml(platform: str = "tencent_channel") -> dict[str, Any]:
    path = _config_path(platform)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _validate_dict(data: dict[str, Any]) -> list[str]:
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


def _save_yaml(data: dict[str, Any], *, platform: str = "tencent_channel") -> None:
    """备份 + 原子写。"""
    cfg = _config_path(platform)
    if cfg.exists():
        shutil.copy2(cfg, _backup_path(platform))
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    _atomic_write(cfg, text)


def _display_secret(value: str) -> str:
    """env 引用原样显示;明文不回显(用户改了才会被读)。"""
    if not value:
        return ""
    if value.startswith("${") and value.endswith("}"):
        return value
    return ""


def _merge_secret(submitted: str, fallback: str) -> str:
    s = (submitted or "").strip()
    return s if s else fallback


def _view_model(data: dict[str, Any], *, platform: str | None = None) -> dict[str, Any]:
    """磁盘 dict → 模板用的扁平字段。

    若 platform 指定,优先从 platforms.<platform> 读取该平台的独立配置;
    未指定或 platform 区块不存在时回退到顶层 flat 字段(后向兼容)。
    """
    app = data.get("app", {})
    paths = data.get("paths", {})
    webui = data.get("webui", {})

    # 平台特定配置:优先读 platforms.<p>,fallback 到顶层
    plat_section: dict[str, Any] | None = None
    if platform:
        plat_section = (data.get("platforms") or {}).get(platform)
    if plat_section is None:
        # backward compat: read from flat fields
        plat_section = {}

    def _plat(key: str, default: Any = None) -> Any:
        return (
            plat_section.get(key, data.get(key, default))
            if plat_section is not None
            else data.get(key, default)
        )

    sched = _plat("scheduler") or {}
    pub = _plat("publisher") or {}
    feishu = _plat("feishu") or {}
    mon = _plat("monitoring") or {}
    bitable = feishu.get("bitable", {}) if isinstance(feishu, dict) else {}
    fm = feishu.get("field_map", {}) if isinstance(feishu, dict) else {}
    sync = feishu.get("sync", {}) if isinstance(feishu, dict) else {}
    wecom = mon.get("notifiers", {}).get("wecom", {}) if isinstance(mon, dict) else {}

    step_pause = pub.get("step_pause_seconds", [1.0, 3.0]) or [1.0, 3.0]
    # path_aliases:键值序对,模板按顺序渲染。排序保证 UI 表单稳定(避免每次刷新顺序乱跳)。
    raw_aliases = paths.get("path_aliases") or {}
    path_aliases_list: list[tuple[str, str]] = sorted(
        (str(k), str(v)) for k, v in raw_aliases.items()
    )
    return {
        # app
        "app_data_dir": app.get("data_dir", ""),
        "app_logs_dir": app.get("logs_dir", ""),
        "app_timezone": app.get("timezone", "Asia/Shanghai"),
        # paths(nas_root + path_aliases)
        "paths_nas_root": paths.get("nas_root", ""),
        "paths_path_aliases": path_aliases_list,
        # scheduler
        "sched_enabled": bool(sched.get("enabled", True)),
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
        "feishu_fm": {
            k: fm.get(k, label) for k, label in _field_map_keys(platform or "tencent_channel")
        },
        "feishu_sync_writeback": bool(sync.get("write_back_enabled", True)),
        # monitoring
        "mon_cookie_warn_days": mon.get("cookie_warn_days", 1.5),
        "mon_wecom_enabled": bool(wecom.get("enabled", True)),
        "mon_wecom_webhook_display": _display_secret(wecom.get("webhook", "")),
        "notify_on": mon.get("notify_on", []) or [],
        # M9 归档 + 积压
        "mon_log_retention_days": mon.get("log_retention_days", 30),
        "mon_screenshot_retention_days": mon.get("screenshot_retention_days", 90),
        "mon_backlog_warn_threshold": mon.get("backlog_warn_threshold", 20),
        # webui
        "webui_host": webui.get("host", "127.0.0.1"),
        "webui_port": webui.get("port", 8765),
        "webui_open_browser": bool(webui.get("open_browser_on_start", True)),
        # accounts(每条多了 video/cover_search_root)
        "accounts": data.get("accounts", {}) or {},
    }


def _render_config(
    request: Request,
    *,
    data: dict[str, Any],
    flash: str | None = None,
    errors: list[str] | None = None,
    edit_account_id: str | None = None,
    platform: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    cfg_path = _config_path(platform or "tencent_channel")
    p_label = _platform_label(platform or "tencent_channel")
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "active": "config",
            "abs_path": str(cfg_path.resolve()),
            "exists": cfg_path.exists(),
            "flash": flash,
            "errors": errors or [],
            "vm": _view_model(data, platform=platform),
            "notify_on_options": NOTIFY_ON_OPTIONS,
            "field_map_keys": _field_map_keys(platform or "tencent_channel"),
            "edit_account_id": edit_account_id,
            "platform_label": p_label,
        },
        status_code=status_code,
    )


# ---------- 主配置(除账号外的所有字段)----------


@router.get("/config", response_class=HTMLResponse)
def config_page(
    request: Request,
    flash: str | None = None,
    edit: str | None = None,
    platform: str | None = None,
) -> HTMLResponse:
    """edit query 参数:传账号 ID 时,该账号行内 inline 展开编辑表单。
    platform 参数:选择要编辑的平台配置(如 tencent_channel / taobao_guanghe)。
    """
    return _render_config(
        request,
        data=_load_raw_yaml(platform or "tencent_channel"),
        flash=flash,
        edit_account_id=edit,
        platform=platform,
    )


@router.post("/config", response_class=HTMLResponse)
def config_save(
    request: Request,
    platform: str = Query("tencent_channel"),
    # app
    app_data_dir: str = Form(...),
    app_logs_dir: str = Form(...),
    app_timezone: str = Form(...),
    # paths(只剩 nas_root)
    paths_nas_root: str = Form(...),
    # scheduler
    sched_enabled: bool = Form(False),
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
    # feishu
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
    mon_log_retention_days: int = Form(30),
    mon_screenshot_retention_days: int = Form(90),
    mon_backlog_warn_threshold: int = Form(20),
    # webui
    webui_host: str = Form(...),
    webui_port: int = Form(...),
    webui_open_browser: bool = Form(False),
) -> HTMLResponse:
    with _config_lock:
        old = _load_raw_yaml(platform)
        old_feishu = old.get("feishu", {})
        old_wecom = old.get("monitoring", {}).get("notifiers", {}).get("wecom", {})
        # legacy 整体 POST 没 path_aliases 字段,保留旧值避免被 nas_root 边吹掉
        old_path_aliases = (old.get("paths") or {}).get("path_aliases") or {}
        paths_section: dict[str, Any] = {"nas_root": paths_nas_root}
        if old_path_aliases:
            paths_section["path_aliases"] = old_path_aliases

        new_data: dict[str, Any] = {
            "app": {
                "data_dir": app_data_dir,
                "logs_dir": app_logs_dir,
                "timezone": app_timezone,
            },
            "paths": paths_section,
            "accounts": old.get("accounts", {}),
            "scheduler": {
                "enabled": sched_enabled,
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
                "app_secret": _merge_secret(feishu_app_secret, old_feishu.get("app_secret", "")),
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
                    "wecom": {
                        "enabled": mon_wecom_enabled,
                        "webhook": _merge_secret(mon_wecom_webhook, old_wecom.get("webhook", "")),
                    },
                },
                "notify_on": notify_on,
                "log_retention_days": mon_log_retention_days,
                "screenshot_retention_days": mon_screenshot_retention_days,
                "backlog_warn_threshold": mon_backlog_warn_threshold,
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

        _save_yaml(new_data, platform=platform)
    return _render_config(
        request, data=new_data, flash=f"已保存,旧版本备份在 {_backup_path(platform)}"
    )


# ---------- 账号 CRUD ----------


def _build_account_entry(
    display_name: str,
    enabled: bool,
    daily_limit: int,
    video_search_root: str,
    cover_search_root: str,
    user_data_dir: str,
    platform: str = "tencent_channel",
) -> dict[str, Any]:
    return {
        "display_name": display_name,
        "enabled": enabled,
        "daily_limit": daily_limit,
        "user_data_dir": user_data_dir,
        "video_search_root": video_search_root,
        "cover_search_root": cover_search_root,
        "platform": platform,
    }


@router.post("/config/accounts/add")
def add_account(
    display_name: str = Form(...),
    daily_limit: int = Form(20),
    video_search_root: str = Form(...),
    cover_search_root: str = Form(...),
    enabled: bool = Form(True),
    platform: str = Form("tencent_channel"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """account_id / user_data_dir 均由系统自动生成(account_<8-hex> + 对应 profile 目录)。

    本地新增成功后,会尝试把 display_name 追加到飞书 Bitable "账号" 单选字段的选项里
    (前提 feishu.enabled=true);失败不阻断本地新增,只在 flash 里追加提示。

    DB Account 行也同步插入(sync.py 的 active_accounts 以 DB 为准,不入库的话
    validator 会判 '不存在或已停用')。
    """
    display_name = display_name.strip()
    if not display_name:
        return RedirectResponse("/config?flash=新增失败:显示名不能为空", status_code=303)
    with _config_lock:
        data = _load_raw_yaml(platform or "tencent_channel")
        accounts = data.setdefault("accounts", {}) or {}
        # 提前查重:display_name 必须唯一(Settings 也会兜底,但那里报错带 pydantic
        # 包装 + 暴露 generated ID,运营看了一头雾水;这里给清爽错误)
        dup_aid = _find_duplicate_display_name(accounts, display_name, exclude_id=None)
        if dup_aid is not None:
            return RedirectResponse(
                f"/config?flash=新增失败:已有账号叫 {display_name!r}(ID={dup_aid}),请改个名",
                status_code=303,
            )
        aid = _generate_account_id(accounts)
        user_data_dir = _profile_dir_for(aid)
        accounts[aid] = _build_account_entry(
            display_name=display_name,
            enabled=enabled,
            daily_limit=daily_limit,
            video_search_root=video_search_root,
            cover_search_root=cover_search_root,
            user_data_dir=user_data_dir,
            platform=platform,
        )
        data["accounts"] = accounts
        errors = _validate_dict(data)
        if errors:
            return RedirectResponse(f"/config?flash=新增失败: {errors[0]}", status_code=303)
        _save_yaml(data, platform=platform)
        if session.get(Account, aid) is None:
            session.add(
                Account(
                    id=aid,
                    display_name=display_name,
                    platform=platform,
                    user_data_dir=user_data_dir,
                    daily_limit=daily_limit,
                )
            )
    suffix = _sync_account_to_feishu(data, display_name)
    return RedirectResponse(f"/config?flash=已添加账号 {aid}{suffix}", status_code=303)


def _find_duplicate_display_name(
    accounts: dict[str, Any], display_name: str, *, exclude_id: str | None
) -> str | None:
    """在已有账号里找跟 display_name 同名的(忽略大小写、忽略首尾空白);
    返回那个 account_id;没找到返 None。
    exclude_id:跳过该 ID(给 update 路由用,避免把自己当成重复)。
    """
    target = display_name.strip().lower()
    for aid, entry in accounts.items():
        if aid == exclude_id:
            continue
        existing = (entry.get("display_name") or "").strip().lower()
        if existing == target:
            return aid
    return None


def _sync_account_to_feishu(data: dict[str, Any], display_name: str) -> str:
    """把 display_name 追加到飞书"账号"单选字段;返回拼到 flash 后面的中文提示。

    flash 后缀(成功 / 已存在 / 跳过 / 失败)由调用方拼到主提示后,让运营一眼看到
    本地保存 + 飞书同步两段结果。
    """
    feishu = data.get("feishu", {}) or {}
    if not feishu.get("enabled"):
        return "(飞书未启用,未同步选项)"
    try:
        from wxsp.config import _expand_env_vars
        from wxsp.feishu import add_account_option, make_client
    except Exception as exc:
        return f"(飞书同步失败: import {exc})"
    bitable = feishu.get("bitable", {}) or {}
    field_map = feishu.get("field_map", {}) or {}
    app_id = (feishu.get("app_id") or "").strip()
    app_secret_raw = (feishu.get("app_secret") or "").strip()
    app_token = (bitable.get("app_token") or "").strip()
    table_id = (bitable.get("table_id") or "").strip()
    account_field_name = (field_map.get("account") or "账号").strip()
    if not (app_id and app_secret_raw and app_token and table_id):
        return "(飞书配置不全,未同步选项)"
    try:
        app_secret = _expand_env_vars(app_secret_raw)
    except ValueError as exc:
        return f"(飞书 app_secret 展开失败: {exc})"
    try:
        client = make_client(app_id, app_secret)
        result = add_account_option(
            client,
            app_token=app_token,
            table_id=table_id,
            account_field_name=account_field_name,
            option_name=display_name,
        )
    except Exception as exc:
        return f"(飞书同步失败: {exc})"
    if result == "exists":
        return f"(飞书 {account_field_name!r} 已含选项 {display_name!r},未重复)"
    return f"(已同步到飞书 {account_field_name!r} 字段)"


@router.post("/config/accounts/{account_id}/update")
def update_account(
    account_id: str,
    display_name: str = Form(...),
    daily_limit: int = Form(...),
    video_search_root: str = Form(...),
    cover_search_root: str = Form(...),
    enabled: bool = Form(False),
    platform: str = Form("tencent_channel"),
) -> RedirectResponse:
    """编辑账号(account_id 和 user_data_dir 不可改:它们与 chrome profile 强绑定)。"""
    display_name = display_name.strip()
    if not display_name:
        return RedirectResponse(
            f"/config?flash=保存失败:显示名不能为空&edit={account_id}", status_code=303
        )
    with _config_lock:
        data = _load_raw_yaml(platform or "tencent_channel")
        accounts = data.get("accounts", {}) or {}
        if account_id not in accounts:
            return RedirectResponse(
                f"/config?flash=账号 {account_id} 不存在,无法编辑", status_code=303
            )
        # 改名时也要查重:不能跟其他账号撞 display_name(自己不算)
        dup_aid = _find_duplicate_display_name(accounts, display_name, exclude_id=account_id)
        if dup_aid is not None:
            return RedirectResponse(
                f"/config?flash=保存失败:显示名 {display_name!r} 已被账号 {dup_aid} 用了"
                f"&edit={account_id}",
                status_code=303,
            )
        old_entry = accounts[account_id]
        accounts[account_id] = _build_account_entry(
            display_name=display_name,
            enabled=enabled,
            daily_limit=daily_limit,
            video_search_root=video_search_root,
            cover_search_root=cover_search_root,
            user_data_dir=old_entry.get("user_data_dir") or _profile_dir_for(account_id),
            platform=platform,
        )
        data["accounts"] = accounts
        errors = _validate_dict(data)
        if errors:
            return RedirectResponse(
                f"/config?flash=保存失败: {errors[0]}&edit={account_id}", status_code=303
            )
        _save_yaml(data, platform=platform)
    return RedirectResponse(f"/config?flash=已更新账号 {account_id}", status_code=303)


@router.post("/config/accounts/{account_id}/delete")
def delete_account(account_id: str, platform: str = "tencent_channel") -> RedirectResponse:
    with _config_lock:
        data = _load_raw_yaml(platform or "tencent_channel")
        accounts = data.get("accounts", {}) or {}
        if account_id not in accounts:
            return RedirectResponse(
                f"/config?flash=账号 {account_id} 不存在,未删除", status_code=303
            )
        accounts.pop(account_id)
        data["accounts"] = accounts
        errors = _validate_dict(data)
        if errors:
            return RedirectResponse(f"/config?flash=删除失败: {errors[0]}", status_code=303)
        _save_yaml(data, platform=platform)
    return RedirectResponse(f"/config?flash=已删除账号 {account_id}", status_code=303)


# ---------- 分区独立保存(2026-05-16 UI 重设计)----------
#
# 每个 section 一个独立 POST 端点,只更新该 section 涉及的 yaml 键。
# 老的 POST /config 端点保留作兼容兜底,新 UI 不再使用。


def _apply_and_save(
    flash_ok: str, apply_fn: Callable[[dict[str, Any]], None], *, platform: str = "tencent_channel"
) -> RedirectResponse:
    """读 config.yaml → apply_fn(data) 改 → 校验 → 写盘,全程持锁,杜绝竞态条件。"""
    with _config_lock:
        data = _load_raw_yaml(platform)
        apply_fn(data)
        errors = _validate_dict(data)
        if errors:
            msg = f"保存失败: {'; '.join(errors)}"
            return RedirectResponse(f"/config?flash={msg}", status_code=303)
        _save_yaml(data, platform=platform)
    return RedirectResponse(f"/config?flash={flash_ok}", status_code=303)


@router.post("/config/section/publisher")
def save_publisher(
    pub_headless: bool = Form(False),
    pub_upload_timeout: int = Form(...),
    pub_step_pause_min: float = Form(...),
    pub_step_pause_max: float = Form(...),
    pub_screenshot_on_error: bool = Form(False),
    pub_max_concurrent: int = Form(...),
) -> RedirectResponse:
    def apply(data: dict[str, Any]) -> None:
        data["publisher"] = {
            "headless": pub_headless,
            "upload_timeout_seconds": pub_upload_timeout,
            "step_pause_seconds": [pub_step_pause_min, pub_step_pause_max],
            "screenshot_on_error": pub_screenshot_on_error,
            "max_concurrent_accounts": pub_max_concurrent,
        }

    return _apply_and_save("✓ 发布规则已保存", apply)


@router.post("/config/section/scheduler")
def save_scheduler(
    sched_enabled: bool = Form(False),
    sched_hour: int = Form(...),
    sched_minute: int = Form(...),
    sched_strategy: str = Form(...),
) -> RedirectResponse:
    def apply(data: dict[str, Any]) -> None:
        data["scheduler"] = {
            "enabled": sched_enabled,
            "daily_cron_hour": sched_hour,
            "daily_cron_minute": sched_minute,
            "strategy": sched_strategy,
        }

    return _apply_and_save("✓ 调度时间已保存", apply)


@router.post("/config/section/feishu")
def save_feishu(
    feishu_enabled: bool = Form(False),
    feishu_app_id: str = Form(...),
    feishu_app_secret: str = Form(""),
    feishu_bitable_app_token: str = Form(...),
    feishu_bitable_table_id: str = Form(...),
    feishu_sync_writeback: bool = Form(False),
    paths_nas_root: str = Form(...),
    # path_aliases:两个平行 list,zip 后过滤空对(运营点了 + 但没填就提交也不会落进 yaml)
    path_alias_key: list[str] = Form(default_factory=list),
    path_alias_value: list[str] = Form(default_factory=list),
) -> RedirectResponse:
    aliases = _collect_path_aliases(path_alias_key, path_alias_value)

    def apply(data: dict[str, Any]) -> None:
        old_feishu = data.get("feishu", {})
        old_field_map = old_feishu.get("field_map", {})
        paths_section: dict[str, Any] = {"nas_root": paths_nas_root}
        if aliases:
            paths_section["path_aliases"] = aliases
        data["paths"] = paths_section
        data["feishu"] = {
            "enabled": feishu_enabled,
            "app_id": feishu_app_id,
            "app_secret": _merge_secret(feishu_app_secret, old_feishu.get("app_secret", "")),
            "bitable": {
                "app_token": feishu_bitable_app_token,
                "table_id": feishu_bitable_table_id,
            },
            "field_map": old_field_map,
            "sync": {"write_back_enabled": feishu_sync_writeback},
        }

    return _apply_and_save("✓ 连接服务已保存", apply)


def _collect_path_aliases(keys: list[str], values: list[str]) -> dict[str, str]:
    """zip 两个平行 list,key/value 任一为空白的行视为"用户没填完"丢弃。
    同 key 多次出现(运营粘了重复)取最后一行,行为与字典字面量一致,不另抛错。
    """
    aliases: dict[str, str] = {}
    for k, v in zip(keys, values, strict=False):
        ks = (k or "").strip()
        vs = (v or "").strip()
        if not ks or not vs:
            continue
        aliases[ks] = vs
    return aliases


@router.post("/config/section/monitoring")
def save_monitoring(
    mon_cookie_warn_days: float = Form(...),
    mon_wecom_enabled: bool = Form(False),
    mon_wecom_webhook: str = Form(""),
    notify_on: list[str] = Form(default_factory=list),
    mon_log_retention_days: int = Form(30),
    mon_screenshot_retention_days: int = Form(90),
    mon_backlog_warn_threshold: int = Form(20),
) -> RedirectResponse:
    def apply(data: dict[str, Any]) -> None:
        old_wecom = data.get("monitoring", {}).get("notifiers", {}).get("wecom", {})
        data["monitoring"] = {
            "cookie_warn_days": mon_cookie_warn_days,
            "notifiers": {
                "wecom": {
                    "enabled": mon_wecom_enabled,
                    "webhook": _merge_secret(mon_wecom_webhook, old_wecom.get("webhook", "")),
                },
            },
            "notify_on": notify_on,
            "log_retention_days": mon_log_retention_days,
            "screenshot_retention_days": mon_screenshot_retention_days,
            "backlog_warn_threshold": mon_backlog_warn_threshold,
        }

    return _apply_and_save("✓ 通知告警已保存", apply)


@router.post("/config/section/system")
def save_system(
    app_data_dir: str = Form(...),
    app_logs_dir: str = Form(...),
    app_timezone: str = Form(...),
    webui_host: str = Form(...),
    webui_port: int = Form(...),
    webui_open_browser: bool = Form(False),
) -> RedirectResponse:
    def apply(data: dict[str, Any]) -> None:
        data["app"] = {
            "data_dir": app_data_dir,
            "logs_dir": app_logs_dir,
            "timezone": app_timezone,
        }
        data["webui"] = {
            "host": webui_host,
            "port": webui_port,
            "open_browser_on_start": webui_open_browser,
        }

    return _apply_and_save("✓ 系统设置已保存", apply)


@router.post("/config/section/fieldmap")
def save_fieldmap(
    feishu_fm_video_file: str = Form(...),
    feishu_fm_title: str = Form(...),
    feishu_fm_description: str = Form(...),
    feishu_fm_tags: str = Form(""),
    feishu_fm_cover: str = Form(""),
    feishu_fm_topic: str = Form(""),
    feishu_fm_original_claim: str = Form(""),
    feishu_fm_declaration: str = Form(""),
    feishu_fm_ai_optimize: str = Form(""),
    feishu_fm_product_ids: str = Form(""),
    feishu_fm_account: str = Form(...),
    feishu_fm_execute_date: str = Form(...),
    feishu_fm_publish_at: str = Form(...),
    feishu_fm_status: str = Form(...),
    feishu_fm_remote_url: str = Form(...),
    feishu_fm_error_message: str = Form(...),
) -> RedirectResponse:
    def apply(data: dict[str, Any]) -> None:
        feishu = data.setdefault("feishu", {})
        feishu["field_map"] = {
            "video_file": feishu_fm_video_file,
            "title": feishu_fm_title,
            "description": feishu_fm_description,
            "tags": feishu_fm_tags,
            "cover": feishu_fm_cover,
            "topic": feishu_fm_topic,
            "original_claim": feishu_fm_original_claim,
            "declaration": feishu_fm_declaration,
            "ai_optimize": feishu_fm_ai_optimize,
            "product_ids": feishu_fm_product_ids,
            "account": feishu_fm_account,
            "execute_date": feishu_fm_execute_date,
            "publish_at": feishu_fm_publish_at,
            "status": feishu_fm_status,
            "remote_url": feishu_fm_remote_url,
            "error_message": feishu_fm_error_message,
        }

    return _apply_and_save("✓ 字段映射已保存", apply)


# ---------- 通知告警:测试推送 ----------


@router.post("/config/test-wecom", response_class=HTMLResponse)
def test_wecom(mon_wecom_webhook: str = Form("")) -> HTMLResponse:
    """企微测试推送:表单 webhook 为空 → 用 config.yaml 已存的;
    支持 ${ENV_VAR} 引用展开;真发一条 markdown 给 webhook;结果以片段返回,inline 展示。
    """
    from wxsp.notify import NotifyEvent, WecomNotifier

    submitted = (mon_wecom_webhook or "").strip()
    if not submitted:
        # 用户没改密码框 → 用磁盘里已存的(可能是 ${ENV} 或明文)
        disk_data = _load_raw_yaml()
        submitted = (
            disk_data.get("monitoring", {}).get("notifiers", {}).get("wecom", {}).get("webhook", "")
            or ""
        ).strip()

    if not submitted:
        return HTMLResponse(_flash_fragment("warn", "webhook 为空,无法测试"))

    try:
        url = _expand_env_vars(submitted)
    except ValueError as exc:
        return HTMLResponse(_flash_fragment("error", f"环境变量展开失败:{exc}"))

    if not url.startswith("https://"):
        return HTMLResponse(_flash_fragment("error", "webhook URL 必须以 https:// 开头"))

    notifier = WecomNotifier(webhook=url)
    try:
        ok = notifier.send(
            NotifyEvent(
                type="config_test",
                level="info",
                title="自动发布平台 · 测试推送",
                content="如果你看到这条消息,说明机器人地址配置正确。",
            )
        )
    except Exception as exc:
        logger.exception(f"[web/test-wecom] {exc}")
        return HTMLResponse(_flash_fragment("error", f"调用失败:{exc}"))
    if ok:
        return HTMLResponse(_flash_fragment("ok", "✓ 已发送,请到企微群查看"))
    return HTMLResponse(_flash_fragment("error", "推送失败,看后端日志(errcode/网络)"))


def _flash_fragment(level: str, text: str) -> str:
    """HTMX inline flash 片段。level: ok | warn | error。"""
    safe = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    return f'<div class="flash {level}">{safe}</div>'
