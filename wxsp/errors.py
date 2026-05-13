"""错误类型 + 分类(M5)。"""

from __future__ import annotations

from patchright.sync_api import Error as PWError
from patchright.sync_api import TimeoutError as PWTimeoutError


class PublisherError(Exception):
    """所有 publisher 业务异常的基类。"""


class NasUnreachable(PublisherError):
    """NAS 文件访问失败(M4)。"""


class CookieExpired(PublisherError):
    """登录失效:页面跳到了扫码页或检测到未登录标记。"""


class RiskControl(PublisherError):
    """页面出现风控文案(请稍后/系统繁忙/操作过于频繁/账号异常)。"""


class ElementNotFound(PublisherError):
    """目标元素在超时内未出现 —— 可能视频号改版。"""


class UploadFailed(PublisherError):
    """视频上传中断 / 页面提示上传失败。"""


class NetworkError(PublisherError):
    """网络/导航失败,可重试。"""


class VideoInvalid(PublisherError):
    """视频文件本身有问题(损坏/格式/大小);validator 该拦未拦下的兜底。"""


class UnknownError(PublisherError):
    """兜底类型,classify 返回 'unknown' 时的占位。"""


_KIND_BY_TYPE: dict[type[Exception], str] = {
    CookieExpired: "cookie_expired",
    RiskControl: "risk_control",
    ElementNotFound: "element_not_found",
    UploadFailed: "upload_failed",
    NasUnreachable: "nas_unreachable",
    NetworkError: "network",
    VideoInvalid: "video_invalid",
}


def classify(exc: BaseException) -> str:
    """把异常实例映射到 Task.last_error_type 用的字符串。

    - PublisherError 子类查表
    - patchright TimeoutError → 'element_not_found'(等待元素超时是最常见模式)
    - 其它 patchright Error → 'network'(导航/连接类)
    - 其它任何异常 → 'unknown'
    """
    for cls, kind in _KIND_BY_TYPE.items():
        if isinstance(exc, cls):
            return kind
    if isinstance(exc, PWTimeoutError):
        return "element_not_found"
    if isinstance(exc, PWError):
        return "network"
    return "unknown"
