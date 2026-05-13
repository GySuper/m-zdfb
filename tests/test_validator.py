"""wxsp.validator types and rules."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from wxsp.config import (
    AppConfig,
    FeishuBitableConfig,
    FeishuConfig,
    FeishuFieldMap,
    FeishuSyncConfig,
    MonitoringConfig,
    NotifiersConfig,
    PathsConfig,
    PublisherConfig,
    SchedulerConfig,
    Settings,
    WebUIConfig,
    WecomNotifierConfig,
)
from wxsp.feishu import BitableRow
from wxsp.validator import FieldError, NasFinder, ValidationResult


def test_field_error_is_frozen() -> None:
    err = FieldError(field="标题", message="12 字(要求 16-30 字)")
    assert err.field == "标题"
    assert err.message == "12 字(要求 16-30 字)"
    with pytest.raises(Exception):  # noqa: B017  # FrozenInstanceError or AttributeError
        err.field = "x"  # type: ignore[misc]


def test_validation_result_ok_shape() -> None:
    result = ValidationResult(ok=True, title="abc" * 6)
    assert result.ok is True
    assert result.errors == []  # 默认空 list


def test_validation_result_fail_shape() -> None:
    result = ValidationResult(
        ok=False,
        errors=[FieldError(field="标题", message="12 字")],
    )
    assert result.ok is False
    assert len(result.errors) == 1


def test_nas_finder_is_protocol() -> None:
    """NasFinder 是 Protocol,任何提供 find_video/find_cover 的对象都满足。"""

    class _Stub:
        def find_video(self, name: str) -> Path:
            return Path("/dev/null")

        def find_cover(self, name: str) -> Path:
            return Path("/dev/null")

    finder: NasFinder = _Stub()
    assert finder.find_video("x").as_posix() == "/dev/null"


# ---------------------------------------------------------------------------
# Task 7: text/select/account rules
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path) -> Settings:
    """构造一个合法的 Settings,validator 实际只用到 feishu.field_map 和 paths。"""
    return Settings(
        app=AppConfig(
            data_dir=tmp_path / "data", logs_dir=tmp_path / "logs", timezone="Asia/Shanghai"
        ),
        paths=PathsConfig(
            nas_root=tmp_path / "nas",
            video_search_root=tmp_path / "nas" / "videos",
            cover_search_root=tmp_path / "nas" / "covers",
        ),
        accounts={},
        scheduler=SchedulerConfig(),
        publisher=PublisherConfig(),
        feishu=FeishuConfig(
            app_id="x",
            app_secret="y",
            bitable=FeishuBitableConfig(app_token="t", table_id="t"),
            field_map=FeishuFieldMap(),
            sync=FeishuSyncConfig(),
        ),
        monitoring=MonitoringConfig(
            notifiers=NotifiersConfig(wecom=WecomNotifierConfig(webhook="http://x")),
        ),
        webui=WebUIConfig(),
    )


class _StubNas:
    """默认 stub:find_video / find_cover 都抛 FileNotFoundError;按需填 *_returns。"""

    def __init__(self) -> None:
        self.video_returns: dict[str, Path] = {}
        self.cover_returns: dict[str, Path] = {}

    def find_video(self, filename: str) -> Path:
        if filename in self.video_returns:
            return self.video_returns[filename]
        raise FileNotFoundError(filename)

    def find_cover(self, filename: str) -> Path:
        if filename in self.cover_returns:
            return self.cover_returns[filename]
        raise FileNotFoundError(filename)


def _date_to_feishu_ms(d: date) -> int:
    """date 转飞书返回的 ms timestamp(UTC 0 点)。"""
    from datetime import timezone

    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _datetime_to_feishu_ms(dt: datetime) -> int:
    """naive Asia/Shanghai datetime → 飞书返回的 ms timestamp(UTC ms)。"""
    from datetime import timedelta, timezone

    utc_dt = dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    return int(utc_dt.timestamp() * 1000)


def _make_happy_row(tmp_path: Path) -> BitableRow:
    """构造一个字段填好的行;title 故意 12 字(不合规),子测试按需 override。"""
    return BitableRow(
        record_id="rec_happy",
        fields={
            "标题": "这是一个测试标题视频内容",  # 12 字,不合规 —— 有意为之
            "描述": "测试描述",
            "标签": [{"text": "标签1"}, {"text": "标签2"}],
            "封面文件": "",
            "合集": "测试合集",
            "原创": True,
            "账号": "account_a",
            "执行日期": _date_to_feishu_ms(date(2026, 5, 13)),
            "定时发布时间": _datetime_to_feishu_ms(datetime(2026, 5, 13, 14, 0)),
            "视频文件": "国庆01.mp4",
            "状态": "待入库",
        },
    )


def _row_with(tmp_path: Path, **overrides: object) -> BitableRow:
    """基于 happy_row,覆盖指定字段,返回新 BitableRow。"""
    base = _make_happy_row(tmp_path)
    fields = dict(base.fields)
    fields.update(overrides)
    return BitableRow(record_id=base.record_id, fields=fields)


def _stub_nas_with_video(tmp_path: Path) -> _StubNas:
    """创建带有 国庆01.mp4 的 stub NAS。"""
    video_path = tmp_path / "x.mp4"
    video_path.write_bytes(b"x")
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = video_path
    return nas


from wxsp.validator import validate  # noqa: E402


def test_validate_title_too_short(tmp_path: Path) -> None:
    row = _row_with(tmp_path, 标题="短标题")  # 3 字
    nas = _stub_nas_with_video(tmp_path)
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "标题" and "16" in e.message for e in result.errors)


def test_validate_title_too_long(tmp_path: Path) -> None:
    row = _row_with(tmp_path, 标题="字" * 31)
    nas = _stub_nas_with_video(tmp_path)
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "标题" and "30" in e.message for e in result.errors)


def test_validate_title_boundary_16_passes(tmp_path: Path) -> None:
    row = _row_with(tmp_path, 标题="字" * 16)
    nas = _stub_nas_with_video(tmp_path)
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert all(e.field != "标题" for e in result.errors), result.errors


def test_validate_tags_too_many(tmp_path: Path) -> None:
    row = _row_with(tmp_path, 标题="字" * 16, 标签=[{"text": f"t{i}"} for i in range(6)])
    nas = _stub_nas_with_video(tmp_path)
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "标签" and "5" in e.message for e in result.errors)


def test_validate_account_empty(tmp_path: Path) -> None:
    row = _row_with(tmp_path, 标题="字" * 16, 账号="")
    nas = _stub_nas_with_video(tmp_path)
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "账号" and "未指定" in e.message for e in result.errors)


def test_validate_account_not_in_active_set(tmp_path: Path) -> None:
    row = _row_with(tmp_path, 标题="字" * 16, 账号="account_unknown")
    nas = _stub_nas_with_video(tmp_path)
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "账号" and "account_unknown" in e.message for e in result.errors)
