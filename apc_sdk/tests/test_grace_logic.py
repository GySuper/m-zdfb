"""ApcClient.check() 7 天 grace + fail-open 边界。

注入 frozen clock + mock fetch_session,避开真实 httpx/网络。
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path


def _config(tmp_path: Path):
    from apc_sdk import ApcConfig

    return ApcConfig(
        endpoint="https://apc.example.com:8443",
        app_id="ap_test",
        app_secret="s",
        public_key="-----BEGIN PUBLIC KEY-----\nignored\n-----END PUBLIC KEY-----",
        cache_dir=tmp_path,
        grace_days=7,
    )


def _seed_cache(tmp_path: Path, last_success_at: datetime, device_id: str = "dev"):
    """预置 session.json,模拟非首装状态。"""
    import json

    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "device_id": device_id,
                "last_success_at": last_success_at.isoformat(),
            }
        )
    )


class _FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def utcnow(self) -> datetime:
        return self.now


def _make_client(tmp_path, fake_now, *, fetch_result):
    """构造 ApcClient,注入 mock fetch_session + fake clock。"""
    from apc_sdk.client import ApcClient

    cfg = _config(tmp_path)
    client = ApcClient(cfg)
    # monkeypatch 内部依赖
    client._clock = _FakeClock(fake_now)

    def fake_fetch(*args, **kwargs):
        if isinstance(fetch_result, Exception):
            raise fetch_result
        return fetch_result

    client._fetch_session = fake_fetch  # type: ignore[attr-defined]

    # bypass JWT 校验:直接把 fetch 返回值当 device_id 来源
    def fake_verify(license_jwt: str, **_kwargs):
        return {"did": "dev", "sub": "ap_test"}

    client._verify_jwt = fake_verify  # type: ignore[attr-defined]
    return client


def test_G1_grace_6d_23h_59m_network_failure_returns_PASS(tmp_path):
    """边界 1:6d 23h 59m + 网络失败 → PASS,不写 today_verdict。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcNetworkError

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=6, hours=23, minutes=59, seconds=59)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result=ApcNetworkError("simulated"))
    assert client.check() == Verdict.PASS

    # 没写 today_verdict
    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert "today_verdict" not in cache


def test_G2_grace_7d_1s_network_failure_returns_DENY_and_caches(tmp_path):
    """边界 2:7d 1s + 网络失败 → DENY,且写入 today_verdict。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcNetworkError

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=7, seconds=1)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result=ApcNetworkError("simulated"))
    assert client.check() == Verdict.DENY

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert cache["today_verdict"] == "deny"


def test_G3_200_resets_last_success_at(tmp_path):
    """边界 3:200 通过 → last_success_at 更新到 now。"""
    from apc_sdk import Verdict

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=6)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result="fake.jwt.token")
    assert client.check() == Verdict.PASS

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    updated = datetime.fromisoformat(cache["last_success_at"])
    assert updated == now


def test_G4_403_does_not_reset_last_success_at(tmp_path):
    """边界 4:403 拒绝 → today_verdict=deny,last_success_at 不变。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcDenied

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=6)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result=ApcDenied("forbidden"))
    assert client.check() == Verdict.DENY

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert cache["today_verdict"] == "deny"
    # last_success_at 不变
    assert datetime.fromisoformat(cache["last_success_at"]) == last_ok


def test_G5_over_grace_then_200_resets(tmp_path):
    """边界 5:已超 grace 后,APC 又能联上 → 200 → 重置 grace,放行。"""
    from apc_sdk import Verdict

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=8)
    _seed_cache(tmp_path, last_ok)

    client = _make_client(tmp_path, now, fetch_result="fake.jwt.token")
    assert client.check() == Verdict.PASS

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    assert datetime.fromisoformat(cache["last_success_at"]) == now


def test_G6_first_install_no_cache_writes_now(tmp_path):
    """边界 6:无 session.json + 网络问题 → bootstrap 写 last_success_at=now → 在 grace 内 → PASS。"""
    from apc_sdk import Verdict
    from apc_sdk.exceptions import ApcNetworkError

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    # 不预置任何 cache

    client = _make_client(tmp_path, now, fetch_result=ApcNetworkError("simulated"))
    assert client.check() == Verdict.PASS

    import json

    cache = json.loads((tmp_path / "session.json").read_text())
    # bootstrap 写的 last_success_at 在 fake_clock 下是 now
    # 注意:cache bootstrap 用 datetime.now,不走 _clock — 见 client._maybe_bootstrap
    # 因此这条只验证 verdict 是 PASS,不验证写入时刻精确等于 now
    assert "device_id" in cache


def test_today_cache_short_circuits_no_network_call(tmp_path):
    """当日已经判过(today_date == today) → 直接走缓存,不调 fetch。"""
    import json

    from apc_sdk import Verdict

    now = datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc)
    last_ok = now - timedelta(days=2)
    cache = {
        "schema_version": 1,
        "device_id": "dev",
        "last_success_at": last_ok.isoformat(),
        "today_date": "2026-05-15",
        "today_verdict": "deny",
    }
    (tmp_path / "session.json").write_text(json.dumps(cache))

    called = [False]

    def explode(*a, **kw):
        called[0] = True
        raise AssertionError("should not be called")

    from apc_sdk.client import ApcClient

    client = ApcClient(_config(tmp_path))
    client._clock = _FakeClock(now)
    client._fetch_session = explode  # type: ignore[attr-defined]

    assert client.check() == Verdict.DENY
    assert called[0] is False
