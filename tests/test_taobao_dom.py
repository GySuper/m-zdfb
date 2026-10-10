"""Local browser regressions for the live-observed Taobao DOM; no account or publish request."""

from __future__ import annotations

import html
from collections.abc import Iterator
from datetime import datetime

import pytest
from patchright.sync_api import Page, sync_playwright

from wxsp.errors import ElementNotFound, ProductNotFound, ProductSelectionFailed, UploadFailed
from wxsp.platforms import taobao_guanghe as tb
from wxsp.platforms import taobao_selectors as sel

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def browser_page() -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(2_000)
        yield page
        browser.close()


def set_frame(page: Page, markup: str) -> None:
    page.set_content(f'<iframe title="发布器" srcdoc="{html.escape(markup, quote=True)}"></iframe>')
    page.frame_locator(sel.IFRAME_SELECTOR).locator("body").wait_for()


def test_schedule_waits_for_delayed_button_and_replaced_frame(browser_page: Page) -> None:
    markup = """
      <div><label><input type="radio" checked></label><span>定时发布</span></div>
      <input role="combobox" placeholder="请选择日期和时间" value="2026/09/16 12:30">
      <button disabled>定时发布</button>
      <script>setTimeout(() => document.querySelector('button').disabled = false, 5200)</script>
    """
    set_frame(browser_page, "<p>loading</p>")
    browser_page.evaluate(
        "markup => setTimeout(() => { const old = document.querySelector('iframe'); "
        "const fresh = document.createElement('iframe'); fresh.title = '发布器'; "
        "fresh.srcdoc = markup; old.replaceWith(fresh); }, 100)",
        markup,
    )
    tb._verify_schedule(browser_page, datetime(2026, 9, 16, 12, 30))


