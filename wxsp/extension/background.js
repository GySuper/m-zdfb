// 插件后台:wxsp 经 CDP 调 globalThis.wxsp.*,插件经 binding wxspEmit 回传消息。
// 消息:step(进入某步)/ gate(填表完成,停在发布前)/ halted(按 halt_before 停下)
//       / done(发布成功)/ error(kind + step + message)。
// dry-run 不在插件里判断:到 gate 一律停下,只有 wxsp 发 proceed("publish") 才点发布。

import { debugApi } from "./debug.js";
import { StepError, Task } from "./lib.js";
import { PINDUODUO } from "./pinduoduo.js";
import { PDD_GUARD, PDD_LOGIN, PDD_POPUPS, PDD_UPLOAD, SELFTEST } from "./selftest.js";

const PLATFORMS = { pinduoduo: PINDUODUO, selftest: SELFTEST, selftest_pdd_popups: PDD_POPUPS, selftest_pdd_login: PDD_LOGIN, selftest_pdd_upload: PDD_UPLOAD, selftest_pdd_guard: PDD_GUARD };
let current = null;

const emit = (type, data = {}) => globalThis.wxspEmit(JSON.stringify({ type, ...data }));

async function runJob(job) {
  // 新一轮(重跑)顶替旧任务:旧任务的等待立即退出、消息不再上报,并关掉它的标签页
  if (current && !current.aborted) {
    current.aborted = true;
    if (current.tabId != null) chrome.tabs.remove(current.tabId).catch(() => {});
  }
  const task = new Task(job);
  const say = (type, data) => {
    if (!task.aborted) emit(type, data);
  };
  task.emit = say; // 步骤里需要额外上报(如 relogin)时用
  current = task;
  let step = "init";
  try {
    const plat = PLATFORMS[job.platform];
    if (!plat) throw new StepError("unknown", `插件不支持平台 ${job.platform}`);
    for (const [name, fn] of plat.pre) {
      if (name === job.halt_before) {
        say("halted", { step: name });
        return;
      }
      task.checkAborted();
      step = name;
      say("step", { step });
      if (plat.guard) await plat.guard(task, name); // 步骤间保护:掉登录 / 干扰弹窗
      await fn(task);
      await task.pause();
    }
    say("gate");
    const action = await new Promise((resolve) => {
      task.release = resolve;
    });
    if (action !== "publish") return;
    if (!plat.post.length) throw new StepError("element_not_found", "发布步骤尚未实现");
    for (const [name, fn] of plat.post) {
      step = name;
      say("step", { step });
      await fn(task);
    }
    say("done", { remote_url: task.remoteUrl });
  } catch (err) {
    say("error", { kind: err.kind || "unknown", step, message: String(err?.message || err) });
  }
}

globalThis.wxsp = {
  startJob(job) {
    runJob(job);
    return true;
  },
  proceed(action) {
    current?.release?.(action);
    return true;
  },
  screenshot() {
    return current.screenshot();
  },
  html() {
    return current.html();
  },
  debug: debugApi(
    () => current,
    (t) => {
      current = t;
    },
    PLATFORMS,
  ),
};
