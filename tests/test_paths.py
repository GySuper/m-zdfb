"""测试 config.py 的路径解析:打包模式 vs 开发模式。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from wxsp.config import (
    get_config_path,
    get_user_data_dir,
    get_user_logs_dir,
    is_packaged,
)


def test_is_packaged_default_dev_mode() -> None:
    """普通 pytest 运行下,__main__ 没有 __compiled__ 属性 → 开发模式。"""
    assert is_packaged() is False


def test_is_packaged_when_nuitka_compiled() -> None:
    """Nuitka 会给 sys.modules['__main__'] 注入 __compiled__ → True。"""
    main_module = sys.modules["__main__"]
    with patch.object(main_module, "__compiled__", True, create=True):
        assert is_packaged() is True


def test_is_packaged_force_dev_via_env(monkeypatch) -> None:
    """WXSP_DEV_MODE=1 强制开发模式,即使被 Nuitka 编译。"""
    main_module = sys.modules["__main__"]
    monkeypatch.setenv("WXSP_DEV_MODE", "1")
    with patch.object(main_module, "__compiled__", True, create=True):
        assert is_packaged() is False


def test_user_data_dir_dev_mode() -> None:
    """开发模式下 user_data_dir 返回项目根的 ./data。"""
    assert get_user_data_dir() == Path("./data").resolve()


def test_user_data_dir_packaged_uses_platformdirs(monkeypatch) -> None:
    """打包模式走 platformdirs(mac: ~/Library/Application Support/wxsp)。"""
    main_module = sys.modules["__main__"]
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        result = get_user_data_dir()
        assert result.is_absolute()
        assert result.name == "data"
        assert result.parent.name == "wxsp"


def test_user_logs_dir_dev_mode() -> None:
    assert get_user_logs_dir() == Path("./logs").resolve()


def test_config_path_dev_mode() -> None:
    """开发模式 config.yaml 在项目根。"""
    assert get_config_path() == Path("./config.yaml").resolve()


def test_config_path_packaged(monkeypatch) -> None:
    """打包模式 config.yaml 在用户数据目录(get_user_data_dir().parent)。"""
    main_module = sys.modules["__main__"]
    with patch.object(main_module, "__compiled__", True, create=True):
        monkeypatch.delenv("WXSP_DEV_MODE", raising=False)
        config_path = get_config_path()
        data_dir = get_user_data_dir()
        assert config_path.parent == data_dir.parent
        assert config_path.name == "config.yaml"