def test_wrong_date_blocks_submit_even_with_correct_button(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_frame(
        browser_page,
        """
      <div><label><input type="radio" checked></label><span>定时发布</span></div>
      <input role="combobox" placeholder="请选择日期和时间" value="2026/09/16 00:00">
      <button onclick="this.dataset.clicked = 'true'">定时发布</button>
    """,
    )
    monkeypatch.setattr(tb, "_CONTROL_TIMEOUT_MS", 100)
    with pytest.raises(ElementNotFound, match="目标时间"):
        tb._verify_schedule(browser_page, datetime(2026, 9, 16, 12, 30))
    assert (
        browser_page.frame_locator(sel.IFRAME_SELECTOR)
        .locator("button")
        .get_attribute("data-clicked")
        is None
    )


def test_schedule_check_is_idempotent_and_verifies_full_date(browser_page: Page) -> None:
    set_frame(
        browser_page,
        """
      <div><label><input id="mode" type="radio" checked
        onclick="this.checked = false"></label><span>定时发布</span></div>
      <input role="combobox" placeholder="请选择日期和时间" value="2026/09/15 01:00"
        readonly onclick="document.querySelector('#shell').classList.add('opened')">
      <div id="shell" class="next-overlay-wrapper" style="height:0">
        <div class="next-overlay-inner next-date-picker-body" style="position:absolute">
          <input id="date" placeholder="YYYY/MM/DD" value="2026/09/15"
            onkeydown="if(event.key === 'Enter') setTimeout(() => {
              document.querySelector('[role=combobox]').value = this.value + ' 00:00';
              document.querySelector('#time').value = '00:00'; }, 100)">
          <input id="time" placeholder="HH:mm" value="01:00">
          <button onclick="document.querySelector('[role=combobox]').value =
            document.querySelector('#date').value + ' ' +
            document.querySelector('#time').value;
            document.querySelector('#shell').classList.remove('opened')">确定</button>
        </div>
      </div>
      <button>定时发布</button>
    """,
    )
    target = datetime(2026, 9, 16, 12, 30)
    tb._set_schedule(browser_page, target)
    tb._set_schedule(browser_page, target)
    tb._verify_schedule(browser_page, target)


def test_schedule_waits_for_picker_already_expanded_during_mount(browser_page: Page) -> None:
    set_frame(
        browser_page,
        """
      <div><label><input type="radio" checked></label><span>定时发布</span></div>
      <input role="combobox" aria-expanded="true" placeholder="请选择日期和时间"
        value="2026/09/16 12:30" onclick="this.dataset.reclicked = 'true'">
      <div id="picker-mount"></div>
      <button>定时发布</button>
      <script>
        setTimeout(() => {
          document.querySelector('#picker-mount').innerHTML = `
            <div class="next-overlay-wrapper opened" style="height:0">
              <div class="next-overlay-inner next-date-picker-body" style="position:absolute">
                <input placeholder="YYYY/MM/DD" value="2026/09/16">
                <input placeholder="HH:mm" value="12:30">
                <button onclick="this.closest('.next-overlay-wrapper').classList.remove('opened')">确定</button>
              </div>
            </div>`;
        }, 100);
      </script>
    """,
    )
    tb._set_schedule(browser_page, datetime(2026, 9, 16, 12, 30))
    combo = browser_page.frame_locator(sel.IFRAME_SELECTOR).locator(sel.SCHEDULE_COMBOBOX)
    assert combo.get_attribute("data-reclicked") is None


def test_product_search_allows_empty_initial_list_and_keeps_checked_item(
    browser_page: Page,
) -> None:
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" class="next-search-icon"
          style="display:inline-block;width:20px;height:20px"
          onclick="setTimeout(() => document.querySelector('#result').style.display='block', 200)"></i>
        <div id="result" style="display:none">
          <a href="https://item.taobao.com/item.htm?id=1234">wrong prefix</a>
          <div><a href="https://item.taobao.com/item.htm?id=123">product</a>
            <input class="next-checkbox-input" type="checkbox" checked></div>
        </div>
        <button onclick="this.parentElement.style.display='none'">确定</button>
      </div>
    """,
    )
    tb._add_products(browser_page, ["123", "123"])
    checkbox = browser_page.frame_locator(sel.IFRAME_SELECTOR).locator('input[type="checkbox"]')
    assert checkbox.is_checked()


def test_product_search_waits_through_transient_empty_state(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tb, "_CONTROL_TIMEOUT_MS", 300)
    monkeypatch.setattr(tb, "_PRODUCT_SEARCH_TIMEOUT_MS", 2_000)
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" class="next-search-icon"
          style="display:inline-block;width:20px;height:20px"
          onclick="document.querySelector('#result').innerHTML = '<p>没有找到商品</p>';
            setTimeout(() => document.querySelector('#result').innerHTML = `
              <div><a href='https://item.taobao.com/item.htm?id=123'>product</a>
                <input class='next-checkbox-input' type='checkbox'></div>`, 800)"></i>
        <div id="result"></div>
        <button onclick="this.parentElement.style.display='none'">确定</button>
      </div>
    """,
    )
    tb._add_products(browser_page, ["123"])
    iframe = browser_page.frame_locator(sel.IFRAME_SELECTOR)
    assert iframe.locator('input[type="checkbox"]').is_checked()
    assert not iframe.locator(".next-dialog").is_visible()


