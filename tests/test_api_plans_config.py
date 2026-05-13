"""Plans / Config 路由(M8)冒烟测试。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date as _date
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import make_settings
from wxsp.api.app import create_app
from wxsp.api.deps import get_session, get_settings
from wxsp.api.routes_config import _mask_yaml, _restore_masked
from wxsp.db import get_engine, init_db, session_scope
from wxsp.models import Account, Task, Video


@pytest.fixture
def client_empty(tmp_path: Path) -> Iterator[TestClient]:
    engine = get_engine(tmp_path / "db.sqlite")
    init_db(engine)
    settings = make_settings(tmp_path, tmp_path)
    app = create_app()

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


# ============== Plans ==============


def test_plans_empty_day_renders_friendly_msg(client_empty: TestClient) -> None:
    r = client_empty.get("/plans?date=2026-01-01")
    assert r.status_code == 200
    assert "没有任务" in r.text


def test_plans_groups_by_account(tmp_path: Path) -> None:
    engine = get_engine(tmp_path / "db.sqlite")
    init_db(engine)
    today = _date.today()
    with session_scope(engine) as s:
        s.add(
            Account(
                id="account_a", display_name="A", user_data_dir=str(tmp_path / "a"), daily_limit=20
            )
        )
        s.add(
            Account(
                id="account_b", display_name="B", user_data_dir=str(tmp_path / "b"), daily_limit=20
            )
        )
        for vid, aid, title in [
            ("v1", "account_a", "标题A1"),
            ("v2", "account_a", "标题A2"),
            ("v3", "account_b", "标题B1"),
        ]:
            s.add(
                Video(
                    id=vid,
                    file_path=f"/x/{vid}.mp4",
                    title=title,
                    description=None,
                    tags_json="[]",
                    cover_path=None,
                    topic=None,
                    original_claim=False,
                    file_hash=None,
                    ingested_at=datetime.now(),
                )
            )
            s.add(
                Task(
                    video_id=vid,
                    account_id=aid,
                    execute_date=today,
                    publish_at=datetime(today.year, today.month, today.day, 18),
                    status="pending",
                )
            )

    app = create_app()
    settings = make_settings(tmp_path, tmp_path)

    def fake_get_session() -> Iterator[Session]:
        with session_scope(engine) as s:
            yield s

    app.dependency_overrides[get_session] = fake_get_session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        r = c.get(f"/plans?date={today}")
    assert r.status_code == 200
    assert "标题A1" in r.text and "标题A2" in r.text and "标题B1" in r.text
    assert "共 3 条" in r.text
    # account_a 那一组要列出 2 条
    assert "account_a" in r.text and "account_b" in r.text


# ============== Config ==============


def test_config_renders_when_file_missing(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # 没有 config.yaml
    r = client_empty.get("/config")
    assert r.status_code == 200
    assert "未找到" in r.text or "找不到 config.yaml" in r.text


def test_config_masks_secrets(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "feishu:\n"
        "  app_id: cli_abc\n"
        "  app_secret: super_secret_value\n"
        "monitoring:\n"
        "  notifiers:\n"
        "    wecom:\n"
        "      webhook: https://qyapi.fake/key=xyz\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    r = client_empty.get("/config")
    assert r.status_code == 200
    assert "cli_abc" in r.text  # app_id 不敏感,保留
    assert "super_secret_value" not in r.text
    assert "xyz" not in r.text
    assert "***" in r.text


def test_mask_keeps_env_var_references() -> None:
    src = "app_secret: ${FEISHU_APP_SECRET}\nwebhook: ${WECOM_BOT_WEBHOOK}\n"
    out = _mask_yaml(src)
    assert "${FEISHU_APP_SECRET}" in out  # ENV 引用保留
    assert "${WECOM_BOT_WEBHOOK}" in out
    assert "***" not in out


def test_mask_normal_keys_untouched() -> None:
    src = "app:\n  data_dir: ./data\n  timezone: Asia/Shanghai"
    assert _mask_yaml(src) == src


# ============== Config 编辑(POST /config)==============

# 一个最小合法的 Settings 序列化模板;测试里 monkeypatch.chdir 到 tmp_path 后用
_VALID_YAML_TEMPLATE = """
app:
  data_dir: ./data
  logs_dir: ./logs
  timezone: Asia/Shanghai
paths:
  nas_root: /tmp/nas
  video_search_root: /tmp/nas/videos
  cover_search_root: /tmp/nas/covers
accounts: {}
scheduler:
  daily_cron_hour: 9
  daily_cron_minute: 0
  strategy: round-robin
publisher:
  headless: false
  upload_timeout_seconds: 600
  step_pause_seconds: [1, 3]
  screenshot_on_error: true
  max_concurrent_accounts: 1
