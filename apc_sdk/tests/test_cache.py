"""SessionStore: session.json 原子读写 + 首装 bootstrap + 损坏 fallback。"""

import json


def test_load_nonexistent_returns_bootstrapped(tmp_path):
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    cache = store.load_or_bootstrap()
    assert "device_id" in cache
    assert "last_success_at" in cache
    assert cache["schema_version"] == 1
    # 文件应该已经写入磁盘
    assert (tmp_path / "session.json").exists()


def test_load_or_bootstrap_idempotent(tmp_path):
    """两次调用返回相同的 device_id(第二次读已写入的文件,不重写)。"""
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    cache1 = store.load_or_bootstrap()
    cache2 = store.load_or_bootstrap()
    assert cache1["device_id"] == cache2["device_id"]
    assert cache1["last_success_at"] == cache2["last_success_at"]


def test_save_then_load(tmp_path):
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    store.save(
        {
            "schema_version": 1,
            "device_id": "abc",
            "last_success_at": "2026-05-10T00:00:00+00:00",
            "today_date": "2026-05-15",
            "today_verdict": "pass",
        }
    )
    cache = store.load_or_bootstrap()
    assert cache["device_id"] == "abc"
    assert cache["today_verdict"] == "pass"


def test_save_is_atomic(tmp_path):
    """save 后,临时文件不应残留;主文件是完整 JSON。"""
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    store.save({"schema_version": 1, "device_id": "x", "last_success_at": "..."})
    # 没有 .tmp 残留
    assert not (tmp_path / "session.json.tmp").exists()
    # 主文件可解析
    text = (tmp_path / "session.json").read_text()
    assert json.loads(text)["device_id"] == "x"


def test_load_corrupted_json_rebootstraps(tmp_path):
    """文件损坏(非 JSON) → 当作首装重建,不抛。"""
    from apc_sdk.cache import SessionStore

    path = tmp_path / "session.json"
    path.write_text("this is not json {{{")
    store = SessionStore(path)
    cache = store.load_or_bootstrap()
    assert "device_id" in cache
    # 文件被覆盖成合法 JSON
    assert json.loads(path.read_text())["device_id"] == cache["device_id"]


def test_load_missing_required_field_rebootstraps(tmp_path):
    """缺 device_id 也视作损坏,重建。"""
    from apc_sdk.cache import SessionStore

    path = tmp_path / "session.json"
    path.write_text(json.dumps({"schema_version": 1}))
    store = SessionStore(path)
    cache = store.load_or_bootstrap()
    assert "device_id" in cache
    assert "last_success_at" in cache


def test_bootstrap_last_success_at_is_now_utc(tmp_path):
    """首装时 last_success_at 写当前时间 UTC ISO,而不是其他时区。"""
    from datetime import datetime, timezone

    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    before = datetime.now(timezone.utc)
    cache = store.load_or_bootstrap()
    after = datetime.now(timezone.utc)

    written = datetime.fromisoformat(cache["last_success_at"])
    assert written.tzinfo is not None  # 有时区
    assert before <= written <= after


def test_update_partial(tmp_path):
    """update 只改给的 key,不清空别的。"""
    from apc_sdk.cache import SessionStore

    store = SessionStore(tmp_path / "session.json")
    store.load_or_bootstrap()
    store.update(today_date="2026-05-15", today_verdict="pass")
    cache = store.load_or_bootstrap()
    assert cache["today_date"] == "2026-05-15"
    assert cache["today_verdict"] == "pass"
    assert "device_id" in cache  # 没被清掉
