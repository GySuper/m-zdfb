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
    has_title: bool = True
    """发布页是否有独立标题框。False(快手)= 页面只有作品描述框,标题在飞书侧不必填、
    不校验长度,校验层改判「描述」为必填核心字段。详见 validator._is_incomplete / _check_title。"""
    title_max: int = 30
    """validator 标题最大字数。各平台不同:小红书 20,其余默认 30。"""
    tags_max: int = 5
    """validator 话题/标签最大个数。各平台不同:小红书 10,其余默认 5。"""


REGISTRY: dict[str, PlatformMeta] = {
    "tencent_channel": PlatformMeta(
        key="tencent_channel",
        label="视频号",
        title_min=16,
        login_meta={
            "home_url": "https://channels.weixin.qq.com",
            "mode": "selector",
            "selector": (
                'div:has-text("发表视频"), button:has-text("发表"), button:has-text("发布视频")'
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
    "douyin": PlatformMeta(
        key="douyin",
        label="抖音",
        title_min=1,
        login_meta={
            # 扫码登录走「访问首页 → 扫码后抖音自动跳进 creator-micro/* → 登录文案消失」判定。
            # 不能用上传页专属按钮:扫码成功后抖音落到 creator-micro/home(不是上传页),
            # 那页没有「上传视频」按钮 → 旧 selector 模式永远等不到(对真实页校验,2026-06-03)。
            # 首页(root)未登录 URL 不含 creator-micro、登录后自动跳进去,故片段判定无误判。
            "home_url": "https://creator.douyin.com/",
            "mode": "logged_in_url",
            "logged_in_fragment": "creator.douyin.com/creator-micro/",
            "login_markers": "扫码登录,验证码登录",
        },
        # 抖音用到的非公共字段:标签(→话题标签)、封面。其余 9 个公共字段在 base 集里。
        field_map_defaults={"tags": "标签", "cover": "封面文件"},
        needs_fingerprint=False,
    ),
    "kuaishou": PlatformMeta(
        key="kuaishou",
        label="快手",
        title_min=1,
        login_meta={
            # 实测(2026-06-16):未登录【不跳】passport,而是停在 cp.kuaishou.com 落地页(无上传按钮)。
            # 故按"上传按钮是否出现"判登录态(selector 模式);URL 片段判据会把落地页误判成已登录。
            "home_url": "https://cp.kuaishou.com/article/publish/video",
            "mode": "selector",
            "selector": "button[class^='_upload-btn']",
        },
        # 快手用到的非公共字段:标签(→话题标签)、封面。其余公共字段在 base 集里。
        field_map_defaults={"tags": "标签", "cover": "封面文件"},
        needs_fingerprint=False,
        has_title=False,  # 快手发布页无标题框,主文案是「作品描述」
    ),
    "xiaohongshu": PlatformMeta(
        key="xiaohongshu",
        label="小红书",
        title_min=1,  # 小红书视频标题无最小字数硬限(上限 20,在 adapter 截断)
        title_max=20,  # 小红书视频标题上限 20 字
        tags_max=10,  # 小红书话题最多 10 个
        login_meta={
            # 实测(2026-06-30):未登录 goto 发布页不会稳定重定向到 /login(前端 JS 延迟触发 401),
            # 旧的 url 负面判定「URL 不含 /login = 成功」会在第一轮轮询(2s)就误判成功。
            # 改 logged_in_url 正向判定:goto 登录页,已登录会自动跳创作者中心 /new/*,
            # 未登录停在 /login;URL 进入 /new/ = 登录成功的唯一硬信号。无 login_markers
            # (登录成功跳转是确定性事件,不需要登录文案兜底;_login_markers_visible([]) 恒 False)。
            "home_url": "https://creator.xiaohongshu.com/login",
            "mode": "logged_in_url",
            "logged_in_fragment": "creator.xiaohongshu.com/new/",
        },
        field_map_defaults={"tags": "标签", "cover": "封面文件"},
        needs_fingerprint=False,
    ),
}

ALL_PLATFORMS: list[str] = list(REGISTRY)

_FALLBACK = "tencent_channel"


def get_meta(platform: str | None) -> PlatformMeta:
    """取平台 meta;未知/None 回退到 tencent_channel(与历史 .get(.., default) 行为一致)。"""
    return REGISTRY.get(platform or "", REGISTRY[_FALLBACK])