feishu:
  enabled: false
  app_id: cli_real_id
  app_secret: REAL_SECRET_VALUE
  bitable:
    app_token: tok
    table_id: tbl
monitoring:
  cookie_warn_days: 1.5
  notifiers:
    wecom:
      enabled: false
      webhook: https://qyapi.real/key=ABC
  notify_on: []
webui:
  host: 127.0.0.1
  port: 8765
  open_browser_on_start: true
"""


def _setup_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str = _VALID_YAML_TEMPLATE
) -> Path:
    (tmp_path / "config.yaml").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "config.yaml"


def test_config_get_renders_editable_textarea(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_config_dir(tmp_path, monkeypatch)
    r = client_empty.get("/config")
    assert r.status_code == 200
    # textarea 不应被 readonly(空 readonly attr 表示可编辑)
    assert 'name="yaml_text"' in r.text
    # 保存按钮可见
    assert "保存配置" in r.text


def test_config_post_writes_when_valid_and_makes_backup(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _setup_config_dir(tmp_path, monkeypatch)
    old_content = cfg.read_text("utf-8")

    new_yaml = _VALID_YAML_TEMPLATE.replace("port: 8765", "port: 9000")
    r = client_empty.post("/config", data={"yaml_text": new_yaml})
    assert r.status_code == 200
    assert "已保存" in r.text
    # 真写盘
    assert "port: 9000" in cfg.read_text("utf-8")
    # 备份了旧版
    bak = tmp_path / "config.yaml.bak"
    assert bak.exists() and bak.read_text("utf-8") == old_content


def test_config_post_restores_masked_secret(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用户提交时 *** 没改 → 写盘后仍是磁盘上原本的 REAL_SECRET_VALUE。"""
    cfg = _setup_config_dir(tmp_path, monkeypatch)
    submitted = _VALID_YAML_TEMPLATE.replace("app_secret: REAL_SECRET_VALUE", "app_secret: '***'")
    submitted = submitted.replace("webhook: https://qyapi.real/key=ABC", "webhook: '***'")
    r = client_empty.post("/config", data={"yaml_text": submitted})
    assert r.status_code == 200, r.text
    disk = cfg.read_text("utf-8")
    assert "REAL_SECRET_VALUE" in disk
    assert "https://qyapi.real/key=ABC" in disk
    assert "'***'" not in disk and ": ***" not in disk


def test_config_post_invalid_yaml_does_not_write(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _setup_config_dir(tmp_path, monkeypatch)
    snapshot = cfg.read_text("utf-8")
    r = client_empty.post("/config", data={"yaml_text": "app: [unclosed"})
    assert r.status_code == 400
    assert "YAML" in r.text or "语法" in r.text
    assert cfg.read_text("utf-8") == snapshot  # 没碰文件


def test_config_post_missing_env_var_does_not_write(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _setup_config_dir(tmp_path, monkeypatch)
    snapshot = cfg.read_text("utf-8")
    monkeypatch.delenv("DEFINITELY_UNSET_VAR_FOR_TEST", raising=False)
    bad = _VALID_YAML_TEMPLATE.replace(
        "app_secret: REAL_SECRET_VALUE", "app_secret: ${DEFINITELY_UNSET_VAR_FOR_TEST}"
    )
    r = client_empty.post("/config", data={"yaml_text": bad})
    assert r.status_code == 400
    assert "DEFINITELY_UNSET_VAR_FOR_TEST" in r.text
    assert cfg.read_text("utf-8") == snapshot


def test_config_post_schema_violation_does_not_write(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _setup_config_dir(tmp_path, monkeypatch)
    snapshot = cfg.read_text("utf-8")
    # webui.port 是 int,这里灌一个字符串 → Pydantic 校验失败
    bad = _VALID_YAML_TEMPLATE.replace("port: 8765", "port: not_a_number_lol")
    r = client_empty.post("/config", data={"yaml_text": bad})
    assert r.status_code == 400
    assert cfg.read_text("utf-8") == snapshot


def test_restore_masked_keeps_envvar_lines() -> None:
    """*** 还原:env 引用行不受影响,只动 *** 行。"""
    old = "feishu:\n  app_secret: ${FEISHU_APP_SECRET}\nmon:\n  webhook: https://real\n"
    new = "feishu:\n  app_secret: ${FEISHU_APP_SECRET}\nmon:\n  webhook: '***'\n"
    out = _restore_masked(new, old)
    assert "${FEISHU_APP_SECRET}" in out
    assert "https://real" in out
    assert "***" not in out


def test_restore_masked_leaves_unknown_keys_alone() -> None:
    """旧文件里没这 key → 保留 ***,让校验阶段提示用户。"""
    old = "feishu:\n  app_id: cli_abc\n"
    new = "feishu:\n  app_id: cli_abc\n  webhook: '***'\n"
    out = _restore_masked(new, old)
    assert "webhook: '***'" in out
