"""browser_context 启动前清理残留 profile 锁 / 泄漏浏览器。

复现的线上 bug:扫码续期(login)开了 headed 浏览器,用户手动关窗口,patchright
kill EPERM 没杀掉 → Chrome 残留持有 profile 的 SingletonLock → 之后该账号每次 launch
都撞 Chromium 单例(把请求交给旧会话并自身退出)→ patchright TargetClosedError(空白卡住)。

_reap_stale_profile_lock 在 launch 前清掉这种残留。下面用真实子进程 + 真实 SingletonLock
symlink 验证(不 mock),仅 posix(Windows 用互斥量,不是这套 symlink 机制)。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wxsp.browser import _reap_stale_profile_lock

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="SingletonLock symlink 机制仅 posix"
)


def _make_lock(profile: Path, pid: int) -> None:
    """造一份 Chromium 风格的单例锁:SingletonLock 是指向 '<host>-<pid>' 的 symlink。"""
    profile.mkdir(parents=True, exist_ok=True)
    link = profile / "SingletonLock"
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(f"testhost-{pid}", link)
    (profile / "SingletonCookie").write_text("x")
    (profile / "SingletonSocket").write_text("x")


def test_no_lock_is_noop(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    profile.mkdir()
    _reap_stale_profile_lock(profile)  # 不应抛错
    assert not (profile / "SingletonLock").exists()


def test_stale_dead_pid_lock_removed(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    _make_lock(profile, 999999)  # 几乎不可能存活的 pid
    _reap_stale_profile_lock(profile)
    # 陈旧锁应被清掉,让新 launch 干净启动
    assert not (profile / "SingletonLock").exists()
    assert not (profile / "SingletonCookie").exists()
    assert not (profile / "SingletonSocket").exists()


def test_live_unrelated_pid_not_killed_but_lock_cleared(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    # 活着的进程,命令行【不含】该 profile 路径(模拟 PID 复用成了别的程序)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _make_lock(profile, proc.pid)
        _reap_stale_profile_lock(profile)
        time.sleep(0.3)
        assert proc.poll() is None, "不该误杀命令行不含本 profile 的无关进程"
        assert not (profile / "SingletonLock").exists()  # 锁仍清掉
    finally:
        proc.kill()
        proc.wait()


def test_live_matching_browser_is_reaped(tmp_path: Path) -> None:
    profile = tmp_path / "prof"
    profile.mkdir(parents=True, exist_ok=True)
    # 活着的进程,命令行【包含】该 profile 绝对路径(模拟泄漏的 Chrome)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", str(profile.resolve())]
    )
    try:
        _make_lock(profile, proc.pid)
        _reap_stale_profile_lock(profile)
        for _ in range(30):  # 给 SIGKILL 一点时间
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "应清理命令行包含本 profile 的残留浏览器"
        assert not (profile / "SingletonLock").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
