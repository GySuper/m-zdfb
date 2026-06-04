"""HMAC 签名(本文件 Task 3 会追加 JWT 校验)。"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import jwt as pyjwt

from apc_sdk.exceptions import ApcDenied

# 客户端时钟与签发方(后端)之间允许的偏差。license 是签发即用、token 不带 nbf,
# 后端用自身时间签 iat;客户端时钟哪怕慢 1 秒,iat 就落在客户端"未来",pyjwt 默认
# 零容忍会判"token 未生效(iat)"→ 误拒。给 exp/nbf 留 leeway,并关掉无意义的 iat
# 未来校验(签名 + exp 过期仍严格,安全性不变)。
CLOCK_SKEW_LEEWAY_SECONDS = 120


def hmac_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    """对齐 sdk-integration.md §2.3 的签名算法。返回 hex 小写字符串。

    canonical = method + "\\n" + path + "\\n" + ts + "\\n" + sha256(body).hex()
    signature = hmac_sha256(secret, canonical)
    """
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical = f"{method}\n{path}\n{ts}\n{body_sha}"
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_jwt(
    token: str,
    *,
    public_key: str,
    audience: str,
    expected_did: str | None = None,
) -> dict[str, Any]:
    """RS256 校验 + 必要 claim 检查。失败一律抛 ApcDenied。

    APC 服务端把 app_id 放在 `sub` claim 里,不用 `aud`。这里参数仍叫
    `audience` 是因为语义上"指定这个 token 是给谁用的"——历史命名,值就是 app_id。

    Args:
        token: 服务端返回的 license JWT。
        public_key: PEM 公钥(含 BEGIN/END 头尾)。
        audience: 期望 sub(= app_id);不等抛 ApcDenied。
        expected_did: 期望 did(本地已有 device_id);为 None 时跳过(首次签发场景)。

    Returns:
        token claims dict。

    Raises:
        ApcDenied: 任何校验失败(过期、签名错、sub 错、did 错)。
    """
    try:
        claims = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            options={"require": ["exp", "sub"], "verify_iat": False},
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise ApcDenied(f"JWT expired: {exc}") from exc
    except pyjwt.InvalidSignatureError as exc:
        raise ApcDenied(f"JWT signature invalid: {exc}") from exc
    except pyjwt.PyJWTError as exc:
        raise ApcDenied(f"JWT decode failed: {exc}") from exc

    actual_sub = claims.get("sub")
    if actual_sub != audience:
        raise ApcDenied(f"JWT subject mismatch: token sub={actual_sub!r}, expected={audience!r}")

    if expected_did is not None:
        actual_did = claims.get("did")
        if actual_did != expected_did:
            raise ApcDenied(
                f"JWT did mismatch: token did={actual_did!r}, expected did={expected_did!r}"
            )

    return claims
