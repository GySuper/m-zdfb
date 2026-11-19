// 插件自检平台:在本地测试页上走一遍 打开 → 可信点击 → 输入 → 上传 → gate → 发布,
// 供集成测试验证插件通路(不碰任何真实平台)。
import { StepError } from "./lib.js";
import { entryPopups } from "./pinduoduo.js";

export const SELFTEST = {
  pre: [
    ["open", (t) => t.open(t.job.url)],
    ["click", (t) => t.click({ sel: "#btn", what: "测试按钮" })],
    ["type", (t) => t.type(t.job.text)],
    ["upload", (t) => t.setFiles('input[type="file"]', [t.job.video_path])],
  ],
  post: [
    [
      "publish",
      async (t) => {
        await t.click({ sel: "button", text: "发布", exact: true, what: "发布按钮" });
        if (!(await t.until(() => t.page(() => (document.getElementById("log")?.textContent || "").includes("published")), 3000))) {
          throw new StepError("element_not_found", "发布后未看到成功标记");
        }
        t.remoteUrl = await t.url();
      },
    ],
  ],
};

// 拼多多入口弹窗处理在本地夹具页上回归(真实事故场景见 tests/test_managed_chrome.py)
export const PDD_POPUPS = {
  pre: [
    ["open", (t) => t.open(t.job.url)],
    ["entry_popups", entryPopups],
  ],
  post: [],
};
