"""SDK 异常类型。"""


class ApcError(Exception):
    """基类,接入方可以一次 catch 所有 SDK 异常。"""


class ApcConfigError(ApcError):
    """构造时配置非法(空字符串、文件不存在等)。"""


class ApcDenied(ApcError):
    """APC 服务端明确拒绝(4xx)。client.check() 翻译成 Verdict.DENY。"""


class ApcNetworkError(ApcError):
    """网络问题:连不上 / 超时 / TLS 失败 / 5xx。client.check() 走 grace 逻辑。"""
