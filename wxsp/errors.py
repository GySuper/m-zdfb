"""错误类型 + 分类(M4 起逐步填充;M5 加重试装饰器)。"""

from __future__ import annotations


class NasUnreachable(Exception):
    """NAS 文件操作失败。

    在 nas.stage_to_tmp 抛任意 OSError(FileNotFoundError / PermissionError /
    TimeoutError / ConnectionError / 其他 OSError 子类)时统一翻译为此类型。
    M5 retry 装饰器对此异常做 5 次指数退避(5/10/20/40/80s)。
    """
