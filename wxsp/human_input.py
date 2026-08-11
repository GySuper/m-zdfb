"""物理输入层:用 pyautogui 做 OS 级鼠标/键盘操作,实现 isTrusted=true 事件。

核心动机:patchright/puppeteer 的 mouse.click / keyboard.type 走 CDP 协议
(Input.dispatchMouseEvent),浏览器收到的事件 isTrusted=false —— 小红书风控
检测这个属性判定自动化。pyautogui 走 OS 的 SendInput / CGEvent,浏览器收到
isTrusted=true 的真实硬件事件,和真人操作 / RPA 工具完全一致。

patchright 在此模块里只做**定位**(用选择器找到元素在屏幕上的坐标)和**等待**
(等元素出现/上传完成),所有"手"的动作交给 pyautogui。

坐标转换:patchright bounding_box() 返回视口坐标(浏览器内容区左上角为原点),
pyautogui 用屏幕绝对坐标(主显示器左上角为原点)。两者差一个浏览器窗口偏移:
  screenX = window.screenX + (outerWidth-innerWidth)/2 + box.x + box.width/2
  screenY = window.screenY + (outerHeight-innerHeight) + box.y + box.height/2
"""

from __future__ import annotations

import random
import time
from typing import Any

import pyautogui  # type: ignore[import-untyped]
from loguru import logger
from patchright.sync_api import Locator, Page

# pyautogui 安全保护:鼠标移到屏幕左上角 (0,0) 时中止(防失控)。FAILSAFE 可能和
# 窗口最大化冲突(鼠标自然到 0,0),生产环境关闭。改成用 pyautogui.FAILSAFE = False。
pyautogui.FAILSAFE = False

# 底部 fixed/sticky bar 预留高度(px,CSS 视口坐标)。发布页底部常有固定的
# 「发布/暂存」按钮栏挡住正文编辑器下沿,物理点击会点飞到 bar 上。
# _box_center_screen 在元素中心离视口底 < 此值时,把点击点上移到 box 上 1/4 避让。
# 按实测数据反推 bar 约 70px,取 100px 留足余量。
_BOTTOM_BAR_RESERVE = 100

# 标点/空格打字时停顿更长(真人在标点处会慢下来)
_PUNCTUATION = set("，。！？、；：, .!?;:\n")  # noqa: RUF001  # 含中文全角标点(有意数据)

# pyautogui 按键名映射(patchright → pyautogui)
_KEY_MAP = {
    "Backspace": "backspace",
    "Enter": "enter",
    "Space": "space",
    "Escape": "escape",
    "Tab": "tab",
    "ArrowDown": "down",
    "ArrowUp": "up",
    "ArrowLeft": "left",
    "ArrowRight": "right",
}


def _window_offset(page: Page) -> dict[str, float]:
    """取浏览器窗口在屏幕上的偏移量(一次 evaluate)。

    screenX/screenY:浏览器窗口外框左上角的屏幕坐标。
    (outerWidth-innerWidth)/2:左右窗口边框宽度(Windows 有边框,macOS 通常 0)。
    (outerHeight-innerHeight):工具栏+标签栏+地址栏总高度(视口顶到窗口顶的距离)。
    """
    geo: dict[str, float] = page.evaluate(
        """() => ({
            sx: window.screenX, sy: window.screenY,
            dx: (window.outerWidth - window.innerWidth) / 2,
            dy: window.outerHeight - window.innerHeight,
        })"""
    )
    return geo


