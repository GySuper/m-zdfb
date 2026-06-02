"""测试 setup 模式:config.yaml 不存在时,非 /setup 路由 302 → /setup/step/1。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_in_setup_mode(monkeypatch, tmp_path):
    """构造 config.yaml 不存在的环境,加载 fastapi app。"""
    # get_config_path(platform) 现在按平台取,需接收平台参数(返回同一个不存在的路径即可)
    monkeypatch.setattr("wxsp.config.get_config_path", lambda *_a, **_k: tmp_path / "config.yaml")
    import importlib

    import wxsp.api.app as app_module

    importlib.reload(app_module)
    return app_module.app


def test_root_redirects_to_setup_when_no_config(app_in_setup_mode):
    client = TestClient(app_in_setup_mode, follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/1"


def test_accounts_redirects_to_setup_when_no_config(app_in_setup_mode):
    client = TestClient(app_in_setup_mode, follow_redirects=False)
    resp = client.get("/accounts")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/1"


def test_setup_route_not_redirected(app_in_setup_mode):
    """/setup/* 本身不应该被重定向(否则死循环)。"""
    # raise_server_exceptions=False:模板还没建时 /setup/step/1 返 500,不抛异常
    client = TestClient(app_in_setup_mode, follow_redirects=False, raise_server_exceptions=False)
    resp = client.get("/setup/step/1")
    # 200 / 404 / 500 都可以,但不应是 302 自指
    assert resp.headers.get("location") != "/setup/step/1"


def test_static_assets_not_redirected(app_in_setup_mode):
    """静态资源不重定向,否则 HTMX 拉不到。"""
    client = TestClient(app_in_setup_mode, follow_redirects=False)
    resp = client.get("/static/anything.css")
    # 不存在返 404,但不能 302 到 /setup
    assert resp.status_code != 302
