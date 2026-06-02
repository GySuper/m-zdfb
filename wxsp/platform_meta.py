"""平台静态元数据登记表 —— 新增平台的单一信息源。

**纯数据,不 import 任何 wxsp 模块**(尤其不 import 平台 adapter),所以
`config` / `browser` / `validator` / `notify` / `setup` 都能安全读它,不产生循环依赖。

分层约定:
- **身份信息(本模块)**:中文名、标题下限、登录检测、向导默认字段、是否需要指纹。
- **行为(`wxsp/platforms/` + `publisher.py`)**:发布步骤 `PlatformSpec` / `login` / Publisher 实例。

加一个平台 = 写 `platforms/X.py` + `X_selectors.py` + 在 `publisher._PUBLISHERS` 注册实例
+ **在本表加 1 条 `PlatformMeta`**。其余文件(config/notify/browser/validator/setup)不再改动。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformMeta:
    """一个平台的静态身份信息。"""

    key: str
    label: str
    """中文名:config 平台标签 + notify 告警头共用。"""
    title_min: int
    """validator 标题最小字数(视频号 16,淘宝光合无限制取 1)。"""
    login_meta: dict[str, str]
    """browser 登录态检测:{home_url, mode: "selector"|"url", selector|login_fragment}。"""
    field_map_defaults: dict[str, str]
    """setup 向导:该平台**特有**字段的飞书中文名默认值(共有字段在 routes_setup 里)。"""
    needs_fingerprint: bool
    """browser 反检测档位:True = 注入 per-account 指纹 + AutomationControlled,靠
    persistent context 持久化 cookie;False = 不注入指纹,额外用 cookies.json 显式持久化。
    当前两平台一一对应,未来若出现别的组合再拆成独立能力位(YAGNI)。"""


REGISTRY: dict[str, PlatformMeta] = {
    "tencent_channel": PlatformMeta(
        key="tencent_channel",
        label="视频号",
        title_min=16,
        login_meta={
            "home_url": "https://channels.weixin.qq.com",
            "mode": "selector",
            "selector": (
                'div:has-text("发表视频"), button:has-text("发表"), ' 'button:has-text("发布视频")'
            ),
        },
        field_map_defaults={
            "tags": "标签",
            "cover": "封面文件",
            "topic": "合集",
            "original_claim": "原创",
        },
        needs_fingerprint=True,
    ),
    "taobao_guanghe": PlatformMeta(
        key="taobao_guanghe",
        label="淘宝光合",
        title_min=1,
        login_meta={
            "home_url": "https://creator.guanghe.taobao.com",
            "mode": "url",
            "login_fragment": "login.taobao.com",
        },
        field_map_defaults={
            "topic": "话题活动",
            "product_ids": "商品ID",
            "declaration": "创作者声明",
            "ai_optimize": "AI优化",
        },
        needs_fingerprint=False,
    ),
}

ALL_PLATFORMS: list[str] = list(REGISTRY)

_FALLBACK = "tencent_channel"


def get_meta(platform: str | None) -> PlatformMeta:
    """取平台 meta;未知/None 回退到 tencent_channel(与历史 .get(.., default) 行为一致)。"""
    return REGISTRY.get(platform or "", REGISTRY[_FALLBACK])
