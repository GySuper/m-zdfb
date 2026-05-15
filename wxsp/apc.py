"""wxsp 私有 APC 粘合层。封装 ApcClient 实例化 + dev-mode 旁路。

dev-mode(`is_packaged() = False`)永远放行,不触网。
打包模式 fail-open:SDK 内部任何 raise 都视作"放行 + log warning",
避免 SDK bug 把整个 wxsp 干瘫。
"""

from __future__ import annotations

from loguru import logger

import wxsp.config as _wxsp_config
from apc_sdk import ApcClient, ApcConfig, Verdict
from wxsp.apc_config import (
    APC_APP_ID,
    APC_APP_SECRET,
    APC_CERT_FP,
    APC_ENDPOINT,
    APC_PUBLIC_KEY,
)

_client_singleton: ApcClient | None = None


def is_dev_mode() -> bool:
    """开发模式 = 未打包。打包后强制走 APC。"""
    return not _wxsp_config.is_packaged()


def _client() -> ApcClient:
    """惰性单例。dev-mode 不应调到这里(check_pass 短路了)。"""
    global _client_singleton
    if _client_singleton is None:
        cache_dir = _wxsp_config.get_user_data_dir() / ".apc"
        cache_dir.mkdir(parents=True, exist_ok=True)
        _client_singleton = ApcClient(
            ApcConfig(
                endpoint=APC_ENDPOINT,
                app_id=APC_APP_ID,
                app_secret=APC_APP_SECRET,
                public_key=APC_PUBLIC_KEY,
                cache_dir=cache_dir,
                cert_fingerprint=APC_CERT_FP or None,
                grace_days=7,
                request_timeout_seconds=5.0,
            )
        )
    return _client_singleton


def check_pass() -> bool:
    """业务侧调这个。返回 True = 允许跑;False = 装故障。

    dev-mode 永远 True(开发跑 uv run wxsp 不触网)。
    打包模式下 SDK 内部 raise 时 fail-open(避免内部 bug 干瘫 wxsp)。
    """
    if is_dev_mode():
        return True
    try:
        verdict = _client().check()
        return bool(verdict == Verdict.PASS)
    except Exception as exc:
        logger.warning(f"[apc] check 异常,fail-open: {exc!r}")
        return True
