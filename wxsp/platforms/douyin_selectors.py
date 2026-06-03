# ruff: noqa: RUF001
"""抖音创作者中心选择器 —— 抖音改版时的唯一改动点。

已对真实页面校验定稿(2026-06-03,creator.douyin.com)。
核心发布路径(登录态 / 上传 / 标题 / 描述 / 定时 / 发布)均经实跑校验;
封面弹窗(横/竖/AI 封面 + 裁剪)为可选功能,仅校验了入口/弹窗/上传 input,
裁剪与「完成」应用全流程未端到端实跑,首次用自定义封面时按需微调。
优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
UPLOAD_PAGE = "https://creator.douyin.com/creator-micro/content/upload"
HOME_URL = "https://creator.douyin.com/"
# 上传后进入的发布页(实测命中第 2 个变体 content/post/video;两个都保留兜底)
PUBLISH_PAGE_URLS = (
    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
)
# 发布成功后跳转(glob)
MANAGE_URL_GLOB = "https://creator.douyin.com/creator-micro/content/manage**"

# ---- 登录态 ----
# 未登录页出现「扫码登录 / 验证码登录」(无「手机号登录」字样,那只是输入框 placeholder)
LOGIN_TEXT_MARKERS = ("扫码登录", "验证码登录")  # 任一出现 = 未登录
# 登录成功后会跳到 creator-micro/* 下任意页(不一定是 /home),用子串判更稳
LOGGED_IN_URL_FRAGMENT = "creator.douyin.com/creator-micro/"
# platform_meta.login_meta 用的「已登录可见」指示元素:上传页「上传视频」按钮(未登录会被
# 登录页挡住)。注:与 platform_meta.login_meta["selector"] 同值(那边不 import 本模块,
# 故各存一份,改时两处一起改)。
LOGGED_IN_INDICATOR = 'button:has-text("上传视频")'

# ---- 视频上传 ----
VIDEO_FILE_INPUT = "div[class^='container'] input"
# 上传/转码完成 = 预览区出现「重新上传」(在 long-card 容器内)
UPLOAD_DONE_MARKER = '[class^="long-card"] div:has-text("重新上传")'
UPLOAD_FAILED_MARKER = "text=上传失败"  # 失败态未实跑触发,文本兜底
UPLOAD_RETRY_INPUT = 'input[class^="upload-btn-input"]'

# ---- 标题 / 描述(直接全局定位;弃用脆弱的「作品描述」区块 xpath)----
TITLE_INPUT = 'input[placeholder*="作品标题"]'
DESC_EDITOR = '.zone-container[contenteditable="true"]'
TITLE_MAX_LENGTH = 30

# ---- 封面(可选;弹窗复杂,best-effort,未端到端实跑)----
COVER_ENTRY = "text=选择封面"  # 横/竖两个入口,adapter 取 .first
COVER_MODAL = 'div[id*="creator-content-modal"]'
COVER_UPLOAD_INPUT = "div[class^='semi-upload upload'] input.semi-upload-hidden-input"
COVER_DONE_BUTTON = 'button:has-text("完成")'  # 弹窗内可能多个,adapter 取 .first
COVER_REQUIRED_HINT = "请设置封面后再发布"
COVER_RECOMMEND_FIRST = '[class^="recommendCover-"]'
COVER_CONFIRM_APPLY_TEXT = "是否确认应用此封面？"

# ---- 定时发布 ----
SCHEDULE_RADIO = "[class^='radio']:has-text('定时发布')"
SCHEDULE_DATETIME_INPUT = '.semi-input[placeholder="日期和时间"]'
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

# ---- 发布 / 风控 / 成功 ----
PUBLISH_BUTTON_NAME = "发布"  # get_by_role("button", name=, exact=True)
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后再试", "账号异常")
SUCCESS_INDICATORS = ("发布成功",)
