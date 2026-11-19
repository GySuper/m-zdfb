// 插件后台:wxsp 经 CDP 调 globalThis.wxsp.*,插件经 binding wxspEmit 回传消息。
// 消息:step(进入某步)/ gate(填表完成,停在发布前)/ halted(按 halt_before 停下)
//       / done(发布成功)/ error(kind + step + message)。
// dry-run 不在插件里判断:到 gate 一律停下,只有 wxsp 发 proceed("publish") 才点发布。

import { StepError, Task } from "./lib.js";
import { PINDUODUO } from "./pinduoduo.js";
import { PDD_POPUPS, SELFTEST } from "./selftest.js";

const PLATFORMS = { pinduoduo: PINDUODUO, selftest: SELFTEST, selftest_pdd_popups: PDD_POPUPS };
let current = null;

const emit = (type, data = {}) => globalThis.wxspEmit(JSON.stringify({ type, ...data }));

async function runJob(job) {
  const task = new Task(job);
  current = task;
  let step = "init";
  try {
    const plat = PLATFORMS[job.platform];
    if (!plat) throw new StepError("unknown", `插件不支持平台 ${job.platform}`);
    for (const [name, fn] of plat.pre) {
      if (name === job.halt_before) {
        emit("halted", { step: name });
        return;
      }
      step = name;
      emit("step", { step });
      await fn(task);
      await task.pause();
    }
    emit("gate");
    const action = await new Promise((resolve) => {
      task.release = resolve;
    });
    if (action !== "publish") return;
    if (!plat.post.length) throw new StepError("element_not_found", "发布步骤尚未实现");
    for (const [name, fn] of plat.post) {
      step = name;
      emit("step", { step });
      await fn(task);
    }
    emit("done", { remote_url: task.remoteUrl });
  } catch (err) {
    emit("error", { kind: err.kind || "unknown", step, message: String(err?.message || err) });
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
};
