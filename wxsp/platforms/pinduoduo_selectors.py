# ruff: noqa: RUF001
"""拼多多多多视频发布页选择器 —— 改版时的唯一改动点。

2026-07-22 对真实账号(九阳豆浆官方旗舰店)**真发实测**(含完整定时发布流程):
登录态/上传/描述框/话题候选/内容声明下拉/商品弹窗/绑商品后展开的发布设置/
定时发布日历/封面弹窗/点发布后跳转均已命中并验证。
**仍未实测**:风控文案(未触发,沿用通用关键词)。
优先语义化定位(testid/text=),少用脆弱 CSS-module hash class。
"""

from __future__ import annotations

# ---- 登录 URL ----
# SSO 登录链:mms 登录 → 多跳重定向 → 落地 n-creator/video/home。未登录停在 mms/login。
LOGIN_URL = (
    "https://mms.pinduoduo.com/login/sso?platform=live&accessType=auto"
    "&redirectUrl=https://live.pinduoduo.com/login/checker%3FisNewCreatorFrom%3Dvideo"
    "%26referUrl%3D%252Fn-creator%252Fvideo%252Fhome%253Ffrom%253Dmms"
    "%2526msfrom%253Dmms_sidenav%26from%3Dmms"
)
HOME_URL = "https://live.pinduoduo.com/n-creator/video/home"  # 发布页 = 首页(SPA,无独立发布页 URL)
LOGIN_URL_FRAGMENT = "mms.pinduoduo.com/login"  # 未登录停在此
LOGGED_IN_URL_FRAGMENT = "live.pinduoduo.com/n-creator/video/home"  # 登录成功硬信号

# ---- 视频上传 ----
# 隐藏 input(multiple,支持批量)。实测 accept 含 .mp4/.wmv/.mov/.avi/.m4v
VIDEO_FILE_INPUT = 'input[type="file"][accept*=".mp4"]'
VIDEO_UPLOAD_AREA = (
    "div[class^='no-video_wrap']"  # 上传区父容器(物理点击用);class 含 hash,用前缀模糊
)
UPLOAD_DONE_MARKER = "text=视频上传成功"  # 上传成功文案(实测出现即完成)

# ---- 描述(无标题框,主文案;同快手)----
# DraftJS 风格 contenteditable(实测可写入)。sabo-root 是 sabo 编辑器根 class。
DESC_EDITOR = 'div[contenteditable="true"].sabo-root'
DESC_MAX_LENGTH = 500  # 页内计数器 N/500

# ---- 话题(描述框内输入 #关键词 弹候选)----
TOPIC_POPOVER = ".caret-popover-root"  # 输入 #关键词 后弹出
TOPIC_ITEM = ".sabo-hash-tag-item"  # 候选项(点选才真正绑定话题)

# ---- 内容声明(必填下拉;拼多多特有,同淘宝 declaration)----
# beast-core select 组件,下拉触发器 testId 稳定
DECLARATION_TRIGGER = 'input[data-testid="beast-core-select-htmlInput"]'
# 下拉选项文本(点开下拉后用 text= 定位)
DECLARATION_OPTIONS = {
    "内容无需标注": "内容无需标注",
    "含AI生成内容": "含AI生成内容",
    "含虚构演绎内容": "含虚构演绎内容",
    "内容含营销信息": "内容含营销信息",
    "内容为转载": "内容为转载",
    "个人观点，仅供参考": "个人观点，仅供参考",
}
DECLARATION_DEFAULT = "内容无需标注"

# ---- 挂商品(商品ID tab:输入 ID → 下一步直接绑定,无需勾选)----
PRODUCT_TRIGGER = "text=添加商品"
PRODUCT_TAB_BY_ID = "text=商品ID"  # 切到商品ID tab(精确搜索)
PRODUCT_ID_INPUT = 'input[placeholder*="商品id"]'  # placeholder 含"商品id"
PRODUCT_NEXT_BUTTON = 'button:has-text("下一步")'  # 输入ID后点下一步直接绑定
# 绑定成功的标志:出现"删除商品"/"更改商品"按钮
PRODUCT_BOUND_MARKER = 'button:has-text("删除商品")'

# ---- 发布设置(绑商品后才出现)----
SCHEDULE_RADIO_CONTAINER = "text=定时发布"  # 发布设置 radio 容器(label 文本)
SCHEDULE_DATE_INPUT = (
    'input[data-testid="beast-core-datePicker-htmlInput"]'  # 格式 YYYY-MM-DD HH:MM:SS(带秒)
)
SCHEDULE_TIME_INPUT = (
    'input[data-testid="beast-core-timePicker-html-input"]'  # 格式 HH:MM:SS(日历面板内)
)
SCHEDULE_CONFIRM_BUTTON = 'button:has-text("确认")'  # 日历确认(点格子后必须点确认才生效)
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"  # 日期框格式(带秒,区别于其他平台的 %Y-%m-%d %H:%M)

# ---- 封面(可选;best-effort)----
COVER_EDIT_BUTTON = 'button:has-text("编辑封面")'
COVER_MODAL_TITLE = "封面选择"
COVER_UPLOAD_TAB = "text=本地上传"
COVER_FILE_INPUT = 'input[type="file"][accept=".jpg"]'  # accept 含 .jpg/.jpeg/.png
COVER_CONFIRM_BUTTON = 'button:has-text("确定")'

# ---- 发布 / 风控 / 成功 ----
PUBLISH_BUTTON = 'button:has-text("发布")'
# 真发实测(2026-07-22):点发布后跳转 mall-goods-video(无 toast)
SUCCESS_URL_FRAGMENT = "n-creator/video/mall-goods-video"
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后", "账号异常", "违规")
