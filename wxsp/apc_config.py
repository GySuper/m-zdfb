"""APC 凭据(打包时被 build 脚本替换;源码状态是占位符)。

打包脚本 scripts/build_macos.sh / build_windows.ps1 会用 EXIT trap 保证
patch 进 git 工作树后**总会** revert,源码克隆者永远看到占位符。
"""

APC_ENDPOINT = "__APC_ENDPOINT__"
APC_APP_ID = "__APC_APP_ID__"
APC_APP_SECRET = "__APC_APP_SECRET__"
APC_PUBLIC_KEY = "__APC_PUBLIC_KEY__"
APC_CERT_FP = "__APC_CERT_FP__"
