"""Per-account 浏览器设备指纹生成 + 注入。

设计目标:让 4 个账号在同一台机器上跑时,视频号看到的是 4 套不同的设备指纹
(UA / 屏幕 / 时区 / WebGL / Canvas / Audio / 字体 / Client Hints 等),从而绕过
"同设备多账号" 的风控判定。

设计要点:
  - **确定性**:同一个 `account_id` 永远生成同一套指纹(种子 = MD5(account_id))。
    避免每次启动浏览器都换指纹 → 视频号反而会把它当成"同账号换设备"踢登录。
  - **持久化**:首次生成后写入 `{user_data_dir_root}/fingerprints/{account_id}.json`,
    后续启动从 JSON 读取。文件丢了能从种子重建。
  - **应用方式**:
    - 部分字段通过 Playwright context options 应用(user_agent / viewport / locale / timezone)
    - 其余字段通过 `add_init_script` 注入 JS,覆写 navigator / WebGL / Canvas / Audio
      / Client Hints / fonts / mediaDevices 等 API。
  - **不动 publisher.py / 选择器**:fingerprint 是 browser_context 启动时的一次性注入,
    对 publisher 步骤透明。

参考:SynapseAutomation/syn_backend/myUtils/device_fingerprint.py
"""

from __future__ import annotations

import hashlib
import json
import random
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

# ---------------- 指纹候选池(种子从池里挑一个,保证多账号差异化) ----------------

USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

SCREEN_RESOLUTIONS: list[dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 2560, "height": 1440},
    {"width": 1536, "height": 864},
    {"width": 1680, "height": 1050},
]

WEBGL_VENDORS: list[tuple[str, str]] = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (Intel)", "ANGLE (Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (Intel)", "ANGLE (Intel(R) HD Graphics 620 Direct3D11 vs_5_0 ps_5_0)"),
    ("Google Inc. (AMD)", "ANGLE (AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0)"),
]

HARDWARE_CONCURRENCY_POOL: list[int] = [4, 6, 8, 12, 16]
DEVICE_MEMORY_POOL: list[int] = [4, 8, 16]

FONT_LIST: list[str] = [
    "Arial",
    "Arial Black",
    "Calibri",
    "Cambria",
    "Cambria Math",
    "Comic Sans MS",
    "Consolas",
    "Courier New",
    "Georgia",
    "Helvetica",
    "Impact",
    "Lucida Console",
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "Microsoft Sans Serif",
    "Segoe UI",
    "Segoe UI Emoji",
    "Segoe UI Symbol",
    "Tahoma",
    "Times New Roman",
    "Trebuchet MS",
    "Verdana",
]

PLUGINS: list[dict[str, str]] = [
    {
        "name": "PDF Viewer",
        "filename": "internal-pdf-viewer",
        "description": "Portable Document Format",
    },
    {
        "name": "Chrome PDF Viewer",
        "filename": "internal-pdf-viewer",
        "description": "Portable Document Format",
    },
    {
        "name": "Chromium PDF Viewer",
        "filename": "internal-pdf-viewer",
        "description": "Portable Document Format",
    },
    {
        "name": "Microsoft Edge PDF Viewer",
        "filename": "internal-pdf-viewer",
        "description": "Portable Document Format",
    },
    {
        "name": "WebKit built-in PDF",
        "filename": "internal-pdf-viewer",
        "description": "Portable Document Format",
    },
]

MIME_TYPES: list[dict[str, str]] = [
    {"type": "application/pdf", "suffixes": "pdf", "description": "Portable Document Format"},
    {"type": "text/pdf", "suffixes": "pdf", "description": "Portable Document Format"},
]

MEDIA_DEVICES: list[dict[str, str]] = [
    {"kind": "audioinput", "label": "Microphone (Realtek(R) Audio)", "groupId": "audio-1"},
    {"kind": "audiooutput", "label": "Speakers (Realtek(R) Audio)", "groupId": "audio-1"},
    {"kind": "videoinput", "label": "Integrated Camera", "groupId": "video-1"},
]


# ---------------- 数据模型 ----------------


