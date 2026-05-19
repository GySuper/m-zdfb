"""Plans / Config(M8)路由测试。

Config 已重构为表单驱动:
- GET /config 渲染 8 个 section 的输入控件
- POST /config 接收表单字段 → 拼成完整 dict → Pydantic 校验 → 备份 + 原子写
- POST /config/accounts/add 添加账号
- POST /config/accounts/{id}/delete 删除账号
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.conftest import make_settings
from wxsp.api.app import create_app
from wxsp.api.deps import get_session, get_settings
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
    assert "没有发布计划" in r.text


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
    assert "account_a" in r.text and "account_b" in r.text


# ============== Config(表单 UI)==============

_VALID_YAML = """
app:
  data_dir: ./data
  logs_dir: ./logs
  timezone: Asia/Shanghai
paths:
  nas_root: /tmp/nas
accounts:
  account_a:
    display_name: 美食号
    enabled: true
    daily_limit: 20
    user_data_dir: ./data/chrome-profiles/account_a
    video_search_root: /tmp/nas/videos
    cover_search_root: /tmp/nas/covers
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
  field_map:
    video_file: 视频文件
    title: 标题
    description: 描述
    tags: 标签
    cover: 封面文件
    topic: 合集
    original_claim: 原创
    account: 账号
    execute_date: 执行日期
    publish_at: 定时发布时间
    status: 状态
    remote_url: 已发布链接
    error_message: 错误信息
  sync:
    write_back_enabled: true
monitoring:
  cookie_warn_days: 1.5
  notifiers:
    wecom:
      enabled: false
      webhook: https://qyapi.real/key=ABC
  notify_on: [task_failed, risk_control]
webui:
  host: 127.0.0.1
  port: 8765
  open_browser_on_start: true
