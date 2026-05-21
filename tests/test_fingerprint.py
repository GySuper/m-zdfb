"""Tests for wxsp.fingerprint: deterministic per-account fingerprint generation.

关键不变量:
  - 同 account_id → 同指纹(否则视频号判定"同账号换设备",踢登录)
  - 不同 account_id → 必然不同的关键字段(canvas / UA / WebGL,否则风控等价)
  - 落盘后能 round-trip(JSON schema 稳定)
  - init script 是合法 JS 字符串、把 account 字段嵌入了进去
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from wxsp.fingerprint import (
    USER_AGENTS,
    Fingerprint,
    context_options,
    generate_fingerprint,
    get_or_create_fingerprint,
    init_script,
)


def test_generate_fingerprint_is_deterministic() -> None:
    """同 account_id 两次生成必须完全相等 —— 视频号会因指纹漂移踢登录。"""
    fp1 = generate_fingerprint("account_abc")
    fp2 = generate_fingerprint("account_abc")
    assert fp1.to_dict() == fp2.to_dict()


def test_different_accounts_produce_different_critical_fields() -> None:
    """风控的关键差异点必须确实有差异:canvas_noise / UA-or-WebGL / hardware。

    要求 4 个账号里至少 3 套不同的 canvas_noise(种子撞库概率极低,实测应该 4/4)。
    """
    ids = ["account_a", "account_b", "account_c", "account_d"]
    fps = [generate_fingerprint(i) for i in ids]

    canvas_noises = {fp.canvas_noise for fp in fps}
    assert len(canvas_noises) >= 3, f"canvas_noise 撞库严重: {canvas_noises}"

    device_ids = {fp.device_id for fp in fps}
    assert len(device_ids) == 4, "device_id 必须 100% 唯一"

    # UA / WebGL / hardware 至少有一个字段在不同账号上不一样
    profiles = {(fp.user_agent, fp.webgl_renderer, fp.hardware_concurrency) for fp in fps}
    assert len(profiles) >= 2, "UA / WebGL / hardware 全撞了,等于没指纹差异"


def test_generate_fingerprint_fields_are_well_formed() -> None:
    fp = generate_fingerprint("acc_1")
    assert fp.user_agent in USER_AGENTS
    assert "Chrome/" in fp.user_agent
    assert fp.viewport["width"] > 0 and fp.viewport["height"] > 0
    assert fp.viewport["height"] == fp.screen["height"] - 100
    assert fp.timezone == "Asia/Shanghai"
    assert fp.language == "zh-CN"
    assert "zh-CN" in fp.languages
    assert fp.platform == "Win32"
    assert re.fullmatch(r"[A-Za-z0-9]{32}", fp.canvas_noise)
    assert 0.0001 <= fp.audio_noise <= 0.001
    assert 10 <= len(fp.fonts) <= 16
    assert fp.fonts == sorted(fp.fonts)
    assert fp.hardware_concurrency in {4, 6, 8, 12, 16}
    assert fp.device_memory in {4, 8, 16}
    assert len(fp.client_hints["brands"]) >= 1


def test_get_or_create_persists_to_disk_and_reloads(tmp_path: Path) -> None:
    """首次生成 → 写 JSON;第二次读 JSON,内容一致。"""
    storage = tmp_path / "fingerprints"
    fp1 = get_or_create_fingerprint("acc_xyz", storage)

    fp_file = storage / "acc_xyz.json"
    assert fp_file.exists()
    on_disk = json.loads(fp_file.read_text(encoding="utf-8"))
    assert on_disk["account_id"] == "acc_xyz"
    assert on_disk["canvas_noise"] == fp1.canvas_noise

    fp2 = get_or_create_fingerprint("acc_xyz", storage)
    assert fp1.to_dict() == fp2.to_dict()


def test_get_or_create_rebuilds_when_file_corrupted(tmp_path: Path) -> None:
    """损坏的 JSON 不应该让账号永久无法启动 —— 按种子重建,保证确定性。"""
    storage = tmp_path / "fingerprints"
    storage.mkdir()
    (storage / "acc_broken.json").write_text("not-json{", encoding="utf-8")

    fp = get_or_create_fingerprint("acc_broken", storage)
    expected = generate_fingerprint("acc_broken")
    assert fp.to_dict() == expected.to_dict()  # 按种子重建,跟首次一样


def test_context_options_returns_playwright_kwargs() -> None:
    fp = generate_fingerprint("acc_q")
    opts = context_options(fp)
    assert opts["user_agent"] == fp.user_agent
    assert opts["timezone_id"] == "Asia/Shanghai"
    assert opts["locale"] == "zh-CN"
    assert opts["viewport"]["width"] == fp.viewport["width"]
    assert opts["screen"]["width"] == fp.screen["width"]
    assert opts["is_mobile"] is False
    assert opts["has_touch"] is False


def test_init_script_embeds_fingerprint_values() -> None:
    """init script 必须含本账号的关键字段,否则注入了也白注入。"""
    fp = generate_fingerprint("acc_init")
    script = init_script(fp)

    # 关键字段都得在 JS 字面里出现
    assert fp.canvas_noise in script
    assert fp.webgl_vendor in script
    assert fp.webgl_renderer in script
    assert str(fp.hardware_concurrency) in script
    assert str(fp.device_memory) in script

    # 基本 JS 结构(IIFE + 主要 hook 点)
    assert script.lstrip().startswith("(() => {")
    assert "'webdriver'" in script  # Object.defineProperty(navigator, 'webdriver', ...)
    assert "WebGLRenderingContext.prototype.getParameter" in script
    assert "HTMLCanvasElement.prototype.toDataURL" in script
    assert "AudioBuffer.prototype.getChannelData" in script
    assert "RTCPeerConnection" in script
    assert "userAgentData" in script


def test_init_script_unique_per_account() -> None:
    """两个账号注入的 JS 必须不一样,否则风控视角下还是同设备。"""
    s_a = init_script(generate_fingerprint("acc_1"))
    s_b = init_script(generate_fingerprint("acc_2"))
    assert s_a != s_b


def test_fingerprint_round_trip_dict() -> None:
    fp = generate_fingerprint("acc_rt")
    restored = Fingerprint.from_dict(fp.to_dict())
    assert restored.to_dict() == fp.to_dict()


@pytest.mark.parametrize("account_id", ["a", "ab", "account_e88a6397", "中文账号"])
def test_get_or_create_handles_various_account_id_shapes(tmp_path: Path, account_id: str) -> None:
    storage = tmp_path / "fp"
    fp = get_or_create_fingerprint(account_id, storage)
    assert fp.account_id == account_id
    assert (storage / f"{account_id}.json").exists()
