"""HMAC 签名(本文件 Task 3 会追加 JWT 校验)。"""

from __future__ import annotations

import hashlib
import hmac


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