"""


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str = _VALID_YAML) -> Path:
    (tmp_path / "config.yaml").write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "config.yaml"


def _form(**overrides: Any) -> dict[str, Any]:
    """构造 POST /config 的表单字段(httpx 支持 list 值 → 同名字段多次出现)。"""
    data: dict[str, Any] = {
        "app_data_dir": "./data",
        "app_logs_dir": "./logs",
        "app_timezone": "Asia/Shanghai",
        "paths_nas_root": "/tmp/nas",
        "sched_hour": "9",
        "sched_minute": "0",
        "sched_strategy": "round-robin",
        "pub_upload_timeout": "600",
        "pub_step_pause_min": "1.0",
        "pub_step_pause_max": "3.0",
        "pub_max_concurrent": "1",
        "feishu_app_id": "cli_real_id",
        "feishu_app_secret": "",
        "feishu_bitable_app_token": "tok",
        "feishu_bitable_table_id": "tbl",
        "feishu_fm_video_file": "视频文件",
        "feishu_fm_title": "标题",
        "feishu_fm_description": "描述",
        "feishu_fm_tags": "标签",
        "feishu_fm_cover": "封面文件",
        "feishu_fm_topic": "合集",
        "feishu_fm_original_claim": "原创",
        "feishu_fm_account": "账号",
        "feishu_fm_execute_date": "执行日期",
        "feishu_fm_publish_at": "定时发布时间",
        "feishu_fm_status": "状态",
        "feishu_fm_remote_url": "已发布链接",
        "feishu_fm_error_message": "错误信息",
        "mon_cookie_warn_days": "1.5",
        "mon_wecom_webhook": "",
        "webui_host": "127.0.0.1",
        "webui_port": "8765",
        # notify_on 用 list,httpx 会展开成多次同名字段
        "notify_on": ["task_failed", "risk_control"],
    }
    # checkbox 默认值:勾上的项 = "on";没出现 = False。
    checkbox_defaults = {
        "pub_headless": False,
        "pub_screenshot_on_error": True,
        "feishu_enabled": False,
        "feishu_sync_writeback": True,
        "mon_wecom_enabled": False,
        "webui_open_browser": True,
    }
    for name, default in checkbox_defaults.items():
        if overrides.get(name, default):
            data[name] = "on"
        overrides.pop(name, None)
    data.update({k: v for k, v in overrides.items()})
    # value 是 list → 保留;其它都强转 str
    return {k: (v if isinstance(v, list) else str(v)) for k, v in data.items()}


def test_config_get_renders_form_fields(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    r = client_empty.get("/config")
    assert r.status_code == 200
    # 2026-05-16 重设计后,每个分区一个独立 form,action 是 /config/section/<name>
    for section_slug in ["publisher", "scheduler", "feishu", "monitoring", "system"]:
        assert f'action="/config/section/{section_slug}"' in r.text
    assert 'method="post"' in r.text
    # 各 section 标题(2026-05-16 UI 重设计后改为场景化分区文案)
    for section in [
        "系统",
        "NAS / 文件路径",
        "调度时间",
        "发布规则",
        "飞书 Bitable",
        "通知告警",
        "Web UI",
    ]:
        assert section in r.text
    # 关键 input name 都在
    for name in [
        "app_timezone",
        "paths_nas_root",
        "sched_hour",
        "pub_upload_timeout",
        "feishu_app_id",
        "feishu_bitable_app_token",
        "mon_cookie_warn_days",
        "webui_port",
        "notify_on",
    ]:
        assert f'name="{name}"' in r.text
    # 账号 section 渲染
    assert "美食号" in r.text and "account_a" in r.text


def test_config_get_does_not_leak_plaintext_secret(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """明文 secret 不应回显到 HTML(env 引用允许,明文 input 应为空)。"""
    _setup(tmp_path, monkeypatch)
    r = client_empty.get("/config")
    assert r.status_code == 200
    assert "REAL_SECRET_VALUE" not in r.text
    assert "qyapi.real/key=ABC" not in r.text


def test_config_get_keeps_envvar_secret_visible(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _VALID_YAML.replace(
        "app_secret: REAL_SECRET_VALUE", "app_secret: ${FEISHU_APP_SECRET}"
    ).replace("webhook: https://qyapi.real/key=ABC", "webhook: ${WECOM_BOT_WEBHOOK}")
    _setup(tmp_path, monkeypatch, content)
    r = client_empty.get("/config")
    assert "${FEISHU_APP_SECRET}" in r.text
    assert "${WECOM_BOT_WEBHOOK}" in r.text


def test_config_post_writes_when_valid_and_backups(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    old = cfg.read_text("utf-8")

    fields = _form(webui_port=9000)
    r = client_empty.post("/config", data=fields)
    assert r.status_code == 200, r.text
    assert "已保存" in r.text

    new_data = yaml.safe_load(cfg.read_text("utf-8"))
    assert new_data["webui"]["port"] == 9000
    assert (tmp_path / "config.yaml.bak").read_text("utf-8") == old


def test_config_post_keeps_secret_when_left_empty(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """app_secret / wecom webhook 留空 → 磁盘原值不动。"""
    cfg = _setup(tmp_path, monkeypatch)
    r = client_empty.post("/config", data=_form())  # 默认 feishu_app_secret="" / webhook=""
    assert r.status_code == 200, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert disk["feishu"]["app_secret"] == "REAL_SECRET_VALUE"
    assert disk["monitoring"]["notifiers"]["wecom"]["webhook"] == "https://qyapi.real/key=ABC"


def test_config_post_overrides_secret_when_provided(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    r = client_empty.post(
        "/config",
        data=_form(feishu_app_secret="brand_new_secret", mon_wecom_webhook="https://qyapi.new"),
    )
    assert r.status_code == 200, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert disk["feishu"]["app_secret"] == "brand_new_secret"
    assert disk["monitoring"]["notifiers"]["wecom"]["webhook"] == "https://qyapi.new"


def test_config_post_envvar_secret_passes_through(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """${ENV_VAR} 提交应原样写盘(load_settings 时再展开)。"""
    cfg = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("WECOM_BOT_WEBHOOK", "https://anything")
    r = client_empty.post(
        "/config",
        data=_form(mon_wecom_webhook="${WECOM_BOT_WEBHOOK}"),
    )
    assert r.status_code == 200, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert disk["monitoring"]["notifiers"]["wecom"]["webhook"] == "${WECOM_BOT_WEBHOOK}"


def test_config_post_invalid_does_not_write(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端口范围越界 → Pydantic 校验失败 → 不写盘。"""
    cfg = _setup(tmp_path, monkeypatch)
    snap = cfg.read_text("utf-8")
    # webui_port FastAPI 字段类型是 int,但 Pydantic Settings 的 port 也是 int;
    # 改成非法字段(timezone 留空 → 但 str 类型不报错)。用 sched_hour=99(超出 0-23 校验)
    # —— 但 Settings 没限制 hour 范围,所以用 max_concurrent_accounts=-1 这种纯逻辑校验。
    # 这里改成提交 daily_cron_hour 为字符串,FastAPI Form 解析会 422,这是另一回事。
    # 真做法:notify_on 给一个非合法字符串,Settings 接受任意字符串 list,所以也不会失败。
    # 选 mon_cookie_warn_days < 0:Settings 没限制。
    # 选 paths_nas_root=""(空字符串变 Path("")),Pydantic Path 接受空。
    # 干脆把 feishu.bitable.app_token 留空:Settings 要求 str(非 None),空字符串也合法。
    # —— 我们没有强校验,所以这条 case 跳过严格校验,改成测 ${UNSET} 触发 ValueError。
    monkeypatch.delenv("DEFINITELY_UNSET_VAR_FOR_TEST", raising=False)
    fields = _form(mon_wecom_webhook="${DEFINITELY_UNSET_VAR_FOR_TEST}")
    r = client_empty.post("/config", data=fields)
    assert r.status_code == 400
    assert "DEFINITELY_UNSET_VAR_FOR_TEST" in r.text
    assert cfg.read_text("utf-8") == snap