@pytest.mark.parametrize(
    "result",
    [
        "<p>没有找到商品</p>",
        """<div><a href="https://item.taobao.com/item.htm?id=1234">wrong prefix</a>
          <input class="next-checkbox-input" type="checkbox"></div>""",
        """<div style="display:none"><a href="https://item.taobao.com/item.htm?id=123">hidden</a>
          <input class="next-checkbox-input" type="checkbox"></div>""",
    ],
    ids=["empty", "wrong-product", "hidden-product"],
)
def test_product_search_timeout_does_not_confirm_selection(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    monkeypatch.setattr(tb, "_CONTROL_TIMEOUT_MS", 300)
    monkeypatch.setattr(tb, "_PRODUCT_SEARCH_TIMEOUT_MS", 400)
    set_frame(
        browser_page,
        f"""
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" class="next-search-icon"
          style="display:inline-block;width:20px;height:20px"></i>
        {result}
        <button onclick="this.dataset.confirmed = 'true'">确定</button>
      </div>
    """,
    )
    with pytest.raises(ProductNotFound, match="商品ID '123' 搜索无结果"):
        tb._add_products(browser_page, ["123"])
    iframe = browser_page.frame_locator(sel.IFRAME_SELECTOR)
    assert iframe.locator("button").get_attribute("data-confirmed") is None
    assert all(
        not checkbox.is_checked() for checkbox in iframe.locator('input[type="checkbox"]').all()
    )


def test_product_checkbox_replacement_after_click_is_treated_as_selected(
    browser_page: Page,
) -> None:
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" class="next-search-icon"
          style="display:inline-block;width:20px;height:20px"></i>
        <div><a href="https://item.taobao.com/item.htm?id=1054399102483">product</a>
          <input class="next-checkbox-input" type="checkbox" aria-checked="false"
            onclick="event.preventDefault(); setTimeout(() => {
              const replacement = this.cloneNode(); replacement.checked = true;
              replacement.setAttribute('aria-checked', 'true'); this.replaceWith(replacement);
            }, 50)">
        </div>
        <button onclick="this.parentElement.style.display='none'">确定</button>
      </div>
    """,
    )
    tb._add_products(browser_page, ["1054399102483"])
    checkbox = browser_page.frame_locator(sel.IFRAME_SELECTOR).locator('input[type="checkbox"]')
    assert checkbox.is_checked()


def test_product_checkbox_waits_through_detached_input(browser_page: Page) -> None:
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" style="display:inline-block;width:20px;height:20px"></i>
        <div><a href="https://item.taobao.com/item.htm?id=980563551052">product</a>
          <input class="next-checkbox-input" type="checkbox"
            onclick="event.preventDefault(); const card = this.parentElement;
              const replacement = this.cloneNode(); replacement.checked = true;
              replacement.setAttribute('aria-checked', 'true'); this.remove();
              setTimeout(() => card.appendChild(replacement), 800)">
        </div>
        <button onclick="this.parentElement.style.display='none'">确定</button>
      </div>
    """,
    )
    browser_page.set_default_timeout(100)
    try:
        tb._add_products(browser_page, ["980563551052"])
    finally:
        browser_page.set_default_timeout(2_000)
    assert (
        browser_page.frame_locator(sel.IFRAME_SELECTOR)
        .locator('input[type="checkbox"]')
        .is_checked()
    )


def test_product_checkbox_retries_if_first_click_does_not_select(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tb, "_PRODUCT_SELECT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(tb, "_ELEMENT_RETRY_DELAY_SECONDS", 0.02)
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" style="display:inline-block;width:20px;height:20px"></i>
        <div><a href="https://item.taobao.com/item.htm?id=980563551052">product</a>
          <input class="next-checkbox-input" type="checkbox" data-clicks="0"
            onclick="this.dataset.clicks = Number(this.dataset.clicks) + 1;
              if(this.dataset.clicks === '1') event.preventDefault()">
        </div>
        <button onclick="this.parentElement.style.display='none'">确定</button>
      </div>
    """,
    )
    tb._add_products(browser_page, ["980563551052"])
    checkbox = browser_page.frame_locator(sel.IFRAME_SELECTOR).locator('input[type="checkbox"]')
    assert checkbox.is_checked()
    assert checkbox.get_attribute("data-clicks") == "2"


def test_product_checkbox_does_not_click_selected_replacement(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tb, "_ELEMENT_RETRY_DELAY_SECONDS", 0.02)
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" style="display:inline-block;width:20px;height:20px"></i>
        <div onmouseenter="if(!this.dataset.replaced) {
          this.dataset.replaced = 'true'; const checkbox = this.querySelector('input');
          const replacement = checkbox.cloneNode(); replacement.checked = true; checkbox.remove();
          setTimeout(() => this.appendChild(replacement), 800) }">
          <a href="https://item.taobao.com/item.htm?id=980563551052">product</a>
          <input class="next-checkbox-input" type="checkbox" checked data-clicks="0"
            onclick="this.dataset.clicks = Number(this.dataset.clicks) + 1">
        </div>
        <button onclick="this.parentElement.style.display='none'">确定</button>
      </div>
    """,
    )
    tb._add_products(browser_page, ["980563551052"])
    checkbox = browser_page.frame_locator(sel.IFRAME_SELECTOR).locator('input[type="checkbox"]')
    assert checkbox.is_checked()
    assert checkbox.get_attribute("data-clicks") == "0"


