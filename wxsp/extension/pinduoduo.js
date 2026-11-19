// 拼多多多多视频发布步骤(插件版)。选择器与等待策略移植自原 patchright 实现
// (2026-07-22 对真实账号真发实测);输入全部改为 chrome.debugger 可信输入。
import { StepError, sleep } from "./lib.js";

const HOME_URL = "https://live.pinduoduo.com/n-creator/video/home"; // 发布页 = 首页(SPA)
const LOGIN_FRAG = "pinduoduo.com/login";
const HOME_FRAG = "live.pinduoduo.com/n-creator/video/home";
const SUCCESS_FRAG = "n-creator/video/mall-goods-video"; // 点发布后跳转商家带货视频页(无 toast)

const VIDEO_FILE_INPUT = 'input[type="file"][accept*=".mp4"]';
const UPLOAD_DONE_TEXT = "视频上传成功";
const DESC_EDITOR = 'div[contenteditable="true"].sabo-root';
const DESC_MAX_LENGTH = 500; // 页内计数器 N/500
const ANY = "button, a, span, div, label, li, p";

const ENTRY_DIALOG = '[role="dialog"], [aria-modal="true"], [class*="MDL_modal_"]';
const ENTRY_DIALOG_CLOSE = [
  'button[aria-label*="关闭"]',
  '[role="button"][aria-label*="关闭"]',
  'button[aria-label*="close" i]',
  '[role="button"][aria-label*="close" i]',
  '[class*="MDL_closeBtn_"]',
  '[class*="MDL_headerCloseIcon_"]',
  '[data-testid="beast-core-icon-close"]',
]
  // 页面内嵌提示条(如上传后「该视频未匹配到有效商品」)的关闭图标不是弹窗,不碰
  .map((sel) => `${sel}:not([data-testid="beast-core-noticeBar"] *)`)
  .join(", ");

// SSO 登录页「账号登录」表单(2026-11-18 探查:无协议勾选、无图形验证码输入框)
const USERNAME_INPUT = "#usernameId";
const PASSWORD_INPUT = "#passwordId";
const PRODUCT_ID_INPUT = 'input[placeholder*="商品id"]';
const COVER_FILE_INPUT = 'input[type="file"][accept*=".jpg"]';
// beast-core 日期/时间框都是 readonly,只能走面板 UI
const SCHEDULE_DATE_INPUT = 'input[data-testid="beast-core-datePicker-htmlInput"]'; // YYYY-MM-DD HH:MM:SS
const TIME_PICKER = '[data-testid="beast-core-timePicker-input"]';
const TIME_COLUMN = 'ul:has(> li[class*="cIL_item"])';
const CALENDAR_HEADER = '[data-testid="beast-core-datePicker-dropdown-header"]';
const MODAL = '[data-testid="beast-core-modal"]';
const DATE_CELL = '[role="date-cell"]:not([class*="disabled"]):not([class*="outOfMonth"])';
const DECLARATION_TRIGGER = 'input[data-testid="beast-core-select-htmlInput"]';
const DECLARATIONS = ["内容无需标注", "含AI生成内容", "含虚构演绎内容", "内容含营销信息", "内容为转载", "个人观点，仅供参考"];
const DECLARATION_DEFAULT = "内容无需标注";
const RISK_CONTROL_KEYWORDS = ["操作频繁", "操作过于频繁", "请稍后", "账号异常", "违规"];

const wait = (lo, hi) => sleep(lo + Math.random() * (hi - lo));

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
    if (!t.job.login) throw new StepError("cookie_expired", "拼多多登录态失效(停在 SSO 登录页,需重新扫码)");
    // 登录过期时打开视频首页实测落到「请选择登录身份」页(没有表单),所以直接开带表单的 SSO 登录页
    await passwordLogin(t, t.job.login_url, async () => (await t.url()).includes(HOME_FRAG));
    t.emit("relogin");
    // 登录后的跳转链可能还没走完就到了首页地址,接着还会重载一次(实测把刚上传的视频冲掉);
    // 重新打开一次干净的发布页再往下走
    await sleep(3000);
    await t.goto(HOME_URL);
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

