"""测试 routes_setup.py:6 步向导 + 校验 + 最终写 yaml。"""

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
    monkeypatch.setattr("wxsp.config.get_config_path", lambda: config_path)
    monkeypatch.setattr("wxsp.config.get_user_data_dir", lambda: user_data)
    monkeypatch.setattr("wxsp.config.get_user_logs_dir", lambda: tmp_path / "logs")
    import importlib

    import wxsp.api.app as app_module

    importlib.reload(app_module)
    return app_module.app, config_path


@pytest.mark.skip(reason="template comes in Task 7 (setup wizard templates)")
def test_step1_welcome_renders(fresh_app):
    app, _ = fresh_app
    client = TestClient(app)
    resp = client.get("/setup/step/1")
    assert resp.status_code == 200
    assert "欢迎" in resp.text or "wxsp" in resp.text


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


def test_step4_post_validates_accounts(fresh_app):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/setup/step/4",
        data={
            "account_id[]": ["account_a", "account_b"],
            "display_name[]": ["美食号", "健身号"],
            "daily_limit[]": ["20", "20"],
        },
    )
    assert resp.status_code == 302


@pytest.mark.skip(reason="template comes in Task 7")
def test_step4_rejects_invalid_account_id(fresh_app):
    app, _ = fresh_app
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/setup/step/4",
        data={
            "account_id[]": ["Account-A"],
            "display_name[]": ["x"],
            "daily_limit[]": ["20"],
        },
    )
    assert resp.status_code == 200
    assert "account_id" in resp.text.lower() or "格式" in resp.text


def test_complete_writes_config_yaml(fresh_app, tmp_path, monkeypatch):
    """整套 happy path 走完,POST /setup/complete 后 config.yaml 落盘。"""
    app, config_path = fresh_app
    monkeypatch.setattr("wxsp.autostart.enable_autostart", lambda: None)
    client = TestClient(app, follow_redirects=False)

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
    client.post(
        "/setup/step/4",
        data={
            "account_id[]": ["account_a"],
            "display_name[]": ["美食号"],
            "daily_limit[]": ["20"],
        },
    )
    client.post("/setup/step/5", data={"webhook": ""})

    resp = client.post("/setup/complete", data={"enable_autostart": "on"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/accounts"
    assert config_path.exists()

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert parsed["feishu"]["app_id"] == "cli_x"
    assert parsed["feishu"]["app_secret"] == "s"
    assert parsed["paths"]["nas_root"] == str(nas_dir)
    assert "account_a" in parsed["accounts"]
    assert parsed["accounts"]["account_a"]["display_name"] == "美食号"
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