def _box_center_screen(page: Page, locator: Locator) -> tuple[float, float] | None:
    """取 locator 元素中心的屏幕绝对坐标。

    底部 fixed bar 避让:若元素中心离视口底部太近(< _BOTTOM_BAR_RESERVE,
    预留给底部固定 bar 如发布按钮栏的高度),点击点 y 上移到 box 上 1/4,
    让点击落在 bar 之上的可见区域。仅当元素高度足以避让时才上移,
    否则保持中心(极矮元素无避让空间,点中心最稳)。
    """
    box = locator.bounding_box()
    if box is None:
        return None
    geo = _window_offset(page)
    cx = geo["sx"] + geo["dx"] + box["x"] + box["width"] / 2
    # 默认点垂直中心
    target_cy_css = box["y"] + box["height"] / 2
    # 底部 fixed bar 避让:元素中心离视口底太近 → 改点 box 上 1/4
    try:
        inner_h = page.evaluate("() => window.innerHeight")
        center_to_bottom = inner_h - target_cy_css
        if center_to_bottom < _BOTTOM_BAR_RESERVE and box["height"] > _BOTTOM_BAR_RESERVE:
            target_cy_css = box["y"] + box["height"] / 4  # 上 1/4,避开底部 bar
    except Exception:
        pass
    cy = geo["sy"] + geo["dy"] + target_cy_css
    # 诊断:打印坐标数据(定位稳定后删除)
    try:
        screen_size = pyautogui.size()
        logger.info(
            f"[human_input][diag] box=({box['x']:.0f},{box['y']:.0f},{box['width']:.0f}x{box['height']:.0f}) "
            f"css中心y={box['y'] + box['height'] / 2:.0f} 实际点击css_y={target_cy_css:.0f} "
            f"inner_h={inner_h if 'inner_h' in dir() else '?'} "
            f"窗口geo sy={geo['sy']} dy={geo['dy']} 算出屏幕坐标=({cx:.0f},{cy:.0f}) "
            f"pyautogui.size={screen_size[0]}x{screen_size[1]}"
        )
    except Exception:
        pass
    return cx, cy


def _type_delay() -> int:
    """正态分布打字延迟 ms(均值 130,标准差 25,clamp 60-220)。"""
    val = random.gauss(130, 25)
    return max(60, min(220, round(val)))


def physical_click(
    page: Page,
    locator: Locator,
    *,
    click_count: int = 1,
    delay_ms: int = 80,
    verify: Any = None,
    retries: int = 2,
) -> None:
    """pyautogui 物理点击元素中心(含 2/3 击支持)。

    delay_ms:按下到释放的间隔(ms),模拟真实按压时间。
    click_count:连击次数(双击=2,三击=3)。
    点击前自动 scroll_into_view(确保元素在可视区域,否则 pyautogui 屏幕坐标会点飞)。

    verify:可选的验证回调,点击后调用,返回 True=成功 / False=没点中需重试。
            典型用法:verify=lambda: some_locator.is_visible() (点击后元素应消失)。
            人碰鼠标导致没点到时,verify 返回 False → 自动重试(最多 retries 次)。
    retries:verify 失败时的重试次数(默认 2 次)。
    """
    # 先把元素滚到可视区域(否则 bounding_box 可能返回视口外的坐标)
    try:
        locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    # 底部 fixed bar 避让在 _box_center_screen 内处理(滚动对 fixed bar 无效,
    # 改为把点击点上移到 box 上 1/4)。
    coord = _box_center_screen(page, locator)
    if coord is None:
        raise RuntimeError("physical_click: 无法获取元素坐标(bounding_box 为空)")
    cx, cy = coord

    def _do_click() -> None:
        # 每次重试都重新取坐标(页面可能因上次点击而变化)
        nonlocal cx, cy
        if retries > 0 or verify is not None:
            # 有 verify/retries 时重新定位(应对点击后页面变化)
            try:
                locator.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            new_coord = _box_center_screen(page, locator)
            if new_coord is not None:
                cx, cy = new_coord
        # 加微小随机抖动(人手不会精确点正中心)
        tx = cx + random.uniform(-3, 3)
        ty = cy + random.uniform(-2, 2)
        pyautogui.moveTo(tx, ty, duration=random.uniform(0.1, 0.25))
        for _ in range(click_count):
            pyautogui.mouseDown(button="left", _pause=False)
            time.sleep(delay_ms / 1000.0)
            pyautogui.mouseUp(button="left", _pause=False)
            if click_count > 1:
                time.sleep(random.uniform(0.05, 0.12))  # 连击间隔

    _do_click()
    if verify is None:
        return
    # 点击后验证,失败则重试
    time.sleep(0.5)
    for attempt in range(retries + 1):
        try:
            if verify():
                return
        except Exception:
            pass
        if attempt == retries:
            break
        logger.warning(f"[human_input] 点击验证失败,重试 {attempt + 1}/{retries}")
        time.sleep(random.uniform(0.8, 1.5))
        _do_click()
        time.sleep(0.5)
    raise RuntimeError(f"物理点击验证失败(重试 {retries} 次仍未通过),终止任务")


