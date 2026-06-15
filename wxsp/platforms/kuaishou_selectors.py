# ruff: noqa: RUF001
"""快手创作者平台选择器 —— 快手改版时的唯一改动点。

选择器移植自 _ref/social-auto-upload/uploader/ks_uploader/main.py(KSVideo),
**尚未对当前线上页实跑校验**;首次用 `wxsp run --task-id N --dry-run` 跑通后按真实页微调。
优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
UPLOAD_PAGE = "https://cp.kuaishou.com/article/publish/video"
UPLOAD_PAGE_GLOB = "**/article/publish/video**"
# 发布成功后跳转(glob)
MANAGE_URL_GLOB = "**/article/manage/video?status=2&from=publish**"

# ---- 登录态 ----
# 未登录访问上传页会重定向到 passport.kuaishou.com 扫码;URL 含该片段 = 未登录(同淘宝 url 模式)
LOGIN_URL_FRAGMENT = "passport.kuaishou.com"

# ---- 视频上传 ----
# 上传按钮(点击弹原生文件选择器);用 expect_file_chooser 接管
UPLOAD_BUTTON = "button[class^='_upload-btn']"
# 上传中标记:存在 = 还在传;count==0 = 完成
UPLOADING_MARKER = "text=上传中"
UPLOAD_FAILED_MARKER = "text=上传失败"
# 失败重传的隐藏 input
UPLOAD_RETRY_INPUT = 'div.progress-div [class^="upload-btn-input"]'
# 首次进页面的「我知道了」提示按钮
KNOW_BUTTON = 'button[type="button"] span:text("我知道了")'
# Joyride 新手引导遮罩
JOYRIDE_TOOLTIP = 'div[id^="react-joyride-step"] div[role="alertdialog"]'
JOYRIDE_CLOSE = '[aria-label="Skip"], [data-action="skip"], button[title="Skip"]'

# ---- 描述(快手无独立标题框;「描述」框是主文案区)----
DESC_LABEL_TEXT = "描述"
MAX_TAGS = 3  # 快手话题标签上限

# ---- 封面(可选;弹窗 best-effort,未端到端实跑)----
COVER_LABEL_TEXT = "封面设置"
COVER_MODAL = 'div[role="document"].ant-modal'
COVER_UPLOAD_TAB_TEXT = "上传封面"
COVER_CONFIRM_BUTTON_NAME = "确认"

# ---- 定时发布(ant-design DatePicker,controlled component,必须走 native setter)----
SCHEDULE_RADIO_WRAPPER = "label.ant-radio-wrapper"
SCHEDULE_RADIO_TEXT = "定时发布"
SCHEDULE_DATETIME_INPUT = 'input[placeholder="选择日期时间"]'
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# ---- 发布 / 风控 / 成功 ----
PUBLISH_BUTTON_TEXT = "发布"  # get_by_text(exact=True)
PUBLISH_CONFIRM_TEXT = "确认发布"
# 先沿用抖音那套,dry-run 时按快手实际文案补
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后再试", "账号异常")
SUCCESS_INDICATORS = ("发布成功",)