@dataclass
class Fingerprint:
    """单账号的完整指纹。所有字段均为确定性派生,种子来自 account_id。"""

    account_id: str
    device_id: str
    user_agent: str
    chrome_version: str
    viewport: dict[str, int]
    screen: dict[str, int]
    timezone: str
    language: str
    languages: list[str]
    platform: str
    webgl_vendor: str
    webgl_renderer: str
    canvas_noise: str
    audio_noise: float
    hardware_concurrency: int
    device_memory: int
    color_depth: int
    pixel_depth: int
    fonts: list[str]
    plugins: list[dict[str, str]]
    mime_types: list[dict[str, str]]
    media_devices: list[dict[str, str]]
    client_hints: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "device_id": self.device_id,
            "user_agent": self.user_agent,
            "chrome_version": self.chrome_version,
            "viewport": self.viewport,
            "screen": self.screen,
            "timezone": self.timezone,
            "language": self.language,
            "languages": self.languages,
            "platform": self.platform,
            "webgl_vendor": self.webgl_vendor,
            "webgl_renderer": self.webgl_renderer,
            "canvas_noise": self.canvas_noise,
            "audio_noise": self.audio_noise,
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
            "color_depth": self.color_depth,
            "pixel_depth": self.pixel_depth,
            "fonts": self.fonts,
            "plugins": self.plugins,
            "mime_types": self.mime_types,
            "media_devices": self.media_devices,
            "client_hints": self.client_hints,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fingerprint:
        return cls(**data)


# ---------------- 生成 ----------------


