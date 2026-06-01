# ruff: noqa: RUF001, RUF002
"""淘宝光合平台发布页选择器集中管理。

发布页: https://creator.guanghe.taobao.com/page/pubNew/video
注意: 表单在 huodong.taobao.com 的 iframe 内，所有选择器需先 frame_locator(iframe)。
"""

from __future__ import annotations

# ============== URL ==============
PUBLISH_PAGE_URL = "https://creator.guanghe.taobao.com/page/pubNew/video"
LOGIN_URL_FRAGMENT = "login.taobao.com"
CREATOR_HOME = "https://creator.guanghe.taobao.com"

# 首页导航:发布作品 hover 后出现的下拉菜单
PUBLISH_DROPDOWN_TRIGGER = "发布作品"
PUBLISH_VIDEO_MENU_ITEM = "发视频"

# ============== iframe ==============
IFRAME_SELECTOR = 'iframe[title="发布器"]'

# ============== [4] 登录态判定 ==============
LOGGED_IN_INDICATOR = "text=发布视频"

# ============== [5] 视频上传 ==============
FILE_INPUT = 'input[type="file"]'
UPLOAD_AREA = "text=点击上传视频，或将视频拖放到此处"
COVER_WAITING_TEXT = "等待视频上传"
COVER_READY_INDICATOR = "视频封面"

# ============== [7] 标题 ==============
TITLE_INPUT = 'input[placeholder="加个标题让内容更吸引人"]'
TITLE_MAX_LENGTH = 30

# ============== [8] 描述 ==============
DESCRIPTION_EDITOR = "[data-cangjie-key]"
DESCRIPTION_AREA = "text=展开说说"

# ============== [9] 话题活动 ==============
TOPIC_CLICK_AREA = "text=点击添加话题"
TOPIC_DIALOG_HEADING = ".next-dialog-header"
TOPIC_SEARCH_INPUT = 'input[placeholder*="关键词"]'
TOPIC_SEARCH_BUTTON = 'button:has-text("搜索")'
TOPIC_CONFIRM_BUTTON = 'button:has-text("确认提交")'
TOPIC_CLOSE_BUTTON = 'button:has-text("取消")'

# ============== [10] 关联商品 ==============
PRODUCT_TRIGGER = "text=添加商品"
PRODUCT_DIALOG_HEADING = ".next-dialog-header"
PRODUCT_SEARCH_INPUT = 'input[placeholder*="商品"]'
PRODUCT_CONFIRM_BUTTON = 'button:has-text("确定")'
PRODUCT_CLOSE_BUTTON = 'button:has-text("取消")'

# ============== [11] 定时发布 ==============
SCHEDULE_RADIO = "text=定时发布"
SCHEDULE_COMBOBOX = '[role="combobox"]'
SUBMIT_BUTTON_SCHEDULED = 'button:has-text("定时发布")'
SUBMIT_BUTTON_IMMEDIATE = 'button:has-text("立即发布")'

# ============== [12] 创作者声明 ==============
DECLARATION_RADIO_MAP = {
    "内容无需标注": "text=内容无需标注",
    "含AI生成内容": "text=含AI生成内容",
    "含虚构演绎内容": "text=含虚构演绎内容",
    "内容为转载": "text=内容为转载",
    "个人观点，仅供参考": "text=个人观点，仅供参考",
    "内容含营销信息": "text=内容含营销信息",
}

# ============== [13] AI优化 ==============
AI_TOGGLE_SWITCH = '[role="switch"]'

# ============== [14] 允许下载(radio,默认选中 → 点一下取消)
DOWNLOAD_RADIO = "text=允许下载"

# ============== 风控文案 ==============
RISK_CONTROL_KEYWORDS = (
    "请稍后",
    "系统繁忙",
    "操作过于频繁",
    "账号异常",
    "内容不符合",
)

# ============== 成功判定 ==============
SUCCESS_INDICATORS = (
    "发布成功",
    "定时发布成功",
    "已保存",
)
