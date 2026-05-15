"""TLS cert fingerprint pinning。用 pytest-httpserver + trustme 起本地 HTTPS server。"""

import hashlib
import ssl

import pytest
import trustme
from pytest_httpserver import HTTPServer


@pytest.fixture(scope="session")
def ca_and_cert(tmp_path_factory):
    """返回 (httpserver_ssl_context, fingerprint_hex_lower)。"""
    ca = trustme.CA()
    server_cert = ca.issue_cert("127.0.0.1")
    cert_pem = server_cert.cert_chain_pems[0].bytes()
    # 算 DER 形式的 sha256
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    parsed = x509.load_pem_x509_certificate(cert_pem)
    der = parsed.public_bytes(Encoding.DER)
    fingerprint = hashlib.sha256(der).hexdigest()

    # 写文件给 pytest-httpserver
    tmp_path = tmp_path_factory.mktemp("pinning_certs")
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(server_cert.private_key_pem.bytes())

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx, fingerprint


@pytest.fixture(scope="session")
def httpserver_ssl_context(ca_and_cert):
    return ca_and_cert[0]


def test_correct_fingerprint_passes(httpserver: HTTPServer, ca_and_cert):
    from apc_sdk.pinning import build_httpx_client

    _, fingerprint = ca_and_cert
    httpserver.expect_request("/ping").respond_with_json({"ok": True})

    with build_httpx_client(timeout=5.0, fingerprint=fingerprint) as client:
        resp = client.get(httpserver.url_for("/ping"))
        assert resp.status_code == 200


def test_wrong_fingerprint_raises_network_error(httpserver: HTTPServer, ca_and_cert):
    from apc_sdk.exceptions import ApcNetworkError
    from apc_sdk.pinning import build_httpx_client

    httpserver.expect_request("/ping").respond_with_json({"ok": True})
    bad_fingerprint = "0" * 64

    with build_httpx_client(timeout=5.0, fingerprint=bad_fingerprint) as client:
        with pytest.raises(ApcNetworkError, match="fingerprint mismatch"):
            client.get(httpserver.url_for("/ping"))


def test_no_fingerprint_uses_default_verify():
    """fingerprint=None 时返回的客户端走默认 CA 校验,不做 pinning。"""
    from apc_sdk.pinning import build_httpx_client

    client = build_httpx_client(timeout=5.0, fingerprint=None)
    # 这个 client 应该对公网 HTTPS 正常,对自签会被 CA 链拦下
    # 我们只验证不 raise 在构造期
    assert client is not None
    client.close()


def test_fingerprint_with_colons_and_uppercase_normalized(httpserver: HTTPServer, ca_and_cert):
    """SDK 接受 'AB:CD:..' 大写带冒号格式,自动归一化。"""
    from apc_sdk.pinning import build_httpx_client

    _, fp_lower = ca_and_cert
    fp_with_colons = ":".join(fp_lower[i : i + 2] for i in range(0, 64, 2)).upper()

    httpserver.expect_request("/ping").respond_with_json({"ok": True})
    with build_httpx_client(timeout=5.0, fingerprint=fp_with_colons) as client:
        resp = client.get(httpserver.url_for("/ping"))
        assert resp.status_code == 200