def _seeded_rng(account_id: str, salt: str = "") -> random.Random:
    """从 account_id 派生确定性随机数生成器。salt 用来在同账号内派生不同子流(避免互相耦合)。"""
    seed_str = f"{account_id}::{salt}"
    seed_int = int(hashlib.md5(seed_str.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed_int)


def _extract_chrome_version(user_agent: str) -> str:
    import re

    match = re.search(r"Chrome/([0-9.]+)", user_agent)
    return match.group(1) if match else "120.0.0.0"


def _build_client_hints(chrome_version: str) -> dict[str, Any]:
    major = chrome_version.split(".")[0]
    return {
        "brands": [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            {"brand": "Not A(Brand", "version": "99"},
        ],
        "mobile": False,
        "platform": "Windows",
        "ua_full_version": chrome_version,
        "platform_version": "10.0.0",
        "architecture": "x86",
        "model": "",
        "bitness": "64",
        "wow64": False,
    }


def generate_fingerprint(account_id: str) -> Fingerprint:
    """从 `account_id` 派生一套完整确定性指纹。"""
    rng = _seeded_rng(account_id)

    user_agent = rng.choice(USER_AGENTS)
    chrome_version = _extract_chrome_version(user_agent)
    screen = rng.choice(SCREEN_RESOLUTIONS)
    # viewport 比 screen 略小,模拟"有任务栏 / 浏览器边框"的真实窗口。
    # 上限 1920 防止大分辨率指纹在小屏幕笔记本上溢出右侧。
    vp_w = min(screen["width"], 1920)
    vp_h = min(screen["height"] - 100, 1080)
    viewport = {"width": vp_w, "height": vp_h}
    webgl_vendor, webgl_renderer = rng.choice(WEBGL_VENDORS)

    # canvas_noise:一段 32 字节随机串,后续在 toDataURL 末尾追加 → canvas 哈希唯一
    canvas_noise = "".join(rng.choices(string.ascii_letters + string.digits, k=32))
    # audio_noise:[0.0001, 0.001] 之间,加在 audio buffer 上扰动 audio fingerprint
    audio_noise = rng.uniform(0.0001, 0.001)

    # 字体子集:从 22 个里随机挑 10-16 个,模拟不同机器装的字体不一样
    font_count = rng.randint(10, 16)
    fonts = sorted(rng.sample(FONT_LIST, font_count))

    return Fingerprint(
        account_id=account_id,
        device_id=hashlib.md5(account_id.encode("utf-8")).hexdigest()[:16],
        user_agent=user_agent,
        chrome_version=chrome_version,
        viewport=viewport,
        screen=screen,
        timezone="Asia/Shanghai",
        language="zh-CN",
        languages=["zh-CN", "zh", "en-US", "en"],
        platform="Win32",
        webgl_vendor=webgl_vendor,
        webgl_renderer=webgl_renderer,
        canvas_noise=canvas_noise,
        audio_noise=audio_noise,
        hardware_concurrency=rng.choice(HARDWARE_CONCURRENCY_POOL),
        device_memory=rng.choice(DEVICE_MEMORY_POOL),
        color_depth=24,
        pixel_depth=24,
        fonts=fonts,
        plugins=list(PLUGINS[:3]),
        mime_types=list(MIME_TYPES),
        media_devices=list(MEDIA_DEVICES),
        client_hints=_build_client_hints(chrome_version),
    )


# ---------------- 持久化 ----------------


def get_or_create_fingerprint(account_id: str, storage_dir: Path) -> Fingerprint:
    """从磁盘读;不存在则按种子生成并写入。

    `storage_dir` 由调用方决定(`wxsp.config.get_user_data_dir() / "fingerprints"`)。
    JSON schema 变了 / 文件损坏 → 删旧文件,按种子重建(保证账号永远能拿到一致指纹)。
    """
    storage_dir.mkdir(parents=True, exist_ok=True)
    fp_file = storage_dir / f"{account_id}.json"

    if fp_file.exists():
        try:
            data = json.loads(fp_file.read_text(encoding="utf-8"))
            return Fingerprint.from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning(f"[fp] {account_id} 指纹文件损坏 ({exc}),按种子重建")

    fp = generate_fingerprint(account_id)
    try:
        fp_file.write_text(
            json.dumps(fp.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[fp] 新建指纹 {account_id} → {fp_file}")
    except OSError as exc:
        logger.error(f"[fp] 写入指纹 {account_id} 失败: {exc}(运行时仍可用,只是下次重建)")
    return fp


# ---------------- 应用到 Playwright ----------------


def context_options(fp: Fingerprint) -> dict[str, Any]:
    """返回 patchright `launch_persistent_context` 的 kwargs 覆盖项。

    调用方把这个 dict 合并进自己的启动参数:
        opts = {...defaults..., **context_options(fp)}
    """
    return {
        "user_agent": fp.user_agent,
        "locale": fp.language,
        "timezone_id": fp.timezone,
        "viewport": fp.viewport,
        "screen": fp.screen,
        "device_scale_factor": 1.0,
        "has_touch": False,
        "is_mobile": False,
    }


def init_script(fp: Fingerprint) -> str:
    """返回一段 JS,通过 `context.add_init_script()` 注入每个 page。

    覆写浏览器层暴露给 JS 的设备特征:
      - navigator.webdriver / platform / hardwareConcurrency / deviceMemory / languages
      - WebGLRenderingContext.getParameter(37445/37446) → 假显卡型号
      - HTMLCanvasElement.toDataURL → 追加 canvas_noise,canvas hash 唯一
      - screen.colorDepth / pixelDepth
      - navigator.plugins / mimeTypes / mediaDevices.enumerateDevices
      - document.fonts.check → 只承认指纹里的字体
      - navigator.userAgentData.getHighEntropyValues → 假 client hints
      - AudioBuffer.getChannelData → 加噪声扰动 audio fingerprint
      - RTCPeerConnection → 屏蔽 ICE candidate(防 WebRTC 泄漏真实内网/外网 IP)
    """
    payload = json.dumps(
        {
            "languages": fp.languages,
            "hardwareConcurrency": fp.hardware_concurrency,
            "deviceMemory": fp.device_memory,
            "webglVendor": fp.webgl_vendor,
            "webglRenderer": fp.webgl_renderer,
            "canvasNoise": fp.canvas_noise,
            "colorDepth": fp.color_depth,
            "pixelDepth": fp.pixel_depth,
            "plugins": fp.plugins,
            "mimeTypes": fp.mime_types,
            "fonts": fp.fonts,
            "mediaDevices": fp.media_devices,
            "clientHints": fp.client_hints,
            "audioNoise": fp.audio_noise,
            "platform": fp.platform,
        },
        ensure_ascii=False,
    )

    return f"""
(() => {{
    const fp = {payload};

    // navigator 基础字段
    try {{ Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'platform', {{ get: () => fp.platform }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => fp.hardwareConcurrency }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'deviceMemory', {{ get: () => fp.deviceMemory }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'languages', {{ get: () => fp.languages }}); }} catch (e) {{}}
    try {{ Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => 0 }}); }} catch (e) {{}}

    // WebGL 显卡指纹
    try {{
        const origGetParam = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {{
            if (parameter === 37445) return fp.webglVendor;   // UNMASKED_VENDOR_WEBGL
            if (parameter === 37446) return fp.webglRenderer; // UNMASKED_RENDERER_WEBGL
            return origGetParam.apply(this, arguments);
        }};
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            const origGetParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(parameter) {{
                if (parameter === 37445) return fp.webglVendor;
                if (parameter === 37446) return fp.webglRenderer;
                return origGetParam2.apply(this, arguments);
            }};
        }}
    }} catch (e) {{}}

    // Canvas 指纹:toDataURL 末尾追加噪声字符串,每个账号一份
    try {{
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function() {{
            const result = origToDataURL.apply(this, arguments);
            return result + fp.canvasNoise;
        }};
    }} catch (e) {{}}

    // screen 字段
    try {{ Object.defineProperty(screen, 'colorDepth', {{ get: () => fp.colorDepth }}); }} catch (e) {{}}
    try {{ Object.defineProperty(screen, 'pixelDepth', {{ get: () => fp.pixelDepth }}); }} catch (e) {{}}

    // plugins / mimeTypes(给一个看起来正常的列表,空数组反而异常)
    try {{
        const makeListLike = (items, nameKey) => {{
            const arr = items.map(it => Object.assign({{}}, it));
            arr.item = (i) => arr[i] || null;
            arr.namedItem = (name) => arr.find(x => x[nameKey] === name) || null;
            return arr;
        }};
        Object.defineProperty(navigator, 'plugins', {{
            get: () => makeListLike(fp.plugins || [], 'name'),
        }});
        Object.defineProperty(navigator, 'mimeTypes', {{
            get: () => makeListLike(fp.mimeTypes || [], 'type'),
        }});
    }} catch (e) {{}}

    // 字体探测:只承认指纹里的字体
    try {{
        const fontSet = new Set(fp.fonts || []);
        if (document.fonts && document.fonts.check) {{
            const origCheck = document.fonts.check.bind(document.fonts);
            document.fonts.check = (query) => {{
                const m = /"([^"]+)"/.exec(query);
                if (m && m[1]) return fontSet.has(m[1]);
                return origCheck(query);
            }};
        }}
    }} catch (e) {{}}

    // mediaDevices 列表
    try {{
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
            navigator.mediaDevices.enumerateDevices = async () => (fp.mediaDevices || []);
        }}
    }} catch (e) {{}}

    // Client Hints (UA-CH)
    try {{
        const ch = fp.clientHints || {{}};
        const uaData = {{
            brands: ch.brands || [],
            mobile: !!ch.mobile,
            platform: ch.platform || 'Windows',
            getHighEntropyValues: async (hints) => {{
                const out = {{}};
                (hints || []).forEach((key) => {{
                    if (key === 'architecture') out.architecture = ch.architecture || 'x86';
                    if (key === 'bitness') out.bitness = ch.bitness || '64';
                    if (key === 'model') out.model = ch.model || '';
                    if (key === 'platform') out.platform = ch.platform || 'Windows';
                    if (key === 'platformVersion') out.platformVersion = ch.platform_version || '10.0.0';
                    if (key === 'uaFullVersion') out.uaFullVersion = ch.ua_full_version || '';
                    if (key === 'fullVersionList') out.fullVersionList = ch.brands || [];
                    if (key === 'wow64') out.wow64 = !!ch.wow64;
                }});
                return out;
            }},
            toJSON: function() {{ return {{ brands: this.brands, mobile: this.mobile, platform: this.platform }}; }},
        }};
        Object.defineProperty(navigator, 'userAgentData', {{ get: () => uaData }});
    }} catch (e) {{}}

    // Audio 指纹:对 getChannelData 加微小噪声
    try {{
        const origGetChannelData = AudioBuffer.prototype.getChannelData;
        AudioBuffer.prototype.getChannelData = function() {{
            const data = origGetChannelData.apply(this, arguments);
            for (let i = 0; i < data.length; i += 100) {{
                data[i] = data[i] + (Math.random() - 0.5) * fp.audioNoise;
            }}
            return data;
        }};
    }} catch (e) {{}}

    // WebRTC IP 泄漏屏蔽:剥离 ICE candidate
    try {{
        if (typeof RTCPeerConnection !== 'undefined') {{
            const origCreateOffer = RTCPeerConnection.prototype.createOffer;
            RTCPeerConnection.prototype.createOffer = async function() {{
                const offer = await origCreateOffer.apply(this, arguments);
                if (offer && offer.sdp) {{
                    offer.sdp = offer.sdp.replace(/a=candidate:[^\\r\\n]*\\r?\\n?/g, '');
                }}
                return offer;
            }};
        }}
    }} catch (e) {{}}
}})();
"""
