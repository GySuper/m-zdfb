"""POST /api/v2/session/init 调用,封装签名 + 错误翻译。

私有模块,接入方走 ApcClient,不直接调这里。
"""

from __future__ import annotations

import json
import time

import httpx

from apc_sdk._types import ApcConfig
from apc_sdk.crypto import hmac_sign
from apc_sdk.exceptions import ApcDenied, ApcNetworkError

_PATH = "/api/v2/session/init"


def fetch_session(client: httpx.Client, cfg: ApcConfig, *, device_id: str | None) -> str:
    """调 APC,返回 license JWT 字符串。

    Args:
        client: httpx.Client(可能带 pinning transport)。
        cfg: ApcConfig。
        device_id: 本地已有的 device_id;None 表示首次签发。

    Returns:
        license JWT 字符串(未校验签名,留给 crypto.verify_jwt)。

    Raises:
        ApcDenied: 4xx 响应。
        ApcNetworkError: 5xx / 网络异常 / 响应缺 license 字段。
    """
    body_dict = {"device_id": device_id, "client_meta": dict(cfg.client_meta)}
    body_str = json.dumps(body_dict, separators=(",", ":"))
    body_bytes = body_str.encode("utf-8")
    ts = str(int(time.time()))
    sig = hmac_sign(cfg.app_secret, "POST", _PATH, ts, body_str)

    url = cfg.endpoint.rstrip("/") + _PATH
    headers = {
        "Content-Type": "application/json",
        "X-Client-Id": cfg.app_id,
        "X-T": ts,
        "X-Sig": sig,
    }

    try:
        response = client.post(
            url, content=body_bytes, headers=headers, timeout=cfg.request_timeout_seconds
        )
    except httpx.HTTPError as exc:
        raise ApcNetworkError(f"APC 请求失败: {exc!r}") from exc

    if 400 <= response.status_code < 500:
        raise ApcDenied(f"APC 拒绝(HTTP {response.status_code}): {response.text[:200]}")
    if response.status_code >= 500:
        raise ApcNetworkError(f"APC 服务端错(HTTP {response.status_code})")

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ApcNetworkError(f"APC 响应非 JSON: {exc}") from exc

    # APC 实际响应:{"ok": true, "data": {"token": "<JWT>", "device_id": "...", ...}}
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ApcNetworkError(f"APC 响应缺 data 对象: {payload!r}")
    license_jwt = data.get("token")
    if not isinstance(license_jwt, str) or not license_jwt:
        raise ApcNetworkError(f"APC 响应 data 里缺 token 字段: {payload!r}")
    return license_jwt