def physical_type(page: Page, text: str, *, force_paste: bool = False) -> None:
    """物理键盘输入中文/混合文本。

    pyautogui 的 press/typewrite 只支持 ASCII 键名,不支持中文 Unicode 字符。
    故对中文/混合文本用剪贴板粘贴(Cmd+V/Ctrl+V)——仍是 OS 级物理键盘事件
    (isTrusted=true),只是输入方式从逐字符改为一次性粘贴。
    纯英文/数字短文本保留逐字符 press(保留打字节奏特征)。

    force_paste:强制走剪贴板粘贴(哪怕纯 ASCII)。用于标点会被中文输入法
    拦截改写的场景(如定时日期的冒号 : 在中文输入法下变 :),粘贴不受
    输入法影响,保证字符精确。代价是丢失打字节奏特征,但对原生 input
    (日期框等)无妨。
    """
    import sys

    has_unicode = any(ord(c) > 127 for c in text)
    if not has_unicode and not force_paste:
        # 纯 ASCII:逐字符 press + 正态分布间隔
        chars_typed = 0
        for char in text:
            if char == " ":
                pyautogui.press("space", _pause=False)
            else:
                pyautogui.press(char, _pause=False)
            chars_typed += 1
            time.sleep(_type_delay() / 1000.0)
            if chars_typed % random.randint(8, 15) == 0:
                time.sleep(random.uniform(0.8, 2.0))
    else:
        # 含中文/Unicode 或 force_paste:剪贴板粘贴(OS 级物理快捷键,isTrusted=true)
        import pyperclip  # type: ignore[import-untyped]

        pyperclip.copy(text)
        time.sleep(0.1)
        mod = "command" if sys.platform == "darwin" else "ctrl"
        pyautogui.hotkey(mod, "v", _pause=False)
        time.sleep(random.uniform(0.3, 0.6))


def physical_press(key: str) -> None:
    """pyautogui 物理按键(Backspace/Enter/Space/Escape 等)。"""
    mapped = _KEY_MAP.get(key, key.lower())
    pyautogui.press(mapped, _pause=False)


def physical_select_all() -> None:
    """物理 Cmd+A(macOS)/ Ctrl+A(Windows) 全选当前输入框内容。"""
    import sys

    mod = "command" if sys.platform == "darwin" else "ctrl"
    pyautogui.hotkey(mod, "a", _pause=False)
    time.sleep(0.2)