def test_product_checkbox_stops_after_bounded_retries(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tb, "_PRODUCT_SELECT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(tb, "_ELEMENT_RETRY_DELAY_SECONDS", 0.02)
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">添加商品</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入商品关键词或商品ID">
        <i role="button" aria-label="搜索" style="display:inline-block;width:20px;height:20px"></i>
        <div><a href="https://item.taobao.com/item.htm?id=980563551052">product</a>
          <input class="next-checkbox-input" type="checkbox" data-clicks="0"
            onclick="event.preventDefault(); this.dataset.clicks = Number(this.dataset.clicks) + 1">
        </div>
        <button onclick="this.dataset.confirmed = 'true'">确定</button>
      </div>
    """,
    )
    with pytest.raises(ProductSelectionFailed, match="勾选未生效"):
        tb._add_products(browser_page, ["980563551052"])
    iframe = browser_page.frame_locator(sel.IFRAME_SELECTOR)
    checkbox = iframe.locator('input[type="checkbox"]')
    assert not checkbox.is_checked()
    assert checkbox.get_attribute("data-clicks") == "3"
    assert iframe.locator("button").get_attribute("data-confirmed") is None


def test_topic_does_not_toggle_already_selected_card(browser_page: Page) -> None:
    set_frame(
        browser_page,
        """
      <div onclick="document.querySelector('.next-dialog').style.display='block'">点击添加话题</div>
      <div class="next-dialog" style="display:none">
        <input placeholder="输入关键词搜索"><button>搜索</button>
        <div data-autolog-container="topic-item-card" class="topic-card-select-active--123"
          onclick="this.className = ''">
          <div class="example--topic-title-font--123">精确话题</div>
        </div>
        <button onclick="this.parentElement.style.display='none'">确认提交</button>
      </div>
    """,
    )
    tb._add_topic(browser_page, "精确话题")
    assert (
        browser_page.frame_locator(sel.IFRAME_SELECTOR)
        .locator(sel.TOPIC_CARD)
        .get_attribute("class")
        == "topic-card-select-active--123"
    )


def test_saved_draft_is_not_publish_success(browser_page: Page) -> None:
    set_frame(browser_page, "<p>已保存</p>")
    with pytest.raises(ElementNotFound, match="成功判定超时"):
        tb._wait_for_success_indicator(browser_page, timeout=0.1)


def test_cover_waits_until_main_image_is_loaded(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tb, "_COVER_WAIT_TIMEOUT_SECONDS", 2)
    set_frame(
        browser_page,
        """
          <img class="publish-content__cover-v2--coverPic--test" src="">
          <script>
            setTimeout(() => document.querySelector('img').src =
              'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=', 200);
          </script>
        """,
    )
    tb._wait_cover_generated(browser_page)


def test_cover_timeout_blocks_follow_up_actions(
    browser_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tb, "_COVER_WAIT_TIMEOUT_SECONDS", 0.1)
    set_frame(
        browser_page, '<img class="publish-content__cover-v2--coverPic--test" src="/missing.jpg">'
    )
    with pytest.raises(UploadFailed, match=r"0\.1s"):
        tb._wait_cover_generated(browser_page)
