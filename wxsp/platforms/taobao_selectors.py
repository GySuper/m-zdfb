# ruff: noqa: RUF001, RUF002
"""淘宝光合平台发布页选择器集中管理。

发布页: https://creator.guanghe.taobao.com/page/pubNew/video
注意: 表单在 huodong.taobao.com 的 iframe 内，所有选择器需先 frame_locator(iframe)。
"""

from __future__ import annotations

# ============== URL ==============
PUBLISH_PAGE_URL = "https://creator.guanghe.taobao.com/page/pubNew/video"
SUCCESS_URL_FRAGMENT = "/page/workspace/tb"
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
# 实测(2026-07-10 DOM 采样):平台显示"等待视频上传..."(带省略号),不带省略号永远 count=0。
# 上传成功后该文案消失、"重新上传"按钮出现;失败则出"视频上传失败"。
UPLOAD_WAITING_TEXT = "等待视频上传..."
UPLOAD_FAILED_TEXT = "视频上传失败"
COVER_READY_INDICATOR = "重新上传"  # 上传成功后出现(失败时也有,故须配合失败文案排除)
# 封面图预览:上传成功后渲染出 <img> 封面缩略图。"视频封面"是 section 标题(一直存在,
# 不能用作判据)。用封面区 img 元素的 src 是否含封面图路径判定渲染完成。
COVER_IMG_PREVIEW = 'img[src*="cover"]'

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
# 搜索结果是商品卡片(非每商品一个独立 checkbox 列)。卡片标题链接的 href 含商品ID,
# 是最稳的锚点(class 都是 CSS-module hash)。用 .format(pid=) 注入。
PRODUCT_ITEM_LINK_BY_ID = 'a[href*="item.htm?id={pid}"]'
# 弹窗默认商品列表就绪信号:click"添加商品"后默认列表还在加载,等首个商品 link
# 出现再开始按 ID 搜索,否则搜索与列表加载竞态致 card.hover() 超时。
PRODUCT_ITEM_LINK_ANY = 'a[href*="item.htm?id="]'
# 从标题链接上溯到最近的、含商品选择 checkbox 的卡片容器。
# 不依赖 CSS-module class:淘宝已从 `--item--` 改为 `--itemCard---<hash>`。
PRODUCT_ITEM_CARD_ANCESTOR = 'xpath=ancestor::div[.//input[@type="checkbox"]][1]'
# 卡片内"商品选择"复选框的真实 input(区别于顶部"筛选近14天")。Fusion 把 opacity:0 的
# input 绝对定位盖在可见方框上专门接收点击 —— 点 <label>/.next-checkbox-inner 会被 input
# 拦截指针事件(报 "input intercepts pointer events" 超时),直接点 input 才是正解。
PRODUCT_ITEM_SELECT_CHECKBOX_INPUT = 'input.next-checkbox-input[type="checkbox"]'
PRODUCT_CONFIRM_BUTTON = 'button:has-text("确定")'
PRODUCT_CLOSE_BUTTON = 'button:has-text("取消")'

# ============== [11] 定时发布 ==============
SCHEDULE_RADIO = "text=定时发布"
SCHEDULE_COMBOBOX = '[role="combobox"]'
SUBMIT_BUTTON_SCHEDULED = 'button:has-text("定时发布")'
SUBMIT_BUTTON_IMMEDIATE = 'button:has-text("立即发布")'
# DatePicker 内部:日期/时间用文本输入框直接填(YYYY/MM/DD + HH:mm),绕开点日历格子。
# 日历里本月日号和下月溢出日号重复(如本月+下月都有 "1""2""3"),点 .first 会选到
# 本月同号 → 跨月任务被排到错误月份。实测:填 YYYY/MM/DD 会让日历自动翻到目标月并选中,
# 但会把时间重置成 00:00,故时间必须在日期之后填。
SCHEDULE_PICKER_OVERLAY = ".next-overlay-wrapper.opened"
SCHEDULE_DATE_INPUT = 'input[placeholder="YYYY/MM/DD"]'
SCHEDULE_TIME_INPUT = 'input[placeholder="HH:mm"]'
SCHEDULE_CONFIRM = 'button:has-text("确定")'

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
# 锚定到含"AI优化"标签的 hosting-section 内的开关,避免误中页面上其它 role=switch。
# next-switch 用 aria-checked="true"/"false" 表示开关态(平台默认开)。
AI_TOGGLE_SWITCH = '[class*="hosting-section"]:has-text("AI优化") [role="switch"]'

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
