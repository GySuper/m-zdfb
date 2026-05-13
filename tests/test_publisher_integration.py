"""手动集成测试 —— 用真账号 + 真视频文件 + dry_run 跑一遍 M5 全流程。

跑法:
  1. config.yaml 配好 account_a 的 user_data_dir + 视频/封面搜索根
  2. NAS 上放一个测试视频(例如 m5_test.mp4)
  3. 飞书侧新建一行(账号=account_a, 视频文件=m5_test.mp4, 定时发布时间=now+2h,
     执行日期=today, 状态=待入库) → `uv run wxsp sync`
  4. 找到入库后的 task_id:
       sqlite3 data/db.sqlite "SELECT id, video_id, status FROM task ORDER BY id DESC LIMIT 1;"
  5. 跑:`WXSP_M5_TEST_TASK_ID=<id> uv run pytest tests/test_publisher_integration.py -v -m integration -s`
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wxsp.config import load_settings
from wxsp.publisher import publish

TASK_ID_ENV = "WXSP_M5_TEST_TASK_ID"


@pytest.mark.integration
def test_dry_run_full_flow_on_real_account() -> None:
    task_id_str = os.environ.get(TASK_ID_ENV)
    if not task_id_str:
        pytest.skip(f"set ${TASK_ID_ENV}=<pending task id> to run")
    task_id = int(task_id_str)

    settings = load_settings()
    result = publish(task_id, dry_run=True, settings=settings)

    assert result.ok, f"dry_run 失败: {result.error_type} {result.error_msg}"
    assert result.dry_run is True
    assert result.screenshots, "dry_run gate 必须截图"
    for p in result.screenshots:
        assert Path(p).exists(), f"截图文件缺失: {p}"
