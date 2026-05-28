"""Web UI 中文化:状态 / Cookie / 日志级别 等枚举的中文展示。

DB / API / config.yaml 仍用英文 key(避免污染数据层),只在渲染层做映射。
"""

from __future__ import annotations

STATUS_CN: dict[str, str] = {
    "pending": "待发布",
    "running": "发布中",
    "success": "已发布",
    "failed": "失败",
    "skipped": "已跳过",
    "interrupted": "已中断",
}

COOKIE_CN: dict[str, str] = {
    "ok": "正常",
    "warn": "即将过期",
    "expired": "已过期",
    "unknown": "未知",
}

LEVEL_CN: dict[str, str] = {
    "info": "信息",
    "warn": "警告",
    "error": "错误",
}

ENABLED_CN: dict[bool, str] = {True: "启用", False: "禁用"}

# 错误类型映射(notify.py 里发的 5 类 + publisher 里抛的几种)
ERROR_TYPE_CN: dict[str, str] = {
    "cookie_expired": "Cookie 失效",
    "risk_control": "风控触发",
    "element_not_found": "元素未找到",
    "task_failed": "任务失败",
    "nas_unreachable": "NAS 不可达",
    "network": "网络异常",
    "video_invalid": "视频非法",
    "upload_failed": "上传失败",
    "feishu_api_error": "飞书 API 错误",
    "unknown": "未知错误",
    "cookie_warning": "Cookie 即将过期",
    "backlog_high": "历史积压超阈值",
}


def status_cn(value: str) -> str:
    return STATUS_CN.get(value, value)


def cookie_cn(value: str) -> str:
    return COOKIE_CN.get(value, value)


def level_cn(value: str) -> str:
    return LEVEL_CN.get(value, value)


def enabled_cn(value: bool) -> str:
    return ENABLED_CN.get(value, "未知")


PLATFORM_CN: dict[str, str] = {
    "tencent_channel": "视频号",
    "taobao_guanghe": "淘宝光合",
}


def error_type_cn(value: str | None) -> str:
    if not value:
        return ""
    return ERROR_TYPE_CN.get(value, value)


def platform_cn(value: str) -> str:
    return PLATFORM_CN.get(value, value)
