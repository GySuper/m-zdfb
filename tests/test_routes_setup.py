"""测试 routes_setup.py:5 步向导 + 校验 + 最终写 yaml(账号在 /config 加)。"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_app(monkeypatch, tmp_path):
    """干净环境:config.yaml 不存在,user_data_dir 在 tmp_path。"""
    config_path = tmp_path / "config.yaml"
    user_data = tmp_path / "data"
    user_data.mkdir()
    # get_config_path(platform) 现在按平台取,需接收平台参数(返回同一个不存在的路径即可)
    monkeypatch.setattr("wxsp.config.get_config_path", lambda *_a, **_k: config_path)
    monkeypatch.setattr("wxsp.config.get_user_data_dir", lambda: user_data)
    monkeypatch.setattr("wxsp.config.get_user_logs_dir", lambda: tmp_path / "logs")
    import importlib

    import wxsp.api.app as app_module

    importlib.reload(app_module)
    return app_module.app, config_path


def test_step1_platform_select_renders(fresh_app):
    """step 1 现在是"选平台"页(M11),不再是欢迎页。"""
    app, _ = fresh_app
    client = TestClient(app)
    resp = client.get("/setup/step/1")
    assert resp.status_code == 200
    assert "选择平台" in resp.text
    assert "视频号" in resp.text  # platform_label(tencent_channel) 渲染为下拉项


def test_step2_post_stores_feishu_and_advances(fresh_app):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/setup/step/2",
        data={
            "app_id": "cli_test_app",
            "app_secret": "secret_test",
            "app_token": "bascntest",
            "table_id": "tbltest",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/3"


def test_step3_post_stores_nas_and_advances(fresh_app, tmp_path):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    nas_dir = tmp_path / "nas"
    nas_dir.mkdir()
    resp = client.post("/setup/step/3", data={"nas_root": str(nas_dir)})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/4"


def test_step4_post_stores_notify_and_advances(fresh_app):
    """step/4 = notify(原来的 step/5),账号步骤已删除。"""
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    # 先把 平台 / feishu / nas 填完,否则 step 4 会被前置守卫拦回
    client.post("/setup/step/1", data={"platform": "tencent_channel"})
    client.post(
        "/setup/step/2",
        data={"app_id": "x", "app_secret": "y", "app_token": "z", "table_id": "t"},
    )
    client.post("/setup/step/3", data={"nas_root": "."})  # cwd 一定存在
    resp = client.post("/setup/step/4", data={"webhook": ""})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/setup/step/5"


def test_complete_writes_config_yaml(fresh_app, tmp_path):
    """整套 happy path 走完,POST /setup/complete 后 config.yaml 落盘;accounts 为空(运营装完后到 /config 加)。"""
    app, config_path = fresh_app
    client = TestClient(app, follow_redirects=False)

    client.post("/setup/step/1", data={"platform": "tencent_channel"})
    client.post(
        "/setup/step/2",
        data={
            "app_id": "cli_x",
            "app_secret": "s",
            "app_token": "bx",
            "table_id": "tbl1",
        },
    )
    nas_dir = tmp_path / "nas"
    nas_dir.mkdir()
    client.post("/setup/step/3", data={"nas_root": str(nas_dir)})
    client.post("/setup/step/4", data={"webhook": ""})

    resp = client.post("/setup/complete")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/accounts"
    assert config_path.exists()

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert parsed["feishu"]["app_id"] == "cli_x"
    assert parsed["feishu"]["app_secret"] == "s"
    assert parsed["paths"]["nas_root"] == str(nas_dir)
    assert parsed["accounts"] == {}  # 向导不建账号
    assert parsed["monitoring"]["notifiers"]["wecom"]["enabled"] is False


def test_step_rejects_when_prior_step_missing(fresh_app):
    """没填飞书就跳到 step 3 提交,后端应该拒绝(redirect 回 step 2)。"""
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post("/setup/step/3", data={"nas_root": "/tmp/nas"})
    assert resp.status_code == 302
    assert "/setup/step/2" in resp.headers["location"]


def test_probe_path_exists(fresh_app, tmp_path):
    app, _ = fresh_app
    client = TestClient(app)
    resp = client.post("/setup/probe-path", data={"path": str(tmp_path)})
    assert resp.status_code == 200
    assert "✓" in resp.text


def test_probe_path_missing(fresh_app):
    app, _ = fresh_app
    client = TestClient(app)
    resp = client.post("/setup/probe-path", data={"path": "/no/such/path/12345"})
    assert resp.status_code == 200
    assert "✗" in resp.text


def test_test_feishu_error_path(fresh_app, monkeypatch):
    """make_client 抛异常时 endpoint 返 ✗ 不抛 500。"""
    app, _ = fresh_app
    monkeypatch.setattr(
        "wxsp.feishu.make_client",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fake")),
    )
    client = TestClient(app)
    resp = client.post(
        "/setup/test-feishu",
        data={"app_id": "x", "app_secret": "y", "app_token": "z", "table_id": "t"},
    )
    assert resp.status_code == 200
    assert "✗" in resp.text
