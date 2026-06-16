"""快手创作者平台选择器 —— 快手改版时的唯一改动点。

选择器移植自 _ref/social-auto-upload/uploader/ks_uploader/main.py(KSVideo),
核心路径(登录态判定 / 上传按钮 / 描述框 / 定时 DatePicker / 发布)已对真实页
`cp.kuaishou.com` 实测校准(2026-06-16)。封面弹窗为可选 best-effort,未端到端实跑。
优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
UPLOAD_PAGE = "https://cp.kuaishou.com/article/publish/video"
UPLOAD_PAGE_GLOB = "**/article/publish/video**"
# 发布成功后跳转(glob)
MANAGE_URL_GLOB = "**/article/manage/video?status=2&from=publish**"

# ---- 登录态 ----
# 实测(2026-06-16):未登录访问上传页【不会】自动跳 passport,而是停在
# cp.kuaishou.com 的落地页,显示「立即登录」、且没有上传按钮。登录态判定因此走
# "上传按钮是否出现"(见 platform_meta.login_meta selector 模式 + _verify_logged_in)。
# 落地页标记:点它跳到 passport 扫码页。
LOGGED_OUT_MARKER = "立即登录"
# passport 登录页 URL 片段(扫码时用来判断"是否还在登录页",而非上传页)
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
# 上传页可能弹「还有上次未发布的视频,是否继续编辑」残稿提示(继续编辑/放弃)。
# 开场 best-effort 点「放弃」清掉,免得残稿干扰本次上传(尤其 dry-run 反复跑会留残稿)。
LEFTOVER_DRAFT_HINT = "还有上次未发布的视频"  # 提示文案前缀(用 substring 匹配,避开全角标点)
DISCARD_DRAFT_BUTTON = "放弃"

# ---- 描述(快手无独立标题框;「作品描述」框是主文案区)----
# 实测:发布页唯一的 contenteditable div 即描述框(class 形如 _description_xxx,placeholder
# 以「作品描述」开头)。用 contenteditable 定位最稳,避开 hash class / label 文案。
DESC_EDITOR = 'div[contenteditable="true"]'
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
