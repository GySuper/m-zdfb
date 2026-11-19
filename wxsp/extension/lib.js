// 插件公共原语:标签页 / 等待 / 页面探测 / 可信输入。
// 页面里只做只读 DOM 探测(chrome.scripting,isolated world);所有输入走
// chrome.debugger(Input.*),页面收到的事件 isTrusted=true。
// 点击链路移植自 tmall-review-collector 的 rpa-input / rpa-target:
// 视口外先滚入 → 两帧矩形稳定 → elementFromPoint 命中校验 → 三段鼠标序列带随机停顿。

export class StepError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const rand = (lo, hi) => lo + Math.random() * (hi - lo);

// 在页面内执行(chrome.scripting 序列化函数,必须自包含)。
// spec: { sel, within?, withinIndex?, text?, exact?, pick?: "first" | "last" }
// within 默认取最后一个可见容器(最上层弹窗);withinIndex 取第 N 个(如时/分/秒三列)。
// 按 text 过滤时只留最内层匹配(同 Playwright text=),避免点到外层大容器。
// mode "count" 返回可见匹配数;"probe" 返回 {x, y} / {need_scroll, ax, ay} / {error}。
async function probeInPage(spec, mode) {
  const visible = (e) => {
    const s = getComputedStyle(e);
    const r = e.getBoundingClientRect();
    return s.display !== "none" && s.visibility !== "hidden" && r.width > 0 && r.height > 0;
  };
  let root = document;
  if (spec.within) {
    const boxes = [...document.querySelectorAll(spec.within)].filter(visible);
    root = spec.withinIndex != null ? boxes[spec.withinIndex] : boxes[boxes.length - 1];
    if (!root) return mode === "count" ? 0 : { error: "所在弹窗未出现" };
  }
  let els = [...root.querySelectorAll(spec.sel)].filter(visible);
  if (spec.text != null) {
    els = els.filter((e) => {
      const t = (e.textContent || "").trim();
      return spec.exact ? t === spec.text : t.includes(spec.text);
    });
    els = els.filter((e) => !els.some((o) => o !== e && e.contains(o)));
  }
  if (mode === "count") return els.length;
  const el = spec.pick === "last" ? els[els.length - 1] : els[0];
  if (!el) return { error: "未找到" };
  // 两帧稳定(后台标签页 rAF 可能停转:120ms 定时器兜底,总预算 250ms)
  const nextFrame = () =>
    new Promise((resolve) => {
      const timer = setTimeout(resolve, 120);
      requestAnimationFrame(() => {
        clearTimeout(timer);
        resolve();
      });
    });
  let rect = el.getBoundingClientRect();
  const deadline = Date.now() + 250;
  while (Date.now() < deadline) {
    await nextFrame();
    const next = el.getBoundingClientRect();
    const stable = Math.abs(rect.left - next.left) < 1 && Math.abs(rect.top - next.top) < 1 && Math.abs(rect.width - next.width) < 1;
    rect = next;
    if (stable) break;
  }
  const vw = document.documentElement.clientWidth || innerWidth;
  const vh = document.documentElement.clientHeight || innerHeight;
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;
  // 被可滚动容器裁掉(如时间滚轮的一列):在容器上方滚,而不是视口中心(实测会判成被遮挡)。
  // 也认 overflow:hidden —— 拼多多 TimePicker 平时 hidden、悬停才 auto,滚轮前会先把鼠标移上去。
  for (let box = el.parentElement; box && box !== document.body; box = box.parentElement) {
    const oy = getComputedStyle(box).overflowY;
    if (!/(auto|scroll|hidden)/.test(oy) || box.scrollHeight <= box.clientHeight + 1) continue;
    const b = box.getBoundingClientRect();
    const cy = b.top + b.height / 2;
    if ((rect.top < b.top || rect.bottom > b.bottom) && cy >= 0 && cy < vh) {
      return { need_scroll: y - cy, ax: Math.max(1, Math.min(vw - 1, b.left + b.width / 2)), ay: cy };
    }
    break;
  }
  if (x < 0 || x >= vw) return { error: "在视口外" };
  if (y < 0 || y >= vh) return { need_scroll: y - vh / 2, ax: Math.max(30, Math.min(vw - 30, x)), ay: vh / 2 };
  const hit = document.elementFromPoint(x, y);
  if (!hit || (hit !== el && !el.contains(hit) && !hit.contains(el))) return { error: "被遮挡" };
  return { x, y };
}

