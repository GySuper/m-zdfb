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


# ---------------------------------------------------------------------------
# Task 8: video_file / cover file rules
# ---------------------------------------------------------------------------


def test_validate_video_file_not_found(tmp_path: Path) -> None:
    row = _row_with(tmp_path, 标题="字" * 16, **{"视频文件": "missing.mp4"})
    nas = _StubNas()  # 不预置 video_returns → 抛 FileNotFoundError
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "视频文件" and "未在" in e.message for e in result.errors)


def test_validate_video_file_wrong_extension(tmp_path: Path) -> None:
    bad = tmp_path / "bad.avi"
    bad.write_bytes(b"x")
    row = _row_with(tmp_path, 标题="字" * 16, **{"视频文件": "bad.avi"})
    nas = _StubNas()
    nas.video_returns["bad.avi"] = bad
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "视频文件" and ".avi" in e.message for e in result.errors)


def test_validate_video_extension_case_insensitive(tmp_path: Path) -> None:
    upper = tmp_path / "x.MP4"
    upper.write_bytes(b"x")
    row = _row_with(tmp_path, 标题="字" * 16, **{"视频文件": "x.MP4"})
    nas = _StubNas()
    nas.video_returns["x.MP4"] = upper
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    # 不关心整体 ok(时间 rules 还没做);只关心视频文件这一项不报错
    assert all(e.field != "视频文件" for e in result.errors), result.errors


def test_validate_video_too_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    big = tmp_path / "big.mp4"
    big.write_bytes(b"x")

    class _FakeStat:
        st_size = 5 * 1024**3
        st_mtime = 1.0

    monkeypatch.setattr(Path, "stat", lambda self, **kwargs: _FakeStat())

    row = _row_with(tmp_path, 标题="字" * 16, **{"视频文件": "big.mp4"})
    nas = _StubNas()
    nas.video_returns["big.mp4"] = big
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "视频文件" and "GiB" in e.message for e in result.errors)


def test_validate_cover_missing(tmp_path: Path) -> None:
    video = tmp_path / "国庆01.mp4"
    video.write_bytes(b"x")
    row = _row_with(tmp_path, 标题="字" * 16, **{"封面文件": "missing.jpg"})
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = video
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "封面文件" and "missing.jpg" in e.message for e in result.errors)


def test_validate_cover_empty_is_ok(tmp_path: Path) -> None:
    video = tmp_path / "国庆01.mp4"
    video.write_bytes(b"x")
    row = _row_with(tmp_path, 标题="字" * 16, **{"封面文件": ""})
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = video
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    # 封面字段不应该报错
    assert all(e.field != "封面文件" for e in result.errors), result.errors


# ---------------------------------------------------------------------------
# Task 9: execute_date / publish_at rules + happy path
# ---------------------------------------------------------------------------


def test_validate_execute_date_missing(tmp_path: Path) -> None:
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["执行日期"] = None
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "国庆01.mp4"
    (tmp_path / "国庆01.mp4").write_bytes(b"x")
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "执行日期" for e in result.errors)


def test_validate_publish_at_too_close(tmp_path: Path) -> None:
    # publish_at = now + 29 分钟 → 早于 now+30min
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["定时发布时间"] = _datetime_to_feishu_ms(datetime(2026, 5, 12, 9, 29))
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "国庆01.mp4"
    (tmp_path / "国庆01.mp4").write_bytes(b"x")
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "定时发布时间" and "30min" in e.message for e in result.errors)


def test_validate_publish_at_too_far(tmp_path: Path) -> None:
    # publish_at = now + 14 天 + 1 分钟 → 超出 now+14d 上限
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["定时发布时间"] = _datetime_to_feishu_ms(datetime(2026, 5, 26, 9, 1))
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    nas.video_returns["国庆01.mp4"] = tmp_path / "国庆01.mp4"
    (tmp_path / "国庆01.mp4").write_bytes(b"x")
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "定时发布时间" and "14d" in e.message for e in result.errors)


def test_validate_publish_at_boundary_30min_passes(tmp_path: Path) -> None:
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["执行日期"] = _date_to_feishu_ms(date(2026, 5, 12))  # 与 publish_at 同天
    fields["定时发布时间"] = _datetime_to_feishu_ms(datetime(2026, 5, 12, 9, 30))
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    video = tmp_path / "国庆01.mp4"
    video.write_bytes(b"x")
    nas.video_returns["国庆01.mp4"] = video
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert all(e.field != "定时发布时间" for e in result.errors), result.errors


def test_validate_publish_date_earlier_than_execute_date(tmp_path: Path) -> None:
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "字" * 16
    fields["执行日期"] = _date_to_feishu_ms(date(2026, 5, 14))
    fields["定时发布时间"] = _datetime_to_feishu_ms(
        datetime(2026, 5, 13, 14, 0)
    )  # 早于 execute_date
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()
    video = tmp_path / "国庆01.mp4"
    video.write_bytes(b"x")
    nas.video_returns["国庆01.mp4"] = video
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    assert any(e.field == "定时发布时间" and "早于执行日期" in e.message for e in result.errors)


def test_validate_multi_error_collection(tmp_path: Path) -> None:
    """同时 3 个字段错 → errors 长度 == 3。"""
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "短"  # 1 字
    fields["视频文件"] = "missing.mp4"
    fields["账号"] = ""
    row = BitableRow(record_id="r", fields=fields)
    nas = _StubNas()  # 不预置 → 找不到
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    assert result.ok is False
    error_fields = {e.field for e in result.errors}
    assert {"标题", "视频文件", "账号"} <= error_fields


def test_validate_happy_path(tmp_path: Path) -> None:
    """所有字段都合规 → ok=True,全部 attribute 填充。"""
    fields = dict(_make_happy_row(tmp_path).fields)
    fields["标题"] = "这是一个测试标题视频内容十八字符"  # 16 字
    row = BitableRow(record_id="rec_happy", fields=fields)

    settings = _make_settings(tmp_path)
    nas = _StubNas()
    video_path = tmp_path / "国庆01.mp4"
    video_path.write_bytes(b"x" * 100)
    nas.video_returns["国庆01.mp4"] = video_path

    result = validate(
        row,
        config=settings,
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a", "account_b"},
    )
    assert result.ok is True, result.errors
    assert result.title == "这是一个测试标题视频内容十八字符"
    assert result.description == "测试描述"
    assert result.tags == ["标签1", "标签2"]
    assert result.topic == "测试合集"
    assert result.original_claim is True
    assert result.account_id == "account_a"
    assert result.video_path == video_path
    assert result.cover_path is None
    assert result.execute_date == date(2026, 5, 13)
    assert result.publish_at == datetime(2026, 5, 13, 14, 0)  # naive 上海时间


def test_validate_video_file_auto_appends_mp4(tmp_path: Path) -> None:
    """运营只填 'test' 不带扩展名 → 工具自动按 'test.mp4' 找。"""
    video = tmp_path / "test.mp4"
    video.write_bytes(b"x")
    row = _row_with(tmp_path, **{"视频文件": "test"})
    nas = _StubNas()
    nas.video_returns["test.mp4"] = video
    result = validate(
        row,
        config=_make_settings(tmp_path),
        now=datetime(2026, 5, 12, 9, 0),
        nas_finder=nas,
        active_account_ids={"account_a"},
    )
    # 不关心整体 ok(时间 rules 与 happy_row 默认值的交互),只关心视频字段不报错
    assert all(e.field != "视频文件" for e in result.errors), result.errors
