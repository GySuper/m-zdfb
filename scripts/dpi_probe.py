"""Windows 高 DPI 物理点击坐标偏移诊断脚本。

根因调查:小红书发布在 Windows 上"标题正常、话题点飞"的疑似根因是
human_input._box_center_screen 的坐标换算没考虑 DPI 缩放。本脚本直接
在真实环境里量化偏移,拿到铁证后再改代码。

用法(Windows / macOS 上都跑一次对比):
    python scripts/dpi_probe.py

脚本会:
1. 用 patchright 开一个本地测试页,页面正中放一个大按钮(命中区覆盖大,
   便于看出偏移方向和量级),按钮上方/下方还各放一个,模拟"标题区/正文区"
   的不同屏幕高度。
2. 对每个按钮:
   - patchright 算出 _box_center_screen 同款"逻辑屏幕坐标"。
   - 用 pyautogui 在该坐标点击(isTrusted)。
   - JS 侧记录点击真实落点的 clientX/clientY,与按钮中心对比,算偏移。
3. 打印 devicePixelRatio / pyautogui.size() / innerWidth 等环境量。
4. 最后给结论:偏移是否与 dpr 成正比、是否随屏幕高度线性放大。

判定标准:
- 若顶部按钮命中、中下部按钮偏移随 y 增大而增大,且偏移量 ≈ (y * (dpr-1))
  → 确认 DPI 换算缺失,修 _box_center_screen。
- 若所有按钮都命中(偏移≈0)→ DPI 不是根因,需另查(焦点/时序)。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pyautogui
from patchright.sync_api import sync_playwright

pyautogui.FAILSAFE = False


# 与 human_input._box_center_screen 完全相同的换算
def box_center_screen(page, box):
    geo = page.evaluate(
        """() => ({
            sx: window.screenX, sy: window.screenY,
            dx: (window.outerWidth - window.innerWidth) / 2,
            dy: window.outerHeight - window.innerHeight,
            dpr: window.devicePixelRatio,
            innerW: window.innerWidth, innerH: window.innerHeight,
            outerW: window.outerWidth, outerH: window.outerHeight,
        })"""
    )
    cx = geo["sx"] + geo["dx"] + box["x"] + box["width"] / 2
    cy = geo["sy"] + geo["dy"] + box["y"] + box["height"] / 2
    return cx, cy, geo


HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  body { margin:0; font-family:sans-serif; }
  .spacer { height: 35vh; }
  .pad { height: 8vh; }
  button.t {
    display:block; margin:0 auto; padding:40px 80px; font-size:20px;
    background:#e33; color:#fff; border:0; border-radius:8px;
  }
  #log { position:fixed; top:0; left:0; background:#fff; font-size:14px;
         padding:8px; max-width:50vw; word-break:break-all; }
</style></head><body>
<div id="log">点中按钮会在这里显示落点坐标</div>
<div class="pad"></div>
<button class="t" id="top"    data-tag="顶部(模拟标题区)">TOP 按钮</button>
<div class="spacer"></div>
<button class="t" id="middle" data-tag="中部(模拟描述区)">MIDDLE 按钮</button>
<div class="spacer"></div>
<button class="t" id="bottom" data-tag="下部(模拟话题/定时区)">BOTTOM 按钮</button>
<script>
  const log = document.getElementById('log');
  document.querySelectorAll('button.t').forEach(b => {
    b.addEventListener('click', (e) => {
      const r = b.getBoundingClientRect();
      const cx = r.left + r.width/2, cy = r.top + r.height/2;
      const dx = e.clientX - cx, dy = e.clientY - cy;
      log.textContent = b.dataset.tag +
        ' | 按钮中心 client(' + Math.round(cx) + ',' + Math.round(cy) +
        ') 实际落点 client(' + Math.round(e.clientX) + ',' + Math.round(e.clientY) +
        ') 偏移 dx=' + Math.round(dx) + ' dy=' + Math.round(dy);
      b.dataset.hit = JSON.stringify({dx:Math.round(dx), dy:Math.round(dy)});
      b.style.background = '#3a3';
    });
  });
</script></body></html>"""


