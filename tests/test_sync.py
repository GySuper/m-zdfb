"""sync.sync_now 单元测试。

happy path 已由 tests/test_cli_sync.py 通过 CliRunner 端到端覆盖
(Task 2 后那些测试 mock 的就是 wxsp.sync.* 而不是 wxsp.cli.*)。这里只锁:
- 飞书 disabled → 返回空 SyncResult,不抛异常
- 返回类型 SyncResult 结构稳定
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import make_settings
from wxsp.sync import SyncResult, sync_now


def test_sync_now_returns_empty_when_feishu_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path)
    # conftest.make_settings 里默认 feishu.enabled=False
    result = sync_now(settings)
    assert isinstance(result, SyncResult)
    assert result.pulled == 0
    assert result.accepted == 0
    assert result.rejected == 0
    assert result.skipped_existing == 0
    assert result.rejected_details == []
