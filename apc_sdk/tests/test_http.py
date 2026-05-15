"""fetch_session: 拼 body + 签名 + POST + 解析响应。用 respx mock httpx。"""

import json

import httpx
import pytest
import respx


def _basic_config(tmp_path):
    from apc_sdk import ApcConfig

    return ApcConfig(
        endpoint="https://apc.example.com:8443",
        app_id="ap_test",
        app_secret="s3cr3t",
        public_key="ignored-for-this-test",
        cache_dir=tmp_path,
    )


@respx.mock
def test_fetch_session_success_returns_license(tmp_path):
    from apc_sdk._http import fetch_session

    cfg = _basic_config(tmp_path)
    route = respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"license": "eyJ.fake.jwt"})
    )
    client = httpx.Client()
    license_jwt = fetch_session(client, cfg, device_id="dev-1")
    assert license_jwt == "eyJ.fake.jwt"
    assert route.called

    # 检查请求体
    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body["device_id"] == "dev-1"
    assert sent.headers["X-Client-Id"] == "ap_test"
    assert "X-T" in sent.headers
    assert "X-Sig" in sent.headers


@respx.mock
def test_fetch_session_device_id_none_when_first_call(tmp_path):
    """首次签发,device_id=None 直接序列化成 null。"""
    from apc_sdk._http import fetch_session

    cfg = _basic_config(tmp_path)
    route = respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"license": "tok"})
    )
    client = httpx.Client()
    fetch_session(client, cfg, device_id=None)
    body = json.loads(route.calls.last.request.content)
    assert body["device_id"] is None


@respx.mock
def test_fetch_session_403_raises_denied(tmp_path):
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcDenied

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(403, json={"error": "DEVICE_DISABLED"})
    )
    client = httpx.Client()
    with pytest.raises(ApcDenied, match=r"403"):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_500_raises_network(tmp_path):
    """5xx 视为网络问题,不算明确拒绝。"""
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcNetworkError

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    client = httpx.Client()
    with pytest.raises(ApcNetworkError, match=r"503"):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_timeout_raises_network(tmp_path):
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcNetworkError

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        side_effect=httpx.ConnectTimeout("simulated")
    )
    client = httpx.Client()
    with pytest.raises(ApcNetworkError):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_200_missing_license_raises_network(tmp_path):
    """200 但响应里没 license 字段 → 视为协议错(走网络分支,不写 today_verdict)。"""
    from apc_sdk._http import fetch_session
    from apc_sdk.exceptions import ApcNetworkError

    cfg = _basic_config(tmp_path)
    respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"oops": "no license"})
    )
    client = httpx.Client()
    with pytest.raises(ApcNetworkError):
        fetch_session(client, cfg, device_id="dev-1")


@respx.mock
def test_fetch_session_includes_client_meta(tmp_path):
    """ApcConfig.client_meta 被注入到 body。"""
    from apc_sdk import ApcConfig
    from apc_sdk._http import fetch_session

    cfg = ApcConfig(
        endpoint="https://apc.example.com:8443",
        app_id="ap_test",
        app_secret="s3cr3t",
        public_key="ignored",
        cache_dir=tmp_path,
        client_meta={"app_version": "0.1.4", "platform": "darwin"},
    )
    route = respx.post("https://apc.example.com:8443/api/v2/session/init").mock(
        return_value=httpx.Response(200, json={"license": "tok"})
    )
    client = httpx.Client()
    fetch_session(client, cfg, device_id="dev-1")
    body = json.loads(route.calls.last.request.content)
    assert body["client_meta"]["app_version"] == "0.1.4"
    assert body["client_meta"]["platform"] == "darwin"
