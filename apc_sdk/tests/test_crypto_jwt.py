"""JWT RS256 校验。用 cryptography 现场签一个 token,然后让被测函数验证。"""

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _keypair() -> tuple[str, str]:
    """返回 (private_pem, public_pem)。"""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def _make_jwt(private_pem: str, **claims) -> str:
    return pyjwt.encode(claims, private_pem, algorithm="RS256")


def test_verify_jwt_valid_token():
    from apc_sdk.crypto import verify_jwt

    priv, pub = _keypair()
    now = datetime.now(timezone.utc)
    token = _make_jwt(
        priv,
        aud="ap_test",
        did="dev_a",
        iat=int(now.timestamp()),
        exp=int((now + timedelta(hours=1)).timestamp()),
    )
    claims = verify_jwt(token, public_key=pub, audience="ap_test")
    assert claims["did"] == "dev_a"
    assert claims["aud"] == "ap_test"


def test_verify_jwt_wrong_audience_rejects():
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    priv, pub = _keypair()
    now = datetime.now(timezone.utc)
    token = _make_jwt(
        priv,
        aud="ap_other",
        iat=int(now.timestamp()),
        exp=int((now + timedelta(hours=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="audience"):
        verify_jwt(token, public_key=pub, audience="ap_test")


def test_verify_jwt_expired_rejects():
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    priv, pub = _keypair()
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    token = _make_jwt(
        priv,
        aud="ap_test",
        iat=int(past.timestamp()),
        exp=int((past + timedelta(minutes=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="expired"):
        verify_jwt(token, public_key=pub, audience="ap_test")


def test_verify_jwt_wrong_signing_key_rejects():
    """用另一对密钥签的 token,本端的 pub key 应当拒签。"""
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    _, our_pub = _keypair()
    attacker_priv, _ = _keypair()
    token = _make_jwt(
        attacker_priv,
        aud="ap_test",
        exp=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="signature"):
        verify_jwt(token, public_key=our_pub, audience="ap_test")


def test_verify_jwt_did_mismatch_when_expected():
    """指定了 expected_did,token did 不匹配 → 拒。"""
    from apc_sdk.crypto import verify_jwt
    from apc_sdk.exceptions import ApcDenied

    priv, pub = _keypair()
    token = _make_jwt(
        priv,
        aud="ap_test",
        did="actual_device",
        exp=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    )
    with pytest.raises(ApcDenied, match="did"):
        verify_jwt(token, public_key=pub, audience="ap_test", expected_did="other_device")


def test_verify_jwt_did_check_skipped_when_expected_is_none():
    """首次签发,本地还没 device_id → expected_did=None 跳过 did 校验。"""
    from apc_sdk.crypto import verify_jwt

    priv, pub = _keypair()
    token = _make_jwt(
        priv,
        aud="ap_test",
        did="anything",
        exp=int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    )
    claims = verify_jwt(token, public_key=pub, audience="ap_test", expected_did=None)
    assert claims["did"] == "anything"
