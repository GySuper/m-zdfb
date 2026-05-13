"""视频号发布页选择器集中管理 —— 改版时唯一的改动点(M5)。

参考 social-auto-upload/uploader/tencent_uploader/main.py 的踩坑成果,
按发布流程的 20 步分组列出。所有定位优先用语义化选择器(text=/role=)。
"""

from __future__ import annotations

# ============== URL ==============
PUBLISH_PAGE_URL = "https://channels.weixin.qq.com/platform/post/create"
POST_LIST_URL = "https://channels.weixin.qq.com/platform/post/list"
# URL 包含此片段视为发布成功后跳转(submit_publish 等待此 URL)
POST_LIST_URL_FRAGMENT = "/post/list"

# ============== [4] 登录态判定 ==============
# 任一可见即认为已登录(扫码框存在时反向证明未登录)
LOGGED_IN_SELECTORS = (
    'div:has-text("发表视频")',
    'button:has-text("发表")',
    'button:has-text("保存草稿")',
)
LOGIN_QRCODE_SELECTORS = (
    "div.login-qrcode-wrap",
    "div.qrcode-wrap",
    "img.qrcode",
)

# ============== [5] 视频上传 ==============
FILE_INPUT = 'input[type="file"]'
# 上传完成判定:发表按钮的 class 不含 weui-desktop-btn_disabled
UPLOAD_PUBLISH_BUTTON_ROLE = ("button", "发表")
UPLOAD_DISABLED_CLASS = "weui-desktop-btn_disabled"
UPLOAD_FAILED_INDICATOR = "div.status-msg.error"
UPLOAD_DELETE_TAG = 'div.media-status-content div.tag-inner:has-text("删除")'

# ============== [6][7][8] 标题 / 描述 / 标签 ==============
# 视频号发布页:标题 + 描述 + tag 全部进 .input-editor 富文本,用键盘 type
TITLE_EDITOR = "div.input-editor"
SHORT_TITLE_LABEL_TEXT = "短标题"

# ============== [9] 封面 ==============
COVER_ENTRY_SELECTORS = (
    'div.vertical-cover-wrap:has-text("个人主页卡片"):has-text("3:4")',
    'div.vertical-cover-wrap:has-text("3:4")',
    'div.vertical-cover-wrap:has-text("个人主页卡片")',
)
COVER_DIALOG_HAS_TEXT = "编辑个人主页卡片"
COVER_FILE_INPUT = '.single-cover-uploader-wrap input[type="file"]'
COVER_CROP_DIALOG_HAS_TEXT = "裁剪封面图"
COVER_CROP_CONFIRM = 'div.weui-desktop-dialog__ft button.weui-desktop-btn_primary:has-text("确定")'
COVER_CONFIRM = 'div.weui-desktop-dialog__ft button.weui-desktop-btn_primary:has-text("确认")'

# ============== [10] 合集 ==============
COLLECTION_LABEL_TEXT = "添加到合集"

# ============== [11] 原创 ==============
ORIGINAL_CHECKBOX_LABEL = "视频为原创"
ORIGINAL_TERMS_LABEL = "我已阅读并同意 《视频号原创声明使用条款》"
ORIGINAL_DECLARE_BUTTON = "声明原创"

# ============== [12] 定时发布 ==============
SCHEDULE_RADIO_LABEL_HAS_TEXT = "定时"
SCHEDULE_DATE_INPUT = 'input[placeholder="请选择发表时间"]'
SCHEDULE_MONTH_LABEL = 'span.weui-desktop-picker__panel__label:has-text("月")'
SCHEDULE_NEXT_MONTH_BTN = "button.weui-desktop-btn__icon__right"
SCHEDULE_DAY_TABLE = "table.weui-desktop-picker__table a"
SCHEDULE_DAY_DISABLED_CLASS = "weui-desktop-picker__disabled"
SCHEDULE_TIME_INPUT = 'input[placeholder="请选择时间"]'

# ============== [13] 风控文案 ==============
# 任一在页面 body 文本中出现 → RiskControl
RISK_CONTROL_KEYWORDS = (
    "请稍后",
    "系统繁忙",
    "操作过于频繁",
    "账号异常",
)

# ============== [15] 提交 ==============
SUBMIT_PUBLISH_BUTTON = 'div.form-btns button:has-text("发表")'

# ============== [17] 提取已发布 URL(尽力而为) ==============
# 视频号定时发布提交后,页面跳到 post/list,新条目在最顶。
# 这个选择器返回首行视频卡的链接;找不到就 None(对定时发布是常态)。
LIST_FIRST_ITEM_LINK = "div.post-feeds-card-wrap a.feed-card-cover-wrap"