export class Task {
  constructor(job) {
    this.job = job;
    this.tabId = null;
    this.remoteUrl = null;
    this.release = null;
  }

  async open(url) {
    const tab = await chrome.tabs.create({ url, active: true });
    this.tabId = tab.id;
    await chrome.debugger.attach({ tabId: tab.id }, "1.3");
    // 不开焦点仿真,后台/失焦窗口里的点击不生效(P0 实测)
    await this.cdp("Emulation.setFocusEmulationEnabled", { enabled: true });
  }

  cdp(method, params = {}) {
    return chrome.debugger.sendCommand({ tabId: this.tabId }, method, params);
  }

  async url() {
    return (await chrome.tabs.get(this.tabId)).url || "";
  }

  async page(func, ...args) {
    const [res] = await chrome.scripting.executeScript({ target: { tabId: this.tabId }, func, args });
    return res?.result;
  }

  // 轮询 check() 直到真值或超时;页面跳转中的瞬时异常当作"还没好"。
  async until(check, timeoutMs, intervalMs = 250) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const v = await check();
        if (v) return v;
      } catch {
        // 页面导航中,下一轮再试
      }
      await sleep(intervalMs);
    }
    return null;
  }

  count(spec) {
    return this.page(probeInPage, spec, "count");
  }

  waitVisible(spec, timeoutMs) {
    return this.until(async () => (await this.count(spec)) > 0, timeoutMs);
  }

  pause() {
    const [lo, hi] = this.job.step_pause || [1, 3];
    return sleep(rand(lo, hi) * 1000);
  }

  async click(spec) {
    const what = spec.what || spec.sel;
    // 每次最多滚 650px,8 次覆盖约 5000px 的长表单
    for (let i = 0; i < 8; i++) {
      const p = await this.page(probeInPage, spec, "probe");
      if (p && typeof p.x === "number") {
        await this.clickAt(p.x, p.y);
        return;
      }
      if (p && typeof p.need_scroll === "number") {
        await this.wheelAt(p.ax, p.ay, Math.max(-650, Math.min(650, p.need_scroll)));
        await sleep(rand(250, 450));
        continue;
      }
      throw new StepError("element_not_found", `${what}${p?.error || "未找到"}`);
    }
    throw new StepError("element_not_found", `${what}滚动后仍不在视口`);
  }

  async clickAt(x, y) {
    await this.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
    await sleep(rand(80, 180));
    await this.cdp("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
    await sleep(rand(60, 140));
    await this.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
  }

  async wheelAt(x, y, deltaY) {
    await this.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
    const steps = Math.max(1, Math.min(6, Math.ceil(Math.abs(deltaY) / 450)));
    let remaining = deltaY;
    for (let i = 0; i < steps; i++) {
      const step = remaining / (steps - i);
      await this.cdp("Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: 0, deltaY: step });
      remaining -= step;
      if (i + 1 < steps) await sleep(rand(60, 140));
    }
  }

  // text 非空时按键会输入字符(如空格)
  async key(key, code = key, windowsVirtualKeyCode = 0, text = undefined) {
    await this.cdp("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode, text });
    await sleep(rand(40, 80));
    await this.cdp("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode });
  }

  // 分段插入文字,段间随机停顿,不一次性灌入
  async type(text) {
    for (let i = 0; i < text.length; ) {
      const n = Math.ceil(rand(2, 6));
      await this.cdp("Input.insertText", { text: text.slice(i, i + n) });
      i += n;
      await sleep(rand(60, 180));
    }
  }

  hasText(text) {
    return this.page((t) => document.body.innerText.includes(t), text);
  }

  // 输入框/只读框的当前值(只读 DOM)
  inputValue(selector) {
    return this.page((sel) => document.querySelector(sel)?.value ?? null, selector);
  }

  async setFiles(selector, paths) {
    const { root } = await this.cdp("DOM.getDocument", { depth: 0 });
    const { nodeId } = await this.cdp("DOM.querySelector", { nodeId: root.nodeId, selector });
    if (!nodeId) throw new StepError("element_not_found", `上传框未找到:${selector}`);
    await this.cdp("DOM.setFileInputFiles", { nodeId, files: paths });
  }

  async screenshot() {
    return (await this.cdp("Page.captureScreenshot", { format: "png" })).data;
  }

  html() {
    return this.page(() => document.documentElement.outerHTML);
  }
}
