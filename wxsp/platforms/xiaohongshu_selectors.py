"""小红书创作者中心选择器 —— 小红书改版时的唯一改动点。

选择器移植自 _ref/social-auto-upload/uploader/xiaohongshu_uploader/main.py
(XiaoHongShuVideo,patchright)。参考侧约 2026-03 对真实页校验过,但**本仓库未对
当前线上页二次校验**(标注未实跑);定稿走 `wxsp run --task-id N --dry-run` 实测微调。
优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
LOGIN_URL = "https://creator.xiaohongshu.com/login"
PUBLISH_VIDEO_URL = "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video"
PUBLISH_VIDEO_URL_GLOB = "**/publish/publish**target=video**"
# 发布成功后跳转(glob)
SUCCESS_URL_GLOB = "**/publish/success?**"

# ---- 登录态 ----
# 未登录访问发布页会重定向到 .../login;URL 含该片段 = 未登录(同淘宝 url 模式)。
# platform_meta.login_meta 用 url 模式 + 本片段;adapter _verify_logged_in 额外兜底登录框可见性。
LOGIN_URL_FRAGMENT = "creator.xiaohongshu.com/login"
LOGIN_BOX_SELECTOR = "div[class*='login-box']"

# ---- 视频上传 ----
VIDEO_FILE_INPUT = "div[class^='upload-content'] input.upload-input"  # 脆弱 class 选择器,改版易漂移,dry-run 重点核对
# 上传/转码完成判据:预览区文本含任一关键词,或标题框出现(见 adapter _upload_video)
UPLOAD_PREVIEW = 'div[class*="preview-new"]'
UPLOAD_DONE_KEYWORDS = ("上传成功", "分辨率", "重新上传", "编辑封面", "已上传", "100%")

# ---- 标题 / 描述 / 话题 ----
TITLE_INPUT = 'input[placeholder*="填写标题"]'
TITLE_MAX_LENGTH = 20  # 小红书视频标题上限 20 字
DESC_EDITOR = 'p[data-placeholder*="输入正文描述"]'
# 话题:键入 #tag 后弹下拉,选第一个候选才真正绑定
TOPIC_CONTAINER = "#creator-editor-topic-container"
TOPIC_ITEM = "#creator-editor-topic-container .item"

# ---- 封面(可选;弹窗 best-effort,未端到端实跑)----
COVER_PLUGIN_TITLE = "div.cover-plugin-title"
COVER_PLUGIN_TITLE_TEXT = "设置封面"
# 从「设置封面」标题向上找封面容器,再定位其中的上传入口
COVER_PREVIEW_ANCESTOR_XPATH = "xpath=ancestor::div[contains(@class, 'cover-plugin-preview')]"
COVER_ENTRY_INNER = "div.cover > div.default"
COVER_MODAL = "div.d-modal.cover-modal"
COVER_FILE_INPUT = 'input[type="file"][accept*="image"]'
COVER_CONFIRM_BUTTON = "button.mojito-button"
COVER_CONFIRM_BUTTON_TEXT = "确定"

# ---- 定时发布 ----
SCHEDULE_SWITCH_CARD = ".custom-switch-card"
SCHEDULE_SWITCH_TEXT = "定时发布"
SCHEDULE_SWITCH = ".d-switch"
SCHEDULE_DATETIME_INPUT = ".d-datepicker-input-filter input.d-text"
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

# ---- 发布 / 风控 / 成功 ----
# 始终定时发布(系统强制 publish_at,无立即发布分支),故点「定时发布」按钮
PUBLISH_BUTTON = 'button:has-text("定时发布")'
# 先沿用通用文案,dry-run 时按小红书实际文案补
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后", "账号异常", "违规")
SUCCESS_INDICATORS = ("发布成功",)
