"""小红书创作者中心选择器 —— 小红书改版时的唯一改动点。

选择器移植自 _ref/social-auto-upload/uploader/xiaohongshu_uploader/main.py
(XiaoHongShuVideo,patchright),并于 2026-06-17 对真实页(新版 creator.xiaohongshu.com,
TipTap 编辑器)逐项实测校验:登录态 / 上传入口 / 标题 / 正文 / 话题下拉 / 定时开关+时间 /
发布按钮 均已命中,并据此修正(发布按钮、正文编辑器在新版有变,见下)。
**仍未实测**:封面弹窗全流程(可选,best-effort)、点发布后的成功跳转 URL(校验时未真发)、
风控文案(未触发)。优先语义化(text= / role= / placeholder=),少用脆弱 CSS class。
"""

from __future__ import annotations

# ---- 页面 URL ----
LOGIN_URL = "https://creator.xiaohongshu.com/login"
PUBLISH_VIDEO_URL = "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video"
PUBLISH_VIDEO_URL_GLOB = "**/publish/publish**target=video**"
# 发布成功后跳转(glob);移植自参考,本次校验未真发,未二次确认
SUCCESS_URL_GLOB = "**/publish/success?**"

# ---- 登录态 ----
# 未登录访问发布页会重定向到 .../login;URL 含该片段 = 未登录(同淘宝 url 模式)。
# platform_meta.login_meta 用 url 模式 + 本片段;adapter _verify_logged_in 额外兜底登录框可见性。
LOGIN_URL_FRAGMENT = "creator.xiaohongshu.com/login"
LOGIN_BOX_SELECTOR = "div[class*='login-box']"  # 实测命中(login-box-container)
# 登录成功的正向判据:扫码成功后小红书跳到创作者中心 /new/* (实测: /new/home),
# 并渲染左侧导航栏(「笔记管理」只登录后才出现)。login() / _verify_logged_in 用它
# 做"已登录"硬判定,避免「URL 暂离 /login + 登录框未渲染」的加载中间态被误判成功。
LOGGED_IN_URL_FRAGMENT = "creator.xiaohongshu.com/new/"
SIDEBAR_MARKER_TEXT = "笔记管理"

# ---- 视频上传 ----
VIDEO_FILE_INPUT = (
    "div[class^='upload-content'] input.upload-input"  # 实测命中(隐藏 input,set_input_files)
)
# 上传/转码完成判据:预览区文本含任一关键词,或标题框出现(见 adapter _upload_video)
UPLOAD_PREVIEW = 'div[class*="preview-new"]'
UPLOAD_DONE_KEYWORDS = ("上传成功", "分辨率", "重新上传", "编辑封面", "已上传", "100%")

# ---- 标题 / 描述 / 话题 ----
TITLE_INPUT = 'input[placeholder*="填写标题"]'  # 实测命中(placeholder「填写标题会有更多赞哦」)
TITLE_MAX_LENGTH = 20  # 小红书视频标题上限 20 字
# 新版正文区是 TipTap/ProseMirror;用 contenteditable 根定位(实测:placeholder 的 <p> 一旦
# 有文字 data-placeholder 即消失,_add_tags 再次聚焦会落空,故不用 p[data-placeholder] 选择器)。
DESC_EDITOR = "div.tiptap.ProseMirror"
# 话题:正文里键入 #关键词 后弹 Tippy 下拉(实测 id 仍为 creator-editor-topic-container,
# 仅在 # 查询激活时存在),第一个 .item 即高亮匹配项(.item.is-selected),点它才真正绑定话题。
TOPIC_CONTAINER = "#creator-editor-topic-container"
TOPIC_ITEM = "#creator-editor-topic-container .item"

# ---- 封面(可选;弹窗 best-effort,未端到端实跑)----
COVER_PLUGIN_TITLE = "div.cover-plugin-title"  # 入口实测命中;弹窗内流程未实跑
COVER_PLUGIN_TITLE_TEXT = "设置封面"
# 从「设置封面」标题向上找封面容器,再定位其中的上传入口
COVER_PREVIEW_ANCESTOR_XPATH = "xpath=ancestor::div[contains(@class, 'cover-plugin-preview')]"
COVER_ENTRY_INNER = "div.cover > div.default"
COVER_MODAL = "div.d-modal.cover-modal"
COVER_FILE_INPUT = 'input[type="file"][accept*="image"]'
COVER_CONFIRM_BUTTON = "button.mojito-button"
COVER_CONFIRM_BUTTON_TEXT = "确定"

# ---- 定时发布 ----
# 实测:两张 .custom-switch-card(原创声明 / 定时发布),按文案过滤后点 .d-switch 开关,
# 开关打开后 .d-datepicker-input-filter input.d-text 出现(非 readonly,可 fill),
# 预填值形如「2026-06-17 11:30」→ 印证 %Y-%m-%d %H:%M 格式。
SCHEDULE_SWITCH_CARD = ".custom-switch-card"
SCHEDULE_SWITCH_TEXT = "定时发布"
SCHEDULE_SWITCH = ".d-switch"
SCHEDULE_DATETIME_INPUT = ".d-datepicker-input-filter input.d-text"
SCHEDULE_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

# ---- 发布 / 风控 / 成功 ----
# 新版底部「发布/暂存离开」按钮在 <xhs-publish-btn> 的【闭合】shadow DOM 里(实测 2026-06-17):
# 红色主按钮 = button.ce-btn.bg-red(定时模式文案「定时发布」/ 即时模式「发布」,用 .bg-red 类
# 避开白色「暂存离开」button.ce-btn.white)。闭合 shadow 普通选择器钻不进 → adapter 在导航前注入
# _FORCE_OPEN_SHADOW_JS 强制 open 后本选择器才命中。⚠️「发布笔记」是左侧栏新建入口,不是提交按钮。
PUBLISH_BUTTON = "xhs-publish-btn button.ce-btn.bg-red"
# 先沿用通用文案,实际风控文案待触发后补
RISK_CONTROL_KEYWORDS = ("操作频繁", "操作过于频繁", "请稍后", "账号异常", "违规")
SUCCESS_INDICATORS = ("发布成功",)
