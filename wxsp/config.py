"""Pydantic Settings + YAML/ENV 加载(M0)。

每平台一个独立配置文件: config_tencent_channel.yaml / config_taobao_guanghe.yaml。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_data_dir as _platform_user_data_dir
from platformdirs import user_log_dir as _platform_user_log_dir
from pydantic import BaseModel, Field, model_validator

from wxsp.platform_meta import REGISTRY


def is_packaged() -> bool:
    if os.environ.get("WXSP_DEV_MODE") == "1":
        return False
    if getattr(sys, "frozen", False):
        return True
    wxsp_pkg = sys.modules.get("wxsp")
    if wxsp_pkg is not None and hasattr(wxsp_pkg, "__compiled__"):
        return True
    return hasattr(sys.modules.get("__main__"), "__compiled__")


def get_user_data_dir() -> Path:
    if is_packaged():
        return Path(_platform_user_data_dir("wxsp")) / "data"
    return Path("./data").resolve()


def get_user_logs_dir() -> Path:
    if is_packaged():
        return Path(_platform_user_log_dir("wxsp"))
    return Path("./logs").resolve()


# 已知平台:中文名 / 登录检测 / 标题下限等单一信息源见 wxsp/platform_meta.py。
# 本模块只从 REGISTRY 派生平台 key 列表,不再各存一份平台表。
ALL_PLATFORMS = list(REGISTRY)


# ---------------------------------------------------------------------------
# global default platform (shared across all platform configs)
# ---------------------------------------------------------------------------


def _default_platform_path() -> Path:
    """Shared file for the global default_platform setting."""
    if is_packaged():
        return Path(_platform_user_data_dir("wxsp")) / "default_platform"
    return Path("./data/default_platform").resolve()


def get_default_platform() -> str:
    """Read the global default platform from the shared file.
    Falls back to the first configured platform, then to tencent_channel.
    """
    path = _default_platform_path()
    if path.exists():
        val = path.read_text(encoding="utf-8").strip()
        if val in REGISTRY:
            return val
    # Fallback: first platform with a config file
    for p in ALL_PLATFORMS:
        if get_config_path(p).exists():
            return p
    return "tencent_channel"


def set_default_platform(platform: str) -> None:
    """Persist the global default platform setting."""
    path = _default_platform_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(platform, encoding="utf-8")


def platform_label(key: str) -> str:
    meta = REGISTRY.get(key)
    return meta.label if meta is not None else key


def get_config_path(platform: str = "tencent_channel") -> Path:
    """返回 config_{platform}.yaml 的绝对路径。

    向后兼容: 如果旧版单文件 config.yaml 存在且目标平台文件不存在, 自动迁移。
    """
    if is_packaged():
        base = Path(_platform_user_data_dir("wxsp"))
    else:
        base = Path(".").resolve()

    new_path = base / f"config_{platform}.yaml"
    if new_path.exists():
        return new_path

    # 向后兼容: 从旧 config.yaml 迁移
    old_path = base / "config.yaml"
    if old_path.exists():
        _migrate_old_config(old_path, base)
        if new_path.exists():
            return new_path

    return new_path


def _migrate_old_config(old_path: Path, base: Path) -> None:
    """将旧版单文件 config.yaml 拆分为每平台独立配置文件。"""
    import shutil

    raw = old_path.read_text(encoding="utf-8")
    data: dict[str, Any] = yaml.safe_load(raw)

    # 如果已有 platforms.* 嵌套结构, 每个平台写一份
    platforms_data = data.get("platforms")
    if platforms_data and isinstance(platforms_data, dict):
        for pkey, pdata in platforms_data.items():
            new_data: dict[str, Any] = {
                "app": data.get("app", {}),
                "paths": data.get("paths", {}),
                "webui": data.get("webui", {}),
                "scheduler": pdata.get("scheduler", {}),
                "publisher": pdata.get("publisher", {}),
            }
            if pdata.get("feishu"):
                new_data["feishu"] = pdata["feishu"]
            if pdata.get("monitoring"):
                new_data["monitoring"] = pdata["monitoring"]
            # 只保留该平台的账号
            new_accounts = {}
            for aid, ac in data.get("accounts", {}).items():
                if isinstance(ac, dict) and ac.get("platform", "tencent_channel") == pkey:
                    new_accounts[aid] = ac
            new_data["accounts"] = new_accounts
            new_path = base / f"config_{pkey}.yaml"
            new_path.write_text(
                yaml.dump(new_data, allow_unicode=True, default_flow_style=False), encoding="utf-8"
            )
        # 备份旧文件
        bak = old_path.with_suffix(".yaml.bak")
        shutil.move(str(old_path), str(bak))
        return

    # 旧格式(无 platforms 嵌套): 整个作为 tencent_channel 的配置
    new_path = base / "config_tencent_channel.yaml"
    shutil.copy(str(old_path), str(new_path))
    bak = old_path.with_suffix(".yaml.bak")
    shutil.move(str(old_path), str(bak))


# ---------------------------------------------------------------------------
# Config models (flat, no platforms nesting)
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    data_dir: Path
    logs_dir: Path
    timezone: str


class PathsConfig(BaseModel):
    nas_root: Path
    path_aliases: dict[str, str] = Field(default_factory=dict)


class AccountConfig(BaseModel):
    display_name: str
    platform: str = "tencent_channel"
    enabled: bool = True
    daily_limit: int
    user_data_dir: Path
    video_search_root: Path
    cover_search_root: Path


class SchedulerConfig(BaseModel):
    enabled: bool = True
    daily_cron_hour: int = 9
    daily_cron_minute: int = 0
    strategy: str = "round-robin"


class PublisherConfig(BaseModel):
    headless: bool = False
    upload_timeout_seconds: int = 600
    step_pause_seconds: tuple[float, float] = (1.0, 3.0)
    screenshot_on_error: bool = True
    max_concurrent_accounts: int = 1


class FeishuFieldMap(BaseModel):
    video_file: str = "视频文件"
    title: str = "标题"
    description: str = "描述"
    tags: str = "标签"
    cover: str = "封面文件"
    topic: str = "合集"
    original_claim: str = "原创"
    account: str = "账号"
    execute_date: str = "执行日期"
    publish_at: str = "定时发布时间"
    status: str = "状态"
    remote_url: str = "已发布链接"
    error_message: str = "错误信息"
    # taobao-specific
    declaration: str = "创作者声明"
    ai_optimize: str = "AI优化"
    product_ids: str = "商品ID"


class FeishuBitableConfig(BaseModel):
    app_token: str
    table_id: str


class FeishuSyncConfig(BaseModel):
    write_back_enabled: bool = True


class FeishuConfig(BaseModel):
    enabled: bool = True
    app_id: str
    app_secret: str
    bitable: FeishuBitableConfig
    field_map: FeishuFieldMap = Field(default_factory=FeishuFieldMap)
    sync: FeishuSyncConfig = Field(default_factory=FeishuSyncConfig)


class WecomNotifierConfig(BaseModel):
    enabled: bool = True
    webhook: str


class NotifiersConfig(BaseModel):
    wecom: WecomNotifierConfig


class MonitoringConfig(BaseModel):
    cookie_warn_days: float = 1.5
    notifiers: NotifiersConfig
    notify_on: list[str] = Field(default_factory=list)
    log_retention_days: int = 30
    screenshot_retention_days: int = 90
    backlog_warn_threshold: int = 20


class WebUIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser_on_start: bool = True


class Settings(BaseModel):
    """每个 config_{platform}.yaml 的完整结构。"""

    app: AppConfig
    paths: PathsConfig
    accounts: dict[str, AccountConfig]
    scheduler: SchedulerConfig
    publisher: PublisherConfig
    feishu: FeishuConfig
    monitoring: MonitoringConfig
    webui: WebUIConfig

    @model_validator(mode="after")
    def _expand_nas_root_template(self) -> Settings:
        nas_root_str = str(self.paths.nas_root)
        for ac in self.accounts.values():
            for field in ("video_search_root", "cover_search_root"):
                current = str(getattr(ac, field))
                if "{nas_root}" in current:
                    expanded = current.replace("{nas_root}", nas_root_str)
                    setattr(ac, field, Path(expanded))
        return self

    @model_validator(mode="after")
    def _check_display_name_uniqueness(self) -> Settings:
        seen: dict[str, str] = {}
        for aid, ac in self.accounts.items():
            if ac.display_name in seen:
                raise ValueError(
                    f"accounts: display_name {ac.display_name!r} "
                    f"被 {seen[ac.display_name]!r} 和 {aid!r} 重复使用,必须唯一"
                )
            seen[ac.display_name] = aid
        return self


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env_vars(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"环境变量 {name} 未设置,无法展开 config.yaml 中的 ${{{name}}}")
        return os.environ[name]

    return _ENV_PATTERN.sub(replace, text)


def load_settings(
    config_path: Path | None = None,
    *,
    platform: str = "tencent_channel",
) -> Settings:
    """加载 config_{platform}.yaml, 展开 ${ENV_VAR} 和 {nas_root}。

    支持旧的 load_settings(config_path=Path(...)) 调用方式向后兼容。
    """
    if config_path is not None:
        # 旧式调用: 直接传文件路径
        if not config_path.exists():
            raise FileNotFoundError(f"找不到配置文件: {config_path}")
        raw = config_path.read_text(encoding="utf-8")
        expanded = _expand_env_vars(raw)
        cfg_data: dict[str, Any] = yaml.safe_load(expanded)
        return Settings.model_validate(cfg_data)

    path = get_config_path(platform)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到配置文件: {path}\n"
            f"请创建 config_{platform}.yaml (可参考 config.example.yaml)"
        )
    raw2 = path.read_text(encoding="utf-8")
    expanded2 = _expand_env_vars(raw2)
    platform_data: dict[str, Any] = yaml.safe_load(expanded2)
    return Settings.model_validate(platform_data)
