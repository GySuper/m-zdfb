"""wxsp/apc.py 粘合层。重点验证 dev-mode 短路 + 异常 fail-open。"""

from unittest.mock import MagicMock, patch


def test_is_dev_mode_when_not_packaged(monkeypatch):
    """开发模式 = 未打包 = is_dev_mode() True。"""
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
    # 默认 pytest 跑在源码模式,is_packaged 应是 False
    from wxsp import apc

    assert apc.is_dev_mode() is True


def test_check_pass_dev_mode_returns_true_without_network(monkeypatch):
    """dev-mode 下 check_pass 直接 True,不应触发 ApcClient 实例化。"""
    monkeypatch.delenv("WXSP_DEV_MODE", raising=False)

    # 让 _client() 一旦被调到就爆炸,确保 dev-mode 不到这里
    with patch("wxsp.apc._client", side_effect=AssertionError("should not be called")):
        from wxsp import apc

        assert apc.check_pass() is True


def test_check_pass_packaged_calls_client(monkeypatch):
    """打包模式下调 ApcClient.check;返回 Verdict.PASS → True。"""
    monkeypatch.setattr("wxsp.config.is_packaged", lambda: True)

    fake_client = MagicMock()
    from apc_sdk import Verdict

    fake_client.check.return_value = Verdict.PASS

    with patch("wxsp.apc._client", return_value=fake_client):
        # 强制刷新 _client_singleton,因为之前 dev-mode 没建过
        import wxsp.apc as apc_mod

        apc_mod._client_singleton = None
        assert apc_mod.check_pass() is True
        fake_client.check.assert_called_once()


def test_check_pass_packaged_deny_returns_false(monkeypatch):
    monkeypatch.setattr("wxsp.config.is_packaged", lambda: True)

    fake_client = MagicMock()
    from apc_sdk import Verdict

    fake_client.check.return_value = Verdict.DENY

    with patch("wxsp.apc._client", return_value=fake_client):
        import wxsp.apc as apc_mod

        apc_mod._client_singleton = None
        assert apc_mod.check_pass() is False


def test_check_pass_packaged_exception_fail_open(monkeypatch, caplog):
    """SDK 内部 raise 时 fail-open(spec §3.2):返回 True + log warning。"""
    monkeypatch.setattr("wxsp.config.is_packaged", lambda: True)

    fake_client = MagicMock()
    fake_client.check.side_effect = RuntimeError("SDK internal bug")

    with patch("wxsp.apc._client", return_value=fake_client):
        import wxsp.apc as apc_mod

        apc_mod._client_singleton = None
        assert apc_mod.check_pass() is True
