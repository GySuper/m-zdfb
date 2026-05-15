"""HMAC 签名向量测试。预期值对齐 sdk-integration.md §2.3 的 Node 示例算法:
canonical = `${method}\n${path}\n${ts}\n${bodySha}`
bodySha   = sha256(body, utf8).hex()
signature = hmac_sha256(secret, canonical).hex()
"""

import hashlib
import hmac


def _reference_sign(secret: str, method: str, path: str, ts: str, body: str) -> str:
    """独立参考实现,跟被测函数对算。"""
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical = f"{method}\n{path}\n{ts}\n{body_sha}"
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def test_hmac_sign_matches_reference_empty_body():
    from apc_sdk.crypto import hmac_sign

    expected = _reference_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", "")
    assert hmac_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", "") == expected


def test_hmac_sign_matches_reference_with_body():
    from apc_sdk.crypto import hmac_sign

    body = '{"device_id":"abc","client_meta":{}}'
    expected = _reference_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", body)
    assert hmac_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", body) == expected


def test_hmac_sign_different_secret_different_result():
    from apc_sdk.crypto import hmac_sign

    a = hmac_sign("secret_a", "POST", "/x", "1", "body")
    b = hmac_sign("secret_b", "POST", "/x", "1", "body")
    assert a != b


def test_hmac_sign_returns_hex_lowercase_64chars():
    from apc_sdk.crypto import hmac_sign

    sig = hmac_sign("s", "POST", "/x", "1", "")
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_hmac_sign_golden_vectors():
    """Pin algorithm to externally-computed hex values; catches spec drift
    (e.g., separator change, encoding bug) that _reference_sign would miss."""
    from apc_sdk.crypto import hmac_sign

    # Empty body
    assert hmac_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", "") == (
        "a77a34ab08ad350514226a42288afa8f38edcb6f0754354f306cf714734a8711"
    )

    # JSON body
    body = '{"device_id":"abc","client_meta":{}}'
    assert hmac_sign("s3cr3t", "POST", "/api/v2/session/init", "1715763600", body) == (
        "02a715a474397fedc4b1f46910f2db5315e6119511c86bb11ab64b704ca577d0"
    )
