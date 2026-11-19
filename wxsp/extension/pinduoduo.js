// 拼多多多多视频发布步骤(插件版)。选择器与等待策略移植自
// wxsp/platforms/pinduoduo.py + pinduoduo_selectors.py(2026-07-22 真发实测)。
// P1:打开发布页 → 校验登录 → 关入口弹窗;其余步骤 P2 移植。
import { StepError, sleep } from "./lib.js";

const HOME_URL = "https://live.pinduoduo.com/n-creator/video/home"; // 发布页 = 首页(SPA)
const LOGIN_FRAG = "pinduoduo.com/login";
const HOME_FRAG = "live.pinduoduo.com/n-creator/video/home";
const VIDEO_FILE_INPUT = 'input[type="file"][accept*=".mp4"]';
const ENTRY_DIALOG = '[role="dialog"], [aria-modal="true"], [class*="MDL_modal_"]';
const ENTRY_DIALOG_CLOSE = [
  'button[aria-label*="关闭"]',
  '[role="button"][aria-label*="关闭"]',
  'button[aria-label*="close" i]',
  '[role="button"][aria-label*="close" i]',
  '[class*="MDL_closeBtn_"]',
  '[class*="MDL_headerCloseIcon_"]',
  '[data-testid="beast-core-icon-close"]',
].join(", ");

async function openPublish(t) {
  await t.open(HOME_URL);
  const landed = await t.until(async () => {
    const u = await t.url();
    return u.includes(LOGIN_FRAG) || u.includes(HOME_FRAG);
  }, 30_000);
  if (!landed) throw new StepError("network", "拼多多发布页加载超时");
}

async function verifyLogin(t) {
  if ((await t.url()).includes(LOGIN_FRAG)) {
    throw new StepError("cookie_expired", "拼多多登录态失效(停在 SSO 登录页,需重新扫码)");
  }
  // 正向确认:上传框是隐藏的 file input,只判断存在
  const ok = await t.until(() => t.page((sel) => !!document.querySelector(sel), VIDEO_FILE_INPUT), 8_000);
  if (!ok) throw new StepError("cookie_expired", "拼多多登录态失效(上传入口未出现,需重新扫码登录)");
}

// 等弹窗数量下降(最多 2s)
async function countDropped(t, spec, before) {
  for (let i = 0; i < 8; i++) {
    await sleep(250);
    if ((await t.count(spec)) < before) return true;
  }
  return false;
}

// 关闭进入发布页时的未知干扰弹窗:只点弹窗自身的关闭控件或按 Escape,不点任何业务按钮。
async function entryPopups(t) {
  const MAX_POPUPS = 5;
  let quiet = 0;
  let dismissed = 0;
  for (let poll = 0; poll < 40; poll++) {
    const dialogs = await t.count({ sel: ENTRY_DIALOG });
    // 有弹窗容器 → 点最上层弹窗里的关闭;没有容器的促销浮层 → 点全局最后一个关闭控件
    const closeSpec = dialogs
      ? { within: ENTRY_DIALOG, sel: ENTRY_DIALOG_CLOSE, pick: "first", what: "弹窗关闭按钮" }
      : { sel: ENTRY_DIALOG_CLOSE, pick: "last", what: "浮层关闭按钮" };
    const watch = dialogs ? { sel: ENTRY_DIALOG } : { sel: ENTRY_DIALOG_CLOSE };
    const before = dialogs || (await t.count(watch));
    if (!before) {
      if (++quiet >= 8) {
        if (dismissed) console.log(`已关闭入口干扰弹窗 ${dismissed} 个`);
        return; // 连续 2 秒无弹窗,页面稳定
      }
      await sleep(250);
      continue;
    }
    quiet = 0;
    if (dismissed >= MAX_POPUPS) throw new StepError("element_not_found", `拼多多入口弹窗超过 ${MAX_POPUPS} 个,停止自动关闭`);
    let closed = false;
    if ((await t.count(closeSpec)) > 0) {
      try {
        await t.click(closeSpec);
        closed = await countDropped(t, watch, before);
      } catch {
        // 关闭按钮点不到就退回 Escape
      }
    }
    if (!closed) {
      await t.key("Escape", "Escape", 27);
      closed = await countDropped(t, watch, before);
    }
    if (!closed) throw new StepError("element_not_found", "拼多多入口弹窗无法安全关闭(仅允许关闭按钮或 Escape)");
    dismissed++;
  }
  throw new StepError("element_not_found", "拼多多入口弹窗持续出现,页面未在 10 秒内稳定");
}

export const PINDUODUO = {
  pre: [
    ["open_publish", openPublish],
    ["verify_login", verifyLogin],
    ["entry_popups", entryPopups],
  ],
  post: [],
};
