"""反检测 init script 常量(M2,按需使用)。

复用自 OpenCLI 项目的反检测代码(MIT 兼容)。

**当前默认不注入**:视频号 2026 反爬升级后,该脚本里
`navigator.webdriver=undefined`(正常浏览器为 `false`)、伪造的 `plugins`
列表等特征会被服务端在 TLS 层识别为自动化工具,主动断连
(`ERR_CONNECTION_CLOSED`)。patchright 自身的 CDP 指纹修补在 M2 验证够用。

保留此常量是为了:M5 publisher 若发现 patchright 兜底不住时,可以选择性、
按页面 / 按场景显式注入,而不是 `browser_context` 一刀切默认开启。
"""

INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""
