"""Pydantic Settings + YAML/ENV 加载(M0)。"""

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


def is_packaged() -> bool:
    """判断当前是否运行在打包产物里。

    覆盖三种打包形态:
    - PyInstaller bundle:`sys.frozen = True`
    - Nuitka --standalone(整个 app 编译):`sys.modules['__main__'].__compiled__`
    - Nuitka --module(只编 wxsp 包成 .so,PyInstaller 包外壳):`wxsp.__compiled__`

    WXSP_DEV_MODE=1 强制走开发模式(用于在打包产物里本地调试)。
    """
    if os.environ.get("WXSP_DEV_MODE") == "1":
        return False
    if getattr(sys, "frozen", False):
        return True
    wxsp_pkg = sys.modules.get("wxsp")
    if wxsp_pkg is not None and hasattr(wxsp_pkg, "__compiled__"):
        return True
    return hasattr(sys.modules.get("__main__"), "__compiled__")


def get_user_data_dir() -> Path:
    """返回 data/ 目录绝对路径。

    打包模式:平台规范位置 / wxsp / data
        mac: ~/Library/Application Support/wxsp/data
        win: %APPDATA%\\wxsp\\data
    开发模式:项目根 ./data
    """
    if is_packaged():
        return Path(_platform_user_data_dir("wxsp")) / "data"
    return Path("./data").resolve()


def get_user_logs_dir() -> Path:
    """返回 logs/ 目录绝对路径。打包模式走 platformdirs,开发模式 ./logs。"""
    if is_packaged():
        return Path(_platform_user_log_dir("wxsp"))
    return Path("./logs").resolve()


def get_config_path() -> Path:
    """返回 config.yaml 的绝对路径。

    打包模式:user_data_dir("wxsp")/config.yaml(与 data/ 同级)
    开发模式:./config.yaml
    """
    if is_packaged():
        return Path(_platform_user_data_dir("wxsp")) / "config.yaml"
    return Path("./config.yaml").resolve()


class AppConfig(BaseModel):
    data_dir: Path
    logs_dir: Path
    timezone: str


class PathsConfig(BaseModel):
    """全局只配 NAS 挂载根目录;每个账号自己配 video/cover 检索路径(在 AccountConfig)。"""

    nas_root: Path


class AccountConfig(BaseModel):
    display_name: str
    enabled: bool = True
    daily_limit: int
    user_data_dir: Path
    # 视频/封面检索路径(支持 {nas_root} 占位,会在 Settings.after 钩子里展开)
    video_search_root: Path
    cover_search_root: Path


class SchedulerConfig(BaseModel):
    enabled: bool = True  # false 时 daemon 仍启动,但不注册 09:00 cron(手动入口仍可用)
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
    # M9 归档保留期(spec §6.3)
    log_retention_days: int = 30
    screenshot_retention_days: int = 90
    # M9 积压告警阈值(spec §5.6),> 此数推一条 backlog_high 告警
    backlog_warn_threshold: int = 20


class WebUIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser_on_start: bool = True


class Settings(BaseModel):
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
        """把账号下 video_search_root / cover_search_root 里的 {nas_root} 占位展开。"""
        nas_root_str = str(self.paths.nas_root)
        for ac in self.accounts.values():
            for field in ("video_search_root", "cover_search_root"):
                current = str(getattr(ac, field))
                if "{nas_root}" in current:
                    expanded = current.replace("{nas_root}", nas_root_str)
                    setattr(ac, field, Path(expanded))
        return self


_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env_vars(text: str) -> str:
    """Replace ${VAR} with os.environ[VAR]; raise ValueError if missing."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"环境变量 {name} 未设置,无法展开 config.yaml 中的 ${{{name}}}")
        return os.environ[name]

    return _ENV_PATTERN.sub(replace, text)


def load_settings(config_path: Path | None = None) -> Settings:
    """Load and validate config.yaml; expand ${ENV_VAR} and {nas_root}."""
    if config_path is None:
        config_path = get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件: {config_path}")
    raw = config_path.read_text(encoding="utf-8")
    expanded = _expand_env_vars(raw)
    data: dict[str, Any] = yaml.safe_load(expanded)
    return Settings.model_validate(data)
