"""拼多多登录相关 URL(Python 侧扫码登录用)。

发布页的选择器已随插件化搬到 wxsp/extension/pinduoduo.js(改版时改那里)。
"""

from __future__ import annotations

LOGIN_URL = (
    "https://mms.pinduoduo.com/login/sso?platform=live&accessType=auto"
    "&redirectUrl=https://live.pinduoduo.com/login/checker%3FisNewCreatorFrom%3Dvideo"
    "%26referUrl%3D%252Fn-creator%252Fvideo%252Fhome%253Ffrom%253Dmms"
    "%2526msfrom%253Dmms_sidenav%26from%3Dmms"
)
# 扫码成功后会跳到这里(不碰 referUrl 坑)
LOGGED_IN_URL_FRAGMENT = "live.pinduoduo.com/n-creator/video/home"