def physical_scroll(page: Page, dy: int) -> None:
    """pyautogui 物理滚轮。dy>0 向下滚动(像素值近似)。"""
    # pyautogui scroll 的 amount 是" clicks",不是像素。正数向上,负数向下。
    # 经验值:100px ≈ 1 click。dy 是像素近似,转成 clicks。
    clicks = max(1, abs(dy) // 100)
    direction = -1 if dy > 0 else 1  # pyautogui 正数=向上,我们要 dy>0=向下
    pyautogui.scroll(direction * clicks, _pause=False)


def physical_upload(
    page: Page, click_locator: Locator, file_input_locator: Locator, file_path: str
) -> None:
    """混合上传:物理点击上传区 → ESC 关系统文件对话框 → set_input_files 注入文件。

    物理点击产生真实的 click 事件(isTrusted=true),让小红书知道"有人点了上传"。
    关掉弹出的系统对话框后,用 patchright set_input_files 注入文件路径 ——
    set_input_files 不产生 DOM 交互事件(isTrusted 管不到),避开了对话框操作风险。
    """
    # ① 物理点击上传区域(触发文件对话框)
    physical_click(page, click_locator)
    # ② 等系统文件对话框弹出后 ESC 关掉(对话框弹出有延迟,等 1 秒)
    time.sleep(1.0)
    physical_press("Escape")
    time.sleep(0.3)
    # ③ set_input_files 注入文件(此时对话框已关,input 仍可设值)
    file_input_locator.set_input_files(file_path)
    logger.info(f"[human_input] 物理点击+set_input_files 上传完成: {file_path}")


def _physical_bezier_move(page: Page, end_x: float, end_y: float) -> None:
    """pyautogui 贝塞尔曲线移动到屏幕坐标(内部用,physical_click 已内置移动)。

    保留供需要先移动再点击的场景。pyautogui moveTo 的 _getXY 函数底层用
    pytweening 的 easeOutQuint 做缓动,比手动插值更平滑。
    """
    pyautogui.moveTo(end_x, end_y, duration=random.uniform(0.15, 0.35))


def is_available() -> bool:
    """检查 pyautogui 是否可用(macOS 未开辅助功能权限时返回 False)。

    用于在 UI 提示用户开权限,或在不可用时退回 CDP 模式。
    """
    try:
        # 不实际移动,只检查 size() 能否调通(权限不足会抛异常或返回异常值)
        pyautogui.size()
        return True
    except Exception:
        return False


def bring_browser_to_front(page: Page) -> None:
    """把浏览器窗口拉到最前 + 最大化,确保 pyautogui 屏幕坐标操作不会点飞。

    macOS: --start-maximized 不生效,用 AppleScript 激活 Chrome + 绿色按钮最大化。
    Windows: --start-maximized 通常生效,额外用 pyautogui 发 Win+Up 兜底最大化。
    """
    import sys

    if sys.platform == "darwin":
        import subprocess

        try:
            # 激活 Chrome 窗口到最前
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                check=False,
                timeout=5,
                capture_output=True,
            )
            time.sleep(0.5)
            # 最大化:用 Cmd+Ctrl+F (macOS 全屏快捷键) 会进全屏模式不好控制。
            # 改用 AppleScript 设窗口 bounds 到屏幕尺寸(非全屏,保留菜单栏)。
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    """
                    tell application "Google Chrome"
                        set screenBounds to bounds of window 1
                        set winWidth to (do shell script "system_profiler SPDisplaysDataType | awk '/Resolution/{print $2; exit}'")
                        set winHeight to (do shell script "system_profiler SPDisplaysDataType | awk '/Resolution/{print $4; exit}'")
                        set bounds of window 1 to {0, 0, winWidth as integer, (winHeight as integer) - 30}
                    end tell
                    """,
                ],
                check=False,
                timeout=5,
                capture_output=True,
            )
        except Exception:
            pass  # best-effort
    else:
        # Windows:物理点击屏幕中心激活 Chrome 窗口(最简单最可靠)。
        # 之前 SetForegroundWindow / AttachThreadInput / minimize→maximize 都不可靠
        # (焦点窃取限制)。物理点击不受此限制——点击 Chrome 窗口区域必然激活它。
        # Chrome --window-position=0,0 + --window-size 几乎全屏,屏幕中心必然在窗口内。
        try:
            w, h = pyautogui.size()
            pyautogui.click(w // 2, h // 2, _pause=False)
            time.sleep(0.3)
            # Win+Up 最大化
            pyautogui.hotkey("win", "up", _pause=False)
            time.sleep(0.3)
        except Exception:
            pass
    logger.info("[human_input] 浏览器窗口已激活+最大化")


def block_user_input() -> None:
    """禁用真实键盘/鼠标输入(仅 Windows)。

    用 Win32 BlockInput:它只拦截真实用户硬件输入,不影响程序通过 SendInput
    注入的输入(pyautogui Windows 底层正是 SendInput),故自动化照常运行。
    非 Windows / 调用失败 → no-op(best-effort,不阻断主流程)。
    紧急解锁:Ctrl+Alt+Del 始终能强制解开 BlockInput(Windows 设计保证)。

    ⚠️ BlockInput 有前台/权限要求:调用线程需持有前台,否则静默返回 0 不生效。
    本函数检查返回值并告警(调用时前台是 Chrome 不是本进程,需 Windows 实测确认)。
    """
    import sys

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ok = ctypes.windll.user32.BlockInput(True)
        if ok:
            logger.info("[human_input] 已禁用真实键盘/鼠标输入")
        else:
            # 返回 0:前台/权限不满足,BlockInput 没生效。自动化仍跑,但人碰输入仍会干扰。
            logger.warning("[human_input] BlockInput 返回 0,未生效(前台/权限不满足)")
    except Exception as err:
        logger.warning(f"[human_input] 禁用输入失败(继续执行): {err}")


def unblock_user_input() -> None:
    """恢复真实键盘/鼠标输入(仅 Windows)。best-effort,失败仅告警。"""
    import sys

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.BlockInput(False)
        logger.info("[human_input] 已恢复键盘/鼠标输入")
    except Exception as err:
        logger.warning(f"[human_input] 恢复输入失败(可能仍锁着,Ctrl+Alt+Del 可强制解锁): {err}")
