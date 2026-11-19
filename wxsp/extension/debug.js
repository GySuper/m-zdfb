// 调试通道:wxsp 经 CDP 调 wxsp.debug.*,在真实页面上单步驱动 / 只读查看 DOM。
// 用于平台改版排查:窗口保持打开,一步步对照选择器,不必每次整轮重跑。
// 红线:不执行任何发布阶段(post)步骤,不点文案为「发布」类的按钮。
import { StepError, Task } from "./lib.js";

const FORBIDDEN_TEXT = new Set(["发布", "一键发布", "立即发布", "确认发布", "发布视频"]);

export function debugApi(getCurrent, setCurrent, platforms) {
  const cur = () => {
    const t = getCurrent();
    if (!t?.tabId) throw new Error("先调 debug.open 打开页面");
    return t;
  };
  // 页面隔离环境里求值(不进页面主环境,不开 Runtime 域)
  async function evalIn(t, expression) {
    const { frameTree } = await t.cdp("Page.getFrameTree");
    const { executionContextId } = await t.cdp("Page.createIsolatedWorld", {
      frameId: frameTree.frame.id,
      worldName: "wxsp-debug",
    });
    const res = await t.cdp("Runtime.evaluate", {
      expression,
      contextId: executionContextId,
      returnByValue: true,
      awaitPromise: true,
    });
    if (res.exceptionDetails) throw new Error(res.exceptionDetails.exception?.description || res.exceptionDetails.text);
    return res.result.value;
  }
  // 点击前在命中点读出元素文案,禁止点发布类按钮
  async function guard(t, x, y) {
    const text = await evalIn(
      t,
      `(() => { const e = document.elementFromPoint(${x}, ${y}); const b = e && (e.closest("button,[role=button]") || e); return (b?.innerText || "").trim(); })()`,
    );
    if (FORBIDDEN_TEXT.has(text)) throw new Error(`调试通道禁止点击「${text}」`);
    return text;
  }
  return {
    async open(job) {
      const t = new Task(job);
      setCurrent(t);
      await t.open(job.url);
      return t.tabId;
    },
    url: () => cur().url(),
    eval: (expression) => evalIn(cur(), expression),
    async step(platform, name) {
      const fn = platforms[platform]?.pre.find(([n]) => n === name)?.[1];
      if (!fn) throw new Error(`没有可调试的发布前步骤 ${platform}.${name}`);
      try {
        await fn(cur());
        return "ok";
      } catch (err) {
        if (err instanceof StepError) return `StepError(${err.kind}): ${err.message}`;
        throw err;
      }
    },
    async click(spec) {
      if (spec.text && FORBIDDEN_TEXT.has(spec.text)) throw new Error(`调试通道禁止点击「${spec.text}」`);
      const t = cur();
      const realClickAt = t.clickAt.bind(t);
      let clicked = "";
      t.clickAt = async (x, y) => {
        clicked = await guard(t, x, y);
        return realClickAt(x, y);
      };
      try {
        await t.click(spec);
      } finally {
        t.clickAt = realClickAt;
      }
      return clicked;
    },
    async clickAt(x, y) {
      const t = cur();
      const text = await guard(t, x, y);
      await t.clickAt(x, y);
      return text;
    },
    type: (text) => cur().type(text),
    fill: (spec, text) => cur().fill(spec, text),
    key: (key, code, vk, text) => cur().key(key, code, vk, text),
    setFiles: (selector, paths) => cur().setFiles(selector, paths),
    count: (spec) => cur().count(spec),
  };
}