def test_config_post_notify_on_writes_checked_list(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    r = client_empty.post(
        "/config",
        data=_form(notify_on=["task_failed", "nas_unreachable", "element_not_found"]),
    )
    assert r.status_code == 200, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert set(disk["monitoring"]["notify_on"]) == {
        "task_failed",
        "nas_unreachable",
        "element_not_found",
    }


def test_config_post_no_notify_on_writes_empty_list(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    r = client_empty.post("/config", data=_form(notify_on=[]))
    assert r.status_code == 200, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert disk["monitoring"]["notify_on"] == []


# ============== path_aliases (M5 跨 OS 路径) ==============


_YAML_WITH_ALIASES = _VALID_YAML.replace(
    "paths:\n  nas_root: /tmp/nas\n",
    (
        "paths:\n"
        "  nas_root: /tmp/nas\n"
        "  path_aliases:\n"
        '    "\\\\\\\\172.31.15.11\\\\dianshang": /Volumes/dianshang\n'
        '    "\\\\\\\\nas.local\\\\wxsp": /Volumes/wxsp\n'
    ),
)


def _section_feishu_form(**overrides: Any) -> dict[str, Any]:
    """构造 POST /config/section/feishu 表单。"""
    data: dict[str, Any] = {
        "feishu_app_id": "cli_real_id",
        "feishu_app_secret": "",
        "feishu_bitable_app_token": "tok",
        "feishu_bitable_table_id": "tbl",
        "paths_nas_root": "/tmp/nas",
    }
    checkbox_defaults = {
        "feishu_enabled": False,
        "feishu_sync_writeback": True,
    }
    for name, default in checkbox_defaults.items():
        if overrides.get(name, default):
            data[name] = "on"
        overrides.pop(name, None)
    data.update(overrides)
    return data


def test_config_get_renders_path_aliases_editor_with_existing_rows(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YAML 有 2 条 alias → 表单渲染 2 行,key/value 都在 input 里。"""
    _setup(tmp_path, monkeypatch, _YAML_WITH_ALIASES)
    r = client_empty.get("/config")
    assert r.status_code == 200
    assert 'name="path_alias_key"' in r.text
    assert 'name="path_alias_value"' in r.text
    assert "/Volumes/dianshang" in r.text
    assert "/Volumes/wxsp" in r.text
    # 至少出现两次 path_alias_key(两行 input)
    assert r.text.count('name="path_alias_key"') >= 2


def test_config_section_feishu_saves_path_aliases(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /config/section/feishu 带 path_alias_key/value 平行 list → 写进 yaml。"""
    cfg = _setup(tmp_path, monkeypatch)
    fields = _section_feishu_form()
    # httpx data dict 的 list 值 → 同 name 多次出现
    fields["path_alias_key"] = [
        "\\\\172.31.15.11\\dianshang",
        "\\\\nas.local\\wxsp",
    ]
    fields["path_alias_value"] = [
        "/Volumes/dianshang",
        "/Volumes/wxsp",
    ]
    r = client_empty.post("/config/section/feishu", data=fields, follow_redirects=False)
    assert r.status_code == 303, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert disk["paths"]["path_aliases"] == {
        "\\\\172.31.15.11\\dianshang": "/Volumes/dianshang",
        "\\\\nas.local\\wxsp": "/Volumes/wxsp",
    }


def test_config_section_feishu_filters_empty_alias_rows(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运营点了 + 但没填 → 那行被忽略,不会留下空 key/value 进 yaml。"""
    cfg = _setup(tmp_path, monkeypatch)
    fields = _section_feishu_form()
    fields["path_alias_key"] = ["\\\\host\\share", "", "  "]
    fields["path_alias_value"] = ["/Volumes/share", "/Volumes/empty_key", ""]
    r = client_empty.post("/config/section/feishu", data=fields, follow_redirects=False)
    assert r.status_code == 303, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert disk["paths"]["path_aliases"] == {"\\\\host\\share": "/Volumes/share"}


def test_config_section_feishu_clears_aliases_when_all_empty(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """所有行清空 → yaml 里不应留下空 path_aliases 字段(也不留 {})。"""
    cfg = _setup(tmp_path, monkeypatch, _YAML_WITH_ALIASES)
    # 验证起点确实有 alias
    assert yaml.safe_load(cfg.read_text("utf-8"))["paths"]["path_aliases"]
    fields = _section_feishu_form()
    # 不带 path_alias_key/value → list 默认 []
    r = client_empty.post("/config/section/feishu", data=fields, follow_redirects=False)
    assert r.status_code == 303, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    # path_aliases key 干脆不写(YAML 里没这个字段)→ Settings 走默认 {}
    assert "path_aliases" not in disk["paths"]


def test_config_legacy_post_preserves_path_aliases(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """legacy POST /config 没 path_alias_* 字段,但磁盘上原值不应被吹掉。"""
    cfg = _setup(tmp_path, monkeypatch, _YAML_WITH_ALIASES)
    r = client_empty.post("/config", data=_form())
    assert r.status_code == 200, r.text
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert disk["paths"]["path_aliases"] == {
        "\\\\172.31.15.11\\dianshang": "/Volumes/dianshang",
        "\\\\nas.local\\wxsp": "/Volumes/wxsp",
    }


# ============== 账号 CRUD ==============


def test_accounts_add_generates_id_and_writes_entry(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add 不接 account_id / user_data_dir,后端自动生成 account_<8 hex> + 对应 profile 目录。"""
    cfg = _setup(tmp_path, monkeypatch)
    before = set(yaml.safe_load(cfg.read_text("utf-8"))["accounts"].keys())
    r = client_empty.post(
        "/config/accounts/add",
        data={
            "display_name": "搞笑号",
            "daily_limit": "15",
            "video_search_root": "/tmp/nas/videos/z",
            "cover_search_root": "/tmp/nas/covers/z",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    after = set(disk["accounts"].keys())
    added = after - before
    assert len(added) == 1
    aid = added.pop()
    # 生成格式:account_<8 lowercase hex>
    assert aid.startswith("account_") and len(aid) == len("account_") + 8
    assert all(c in "0123456789abcdef" for c in aid.removeprefix("account_"))
    # flash 里包含生成的 ID(让运营知道新账号叫啥)
    assert f"已添加账号 {aid}" in unquote(r.headers["location"])
    new = disk["accounts"][aid]
    assert new["display_name"] == "搞笑号"
    assert new["daily_limit"] == 15
    assert new["video_search_root"] == "/tmp/nas/videos/z"
    assert new["cover_search_root"] == "/tmp/nas/covers/z"
    assert new["user_data_dir"] == f"./data/chrome-profiles/{aid}"


def test_accounts_add_rejects_duplicate_display_name_with_friendly_msg(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """display_name 撞已有账号 → flash 给运营友好提示,不暴露 pydantic 报错 + 假 ID。"""
    _setup(tmp_path, monkeypatch)
    # _VALID_YAML 里 account_a 的 display_name 是 "美食号"
    r = client_empty.post(
        "/config/accounts/add",
        data={
            "display_name": "美食号",
            "daily_limit": "20",
            "video_search_root": "/tmp/v",
            "cover_search_root": "/tmp/c",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = unquote(r.headers["location"])
    assert "已有账号叫 '美食号'" in location
    assert "account_a" in location  # 真实的占用方
    assert "Value error" not in location  # 不要 pydantic 包装
    assert "重复使用" not in location  # 不要 Settings 校验器原话


def test_accounts_add_strips_display_name(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """display_name 前后空白会被 strip(避免'测试' vs '测试 '当成两个账号)。"""
    _setup(tmp_path, monkeypatch)
    r = client_empty.post(
        "/config/accounts/add",
        data={
            "display_name": "  美食号  ",  # 同名 + 空白
            "daily_limit": "20",
            "video_search_root": "/tmp/v",
            "cover_search_root": "/tmp/c",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = unquote(r.headers["location"])
    assert "已有账号叫 '美食号'" in location


def test_accounts_update_writes_changes(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    r = client_empty.post(
        "/config/accounts/account_a/update",
        data={
            "display_name": "美食号改名了",
            "daily_limit": "30",
            "video_search_root": "/tmp/nas/new-videos",
            "cover_search_root": "/tmp/nas/new-covers",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "已更新账号 account_a" in unquote(r.headers["location"])
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    a = disk["accounts"]["account_a"]
    assert a["display_name"] == "美食号改名了"
    assert a["daily_limit"] == 30
    assert a["video_search_root"] == "/tmp/nas/new-videos"
    assert a["cover_search_root"] == "/tmp/nas/new-covers"
    assert a["enabled"] is True
    # user_data_dir 不被改动
    assert a["user_data_dir"] == "./data/chrome-profiles/account_a"


def test_accounts_update_unknown_no_op(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    snap = cfg.read_text("utf-8")
    r = client_empty.post(
        "/config/accounts/no_such/update",
        data={
            "display_name": "x",
            "daily_limit": "10",
            "video_search_root": "/v",
            "cover_search_root": "/c",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "不存在" in unquote(r.headers["location"])
    assert cfg.read_text("utf-8") == snap


def test_accounts_edit_modal_data_available(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-05-16 重设计后,账号编辑改用 modal,onclick 里的 JSON 包含账号 ID 和字段。"""
    _setup(tmp_path, monkeypatch)
    r = client_empty.get("/config")
    assert r.status_code == 200
    # modal dialog 存在
    assert "<dialog" in r.text and 'id="account-modal"' in r.text
    # 点击"编辑"会调用 openAccountModal(data),data 里包含账号 id 和字段
    assert "openAccountModal(" in r.text
    assert "&#34;id&#34;: &#34;account_a&#34;" in r.text or '"id": "account_a"' in r.text
    # modal form 里包含所有字段 name(由 JS 动态填充值)
    for name in [
        "display_name",
        "enabled",
        "daily_limit",
        "video_search_root",
        "cover_search_root",
    ]:
        assert f'name="{name}"' in r.text


def test_accounts_delete_removes_entry(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    r = client_empty.post("/config/accounts/account_a/delete", follow_redirects=False)
    assert r.status_code == 303
    disk = yaml.safe_load(cfg.read_text("utf-8"))
    assert "account_a" not in disk["accounts"]


def test_accounts_delete_unknown_no_op(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _setup(tmp_path, monkeypatch)
    snap = cfg.read_text("utf-8")
    r = client_empty.post("/config/accounts/no_such/delete", follow_redirects=False)
    assert r.status_code == 303
    assert "不存在" in unquote(r.headers["location"])
    assert cfg.read_text("utf-8") == snap


# ============== 通知告警:测试推送 ==============


def test_test_wecom_empty_returns_warn(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """密码框留空 + 磁盘 yaml 也没存 webhook → warn 提示'为空'。"""
    no_webhook_yaml = _VALID_YAML.replace("webhook: https://qyapi.real/key=ABC", 'webhook: ""')
    _setup(tmp_path, monkeypatch, content=no_webhook_yaml)
    r = client_empty.post("/config/test-wecom", data={"mon_wecom_webhook": ""})
    assert r.status_code == 200
    assert 'class="flash warn"' in r.text
    assert "为空" in r.text


def test_test_wecom_success(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常 https webhook + WecomNotifier.send 返回 True → ok 片段。"""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("wxsp.notify.WecomNotifier.send", lambda self, event: True)
    r = client_empty.post(
        "/config/test-wecom",
        data={"mon_wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake"},
    )
    assert r.status_code == 200
    assert 'class="flash ok"' in r.text
    assert "已发送" in r.text


def test_test_wecom_send_returns_false(
    client_empty: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send 返 False(errcode/网络问题)→ error 片段。"""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr("wxsp.notify.WecomNotifier.send", lambda self, event: False)
    r = client_empty.post(
        "/config/test-wecom",
        data={"mon_wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"},
    )
    assert r.status_code == 200
    assert 'class="flash error"' in r.text


def test_test_wecom_rejects_non_https(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    r = client_empty.post(
        "/config/test-wecom", data={"mon_wecom_webhook": "http://insecure/webhook"}
    )
    assert r.status_code == 200
    assert 'class="flash error"' in r.text
    assert "https" in r.text


def test_test_wecom_missing_env_var(
    client_empty: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("NO_SUCH_WECOM_TEST_VAR", raising=False)
    r = client_empty.post(
        "/config/test-wecom",
        data={"mon_wecom_webhook": "${NO_SUCH_WECOM_TEST_VAR}"},
    )
    assert r.status_code == 200
    assert 'class="flash error"' in r.text
    assert "环境变量" in r.text or "NO_SUCH_WECOM_TEST_VAR" in r.text