// 关闭平台主动弹出的干扰弹窗:只点弹窗自身的关闭控件或按 Escape,不点任何业务按钮。
// 只能在自动化还没主动打开任何业务弹窗时调用:进入发布页后(entry_popups)、
// 上传完成后(upload_popups,实测会弹「立即推广 冲顶交易额」盖住描述框)。
export async function entryPopups(t) {
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

// 上传完成 = 页面出现「文件名:<本次文件>」且有一行恰好是「视频上传成功」。
// 不能只搜「视频上传成功」:空发布页底部「平台发布激励」卡片里就有「视频上传成功后,…」(实测误判)。
export async function upload(t) {
  const name = t.job.video_path.split(/[\\/]/).pop();
  await t.setFiles(VIDEO_FILE_INPUT, [t.job.video_path]);
  const startMs = Math.min(20_000, t.job.upload_timeout_s * 1000);
  const started = await t.until(() => t.hasText(`文件名：${name}`), startMs, 500);
  if (!started) throw new StepError("upload_failed", "选择视频后页面没有进入上传状态");
  const done = await t.until(async () => (await pageLines(t)).has(UPLOAD_DONE_TEXT), t.job.upload_timeout_s * 1000, 1000);
  if (!done) throw new StepError("upload_failed", "视频上传/处理超时");
  console.log("视频上传完成");
}

async function desc(t) {
  if (!t.job.description) return;
  if (!(await t.waitVisible({ sel: DESC_EDITOR }, 10_000))) throw new StepError("element_not_found", "描述输入框未出现");
  await t.click({ sel: DESC_EDITOR, what: "描述输入框" });
  await wait(500, 1000);
  await t.type(t.job.description.slice(0, DESC_MAX_LENGTH));
}

// 话题写在描述框里:#话题 → 等候选 → 空格确认
async function tags(t) {
  if (!t.job.tags.length) return;
  await t.click({ sel: DESC_EDITOR, what: "描述输入框" });
  for (const tag of t.job.tags) {
    await t.type(`#${tag}`);
    await sleep(1500);
    await t.key(" ", "Space", 32, " ");
  }
}

// 账号密码自动登录(账号开了「账密登录」时)。点登录后没进到首页 —— 滑块 / 短信验证 /
// 密码错误等 —— 一律按登录失效处理,报错里带上页面新冒出的提示。密码只用于输入,不进任何日志。
export async function passwordLogin(t, loginUrl, loggedIn) {
  const { username, password } = t.job.login;
  await t.goto(loginUrl);
  // SSO 页实测 5s 左右才渲染出表单
  if (!(await t.waitVisible({ sel: ANY, text: "账号登录", exact: true }, 20_000))) {
    throw new StepError("cookie_expired", "登录页没出现「账号登录」,无法自动登录,需人工扫码");
  }
  await t.click({ sel: ANY, text: "账号登录", exact: true, what: "账号登录页签" });
  await wait(800, 1500);
  if (!(await t.waitVisible({ sel: USERNAME_INPUT }, 10_000))) {
    throw new StepError("cookie_expired", "账号登录表单没出现,无法自动登录,需人工扫码");
  }
  await t.click({ sel: USERNAME_INPUT, what: "登录账号输入框" });
  await t.type(username);
  await wait(400, 900);
  await t.click({ sel: PASSWORD_INPUT, what: "登录密码输入框" });
  await t.type(password);
  await wait(500, 1000);
  const before = await pageLines(t);
  await t.click({ sel: "button", text: "登录", exact: true, what: "登录按钮" });
  const hints = [];
  const ok = await t.until(async () => {
    if (await loggedIn()) return true;
    for (const line of await pageLines(t)) {
      if (!before.has(line) && line.length <= 60 && !hints.includes(line)) hints.push(line);
    }
    return false;
  }, 30_000, 1000);
  if (!ok) {
    const why = hints.length ? `页面提示:${hints.slice(0, 3).join(" / ")}` : "可能需要滑块或短信验证";
    throw new StepError("cookie_expired", `账号密码自动登录没成功(${why}),需人工扫码`);
  }
  console.log("已用账号密码自动登录");
}

async function pageLines(t) {
  const text = await t.page(() => document.body.innerText);
  return new Set(text.split("\n").map((l) => l.trim()).filter(Boolean));
}

// 点「添加商品」→ 切「商品ID」tab → 逐个填 ID → 下一步绑定 → 等「删除商品」出现
async function products(t) {
  await t.click({ sel: ANY, text: "添加商品", what: "添加商品" });
  await wait(1000, 2000);
  await t.click({ sel: ANY, text: "商品ID", what: "商品ID 页签" });
  await wait(800, 1500);
  const hints = [];
  for (const pid of t.job.product_ids) {
    await t.click({ sel: PRODUCT_ID_INPUT, what: "商品ID 输入框" });
    await t.type(pid);
    await wait(300, 600);
    const before = await pageLines(t);
    await t.click({ sel: "button", text: "下一步", what: "下一步" });
    // 平台的「商品不存在」等轻提示几秒就消失,点完立刻收集新冒出的文字写进报错
    for (let i = 0; i < 6; i++) {
      await sleep(500);
      for (const line of await pageLines(t)) {
        if (!before.has(line) && line.length <= 60 && !hints.includes(line)) hints.push(line);
      }
    }
  }
  if (!(await t.waitVisible({ sel: "button", text: "删除商品" }, 10_000))) {
    const hint = hints.length ? `;页面提示:${hints.slice(0, 3).join(" / ")}` : ";页面无提示";
    throw new StepError("product_not_found", `商品绑定失败(ID: ${t.job.product_ids.join(",")})${hint}`);
  }
  console.log(`绑定商品完成 ${t.job.product_ids.join(",")}`);
}

// 自定义封面:可选,失败降级为平台默认封面(不中断发布)
async function cover(t) {
  if (!t.job.cover_path) return;
  try {
    await t.click({ sel: "button", text: "编辑封面", what: "编辑封面" });
    await wait(1500, 2500);
    await t.click({ sel: ANY, text: "本地上传", what: "本地上传" });
    await wait(800, 1500);
    if (!(await t.until(() => t.page((s) => !!document.querySelector(s), COVER_FILE_INPUT), 10_000))) {
      throw new Error("封面上传框未出现");
    }
    await t.setFiles(COVER_FILE_INPUT, [t.job.cover_path]);
    // 「封面上传中」期间确定按钮是禁用的(实测点了无效),等传完
    await wait(1000, 1500);
    if (!(await t.until(async () => !(await t.hasText("封面上传中")), 30_000, 500))) throw new Error("封面上传超时");
    await wait(500, 1000);
    // 实测传完弹窗有时自己关;还开着才点确定
    if ((await t.count({ sel: MODAL })) > 0) {
      await t.click({ within: MODAL, sel: "button", text: "确定", exact: true, what: "封面确定" });
    }
    if (!(await t.until(async () => (await t.count({ sel: MODAL })) === 0, 10_000))) throw new Error("封面弹窗没有关闭");
    console.log("自定义封面设置完成");
  } catch (err) {
    console.warn(`封面设置失败(降级为平台默认封面): ${err.message}`);
  }
}

// 绑商品后出现的两组单选:商详悬浮窗(展示/不展示)、同步店铺动态(开启同步/取消同步)。
// 飞书留空 = 保持页面默认;填了就点对应选项并核对确实选中。
async function publishOptions(t) {
  for (const choice of [t.job.detail_float, t.job.store_feed]) {
    if (!choice) continue;
    await t.click({ sel: ANY, text: choice, exact: true, what: `「${choice}」选项` });
    await wait(300, 700);
    const checked = await t.page(
      (c) => [...document.querySelectorAll('input[type="radio"]')].some((r) => r.checked && (r.closest("label") || r.parentElement).innerText.trim() === c),
      choice,
    );
    if (!checked) throw new StepError("element_not_found", `「${choice}」没有选中`);
  }
}

// 日历翻到目标年月:头部是年份下拉(值如「2026年」)+ 月份文字(如「12月」)+ 左右箭头
async function gotoMonth(t, year, month) {
  for (let i = 0; i <= 12; i++) {
    const ym = await t.page((h) => {
      const head = document.querySelector(h);
      return head ? [parseInt(head.querySelector("input")?.value, 10), parseInt(head.querySelector('[class*="RPR_dateText"]')?.textContent, 10)] : null;
    }, CALENDAR_HEADER);
    if (!ym || Number.isNaN(ym[0]) || Number.isNaN(ym[1])) throw new StepError("element_not_found", "日历头部年月未找到");
    const diff = (year - ym[0]) * 12 + (month - ym[1]);
    if (diff === 0) return;
    const dir = diff > 0 ? "right" : "left";
    await t.click({ sel: `${CALENDAR_HEADER} [data-testid="beast-core-icon-${dir}"]`, what: diff > 0 ? "日历下个月" : "日历上个月" });
    await wait(400, 800);
  }
  throw new StepError("element_not_found", `日历切不到 ${year}年${month}月`);
}

// 定时发布:选 radio → 日历点日期格子 → 时间滚轮三列各点目标项 → 确认 → 读回日期框逐字核对
async function schedule(t) {
  const target = t.job.publish_at; // "YYYY-MM-DD HH:MM:SS"
  const day = String(Number(target.slice(8, 10)));
  const [hh, mm, ss] = target.slice(11).split(":");
  await t.click({ sel: ANY, text: "定时发布", exact: true, what: "定时发布" });
  await wait(1000, 2000);
  if (!(await t.waitVisible({ sel: SCHEDULE_DATE_INPUT }, 10_000))) throw new StepError("element_not_found", "定时日期框未出现");
  await t.click({ sel: SCHEDULE_DATE_INPUT, what: "定时日期框" });
  await wait(1500, 2000);
  await gotoMonth(t, Number(target.slice(0, 4)), Number(target.slice(5, 7)));
  await t.click({ sel: DATE_CELL, text: day, exact: true, what: `日历 ${day} 号` });
  await wait(500, 1000);
  await t.click({ sel: TIME_PICKER, what: "时间选择框" });
  await wait(1000, 1500);
  for (const [i, v] of [hh, mm, ss].entries()) {
    await t.click({ within: TIME_COLUMN, withinIndex: i, sel: 'li[class*="cIL_item"]', text: v, exact: true, what: `时间滚轮第${i + 1}列 ${v}` });
    await wait(300, 600);
  }
  await t.click({ sel: "button", text: "确认", what: "定时确认" }); // 只选不确认会丢失
  await wait(1500, 2500);
  const actual = await t.inputValue(SCHEDULE_DATE_INPUT);
  if (actual !== target) {
    throw new StepError("element_not_found", `定时时间没设对:页面 ${actual},应为 ${target}`);
  }
}

async function declaration(t) {
  const choice = DECLARATIONS.includes(t.job.declaration) ? t.job.declaration : DECLARATION_DEFAULT;
  await t.click({ sel: DECLARATION_TRIGGER, what: "内容声明下拉" });
  await wait(800, 1500);
  await t.click({ sel: ANY, text: choice, what: `内容声明「${choice}」` });
  await wait(500, 1000);
  console.log(`内容声明已选: ${choice}`);
}

async function risk(t) {
  const text = await t.page(() => document.body.innerText);
  const hit = RISK_CONTROL_KEYWORDS.find((kw) => text.includes(kw));
  if (hit) throw new StepError("risk_control", `页面命中风控关键词: ${hit}`);
}

// 主发布按钮文案就是「发布」二字(排除顶部「发布视频」、底部「一键发布」/「取消发布」)
async function publish(t) {
  await t.click({ sel: "button", text: "发布", exact: true, what: "发布按钮" });
  await wait(2500, 4500);
}

async function waitSuccess(t) {
  if (!(await t.until(async () => (await t.url()).includes(SUCCESS_FRAG), 60_000, 500))) {
    throw new StepError("element_not_found", "发布成功判定超时(URL 未跳转 mall-goods-video)");
  }
  console.log("已跳转商家带货视频页,发布成功");
}

export const PINDUODUO = {
  pre: [
    ["open_publish", openPublish],
    ["verify_login", verifyLogin],
    ["entry_popups", entryPopups],
    ["upload", upload],
    ["upload_popups", entryPopups],
    ["desc", desc],
    ["tags", tags],
    ["products", products],
    ["cover", cover],
    ["publish_options", publishOptions],
    ["schedule", schedule],
    ["declaration", declaration],
    ["risk", risk],
  ],
  post: [
    ["publish", publish],
    ["wait_success", waitSuccess],
  ],
};
