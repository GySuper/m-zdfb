# ruff: noqa: RUF001
"""抖音创作者中心选择器 —— 抖音改版时的唯一改动点。

值迁移自 _ref/social-auto-upload/uploader/douyin_uploader/main.py,
对真实页面校验定稿。优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
UPLOAD_PAGE = "https://creator.douyin.com/creator-micro/content/upload"
HOME_URL = "https://creator.douyin.com/"
# 上传后进入的发布页(抖音有两种 URL 变体,任一命中即算进入)
PUBLISH_PAGE_URLS = (
    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
)
# 发布成功后跳转(glob)
MANAGE_URL_GLOB = "https://creator.douyin.com/creator-micro/content/manage**"

# ---- 登录态 ----
LOGIN_TEXT_MARKERS = ("扫码登录", "手机号登录")  # 任一出现 = 未登录
LOGGED_IN_HOME_PREFIX = "https://creator.douyin.com/creator-micro/home"
# platform_meta.login_meta 用的"已登录可见"指示元素(对真页面定稿)
LOGGED_IN_INDICATOR = 'div[class^="container"] input'

# ---- 视频上传 ----
VIDEO_FILE_INPUT = "div[class^='container'] input"
UPLOAD_DONE_MARKER = '[class^="long-card"] div:has-text("重新上传")'
UPLOAD_FAILED_MARKER = 'div.progress-div > div:has-text("上传失败")'
UPLOAD_RETRY_INPUT = 'div.progress-div [class^="upload-btn-input"]'

# ---- 标题 / 描述 ----
DESC_SECTION_ANCHOR = (
    "作品描述"  # get_by_text(exact) → ancestor::div[2] → following-sibling::div[1]
)
TITLE_INPUT_IN_SECTION = 'input[type="text"]'
DESC_EDITOR_IN_SECTION = '.zone-container[contenteditable="true"]'
TITLE_MAX_LENGTH = 30

# ---- 封面 ----
COVER_ENTRY = 'text="选择封面"'
COVER_MODAL = 'div[id*="creator-content-modal"]'
COVER_UPLOAD_INPUT = "div[class^='semi-upload upload'] >> input.semi-upload-hidden-input"
COVER_DONE_BUTTON = 'button:visible:has-text("完成")'
COVER_EXTRACT_FOOTER = "div.extractFooter"
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
