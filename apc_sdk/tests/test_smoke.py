"""冒烟测试:确认包能 import,公开类型齐全。"""


def test_imports() -> None:
    from apc_sdk import (
        ApcConfig,
        ApcConfigError,
        ApcDenied,
        ApcError,
        ApcNetworkError,
        Verdict,
    )

    assert Verdict.PASS.value == "pass"
    assert Verdict.DENY.value == "deny"

    # ApcError is the base; the other three derive from it
    assert issubclass(ApcConfigError, ApcError)
    assert issubclass(ApcDenied, ApcError)
    assert issubclass(ApcNetworkError, ApcError)

    # ApcConfig is a dataclass (callable type)
    assert callable(ApcConfig)


def test_apc_config_construction() -> None:
    from pathlib import Path

    from apc_sdk import ApcConfig

    cfg = ApcConfig(
        endpoint="https://example.com",
        app_id="ap_x",
        app_secret="s",
        public_key="-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----",
        cache_dir=Path("/tmp/apc"),
    )
    assert cfg.grace_days == 7
    assert cfg.cert_fingerprint is None
