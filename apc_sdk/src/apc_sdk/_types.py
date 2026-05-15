"""SDK 类型 + Verdict + ApcConfig。故意不引入 Pydantic 以减少接入方冲突。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypedDict


class Verdict(str, Enum):
    PASS = "pass"
    DENY = "deny"


@dataclass(frozen=True)
class ApcConfig:
    """ApcClient 构造参数。frozen=True 避免接入方实例化后误改字段。"""

    endpoint: str  # "https://203.0.113.5:8443" 末尾不带 /
    app_id: str
    app_secret: str
    public_key: str  # JWT 校验用 PEM 公钥(含 BEGIN/END 头尾)
    cache_dir: Path
    cert_fingerprint: str | None = None  # 自签时填(小写 hex,无冒号)
    grace_days: int = 7
    request_timeout_seconds: float = 5.0
    client_meta: dict[str, str] = field(default_factory=dict)


class SessionCache(TypedDict, total=False):
    """session.json 内部结构。total=False 让旧版本缓存少字段也能 load。"""

    schema_version: int
    device_id: str
    last_success_at: str  # ISO 8601 UTC
    today_date: str  # YYYY-MM-DD 本地时区
    today_verdict: str  # "pass" | "deny"
    license_jwt: str