def main():
    tmp = Path(tempfile.mkdtemp()) / "dpi_probe.html"
    tmp.write_text(HTML, encoding="utf-8")
    url = tmp.as_uri()

    print(f"[probe] 测试页: {tmp}")
    print(f"[probe] Python: {sys.version.split()[0]}  平台: {sys.platform}")
    print(f"[probe] pyautogui.size() = {pyautogui.size()}  (可能受 DPI awareness 影响)")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--start-maximized", "--window-position=0,0"],
        )
        page = browser.new_page(viewport=None)  # no_viewport: 用窗口实际尺寸
        page.goto(url, wait_until="load")

        # 先拿全局几何
        g0 = page.evaluate(
            "() => ({dpr:devicePixelRatio, iw:innerWidth, ih:innerHeight, ow:outerWidth, oh:outerHeight, sx:screenX, sy:screenY})"
        )
        print(f"[probe] devicePixelRatio = {g0['dpr']}")
        print(f"[probe] inner  = {g0['iw']} x {g0['ih']}  (CSS 逻辑像素)")
        print(f"[probe] outer  = {g0['ow']} x {g0['oh']}")
        print(f"[probe] screenX/Y = ({g0['sx']}, {g0['sy']})")
        print(f"[probe] pyautogui.size() = {pyautogui.size()}  (pyautogui 坐标系)")
        print()

        results = []
        for tag in ["top", "middle", "bottom"]:
            loc = page.locator(f"#{tag}")
            loc.scroll_into_view_if_needed(timeout=3000)
            # 滚动后小停顿,等布局稳定
            page.wait_for_timeout(800)
            box = loc.bounding_box()
            if not box:
                print(f"[probe] !! #{tag} 拿不到 bounding_box")
                continue
            cx, cy, geo = box_center_screen(page, box)
            print(
                f"[probe] #{tag}: box=({box['x']:.0f},{box['y']:.0f},{box['width']:.0f}x{box['height']:.0f})"
            )
            print(
                f"[probe]   _box_center_screen 算出屏幕坐标 = ({cx:.1f}, {cy:.1f})  [dpr={geo['dpr']}]"
            )

            # 清掉上次命中标记
            page.evaluate("(t) => document.getElementById(t).removeAttribute('data-hit')", tag)
            page.evaluate("(t) => document.getElementById(t).style.background = '#e33'", tag)

            # 物理点击(带轻微抖动去掉,直接点中心,便于精确判定)
            pyautogui.moveTo(cx, cy, duration=0.2)
            page.wait_for_timeout(200)
            pyautogui.mouseDown(button="left")
            time.sleep(0.05)
            pyautogui.mouseUp(button="left")
            page.wait_for_timeout(600)

            hit = loc.get_attribute("data-hit")
            if hit:
                import json

                h = json.loads(hit)
                results.append((tag, h["dx"], h["dy"]))
                print(f"[probe]   ✅ 命中! 偏移 dx={h['dx']} dy={h['dy']} (client 坐标系)")
            else:
                results.append((tag, None, None))
                print("[probe]   ❌ 没点中(按钮没变绿)")
            print()

        # 结论
        print("=" * 60)
        print("[结论]")
        dpr = g0["dpr"]
        print(f"  devicePixelRatio = {dpr}")
        for tag, dx, dy in results:
            if dx is None:
                print(f"  {tag}: ❌ 没命中")
            else:
                verdict = "命中" if abs(dx) < 15 and abs(dy) < 15 else f"偏移!dx={dx} dy={dy}"
                print(f"  {tag}: {verdict}")
        print()
        if any(dy is not None and abs(dy) >= 15 for _, _, dy in results):
            print("  ⚠️ 存在 y 方向明显偏移 → 高度越大偏移越大,符合 DPI 换算缺失特征")
            print("  → 修 human_input._box_center_screen:坐标乘 devicePixelRatio")
        elif all(dy is not None and abs(dy) < 15 for _, _, dy in results):
            print("  ✅ 所有按钮都命中 → DPI 不是根因,需另查(焦点/时序/选择器)")

        print()
        if "--wait" in sys.argv:
            input("[probe] 看完后按回车关闭浏览器...")
        else:
            print("[probe] 3 秒后自动关闭浏览器(--wait 可改为手动)")
            page.wait_for_timeout(3000)
        browser.close()


if __name__ == "__main__":
    main()
