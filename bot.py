"""
Paperfly Tracker — Telegram Bot
Ported directly from the working web version (main.py).
"""

import asyncio
import base64
import logging
import os
import re
from datetime import datetime, timedelta

import requests
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode, ChatAction

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN          = os.environ["TELEGRAM_BOT_TOKEN"]
PAPERFLY_USERNAME  = os.environ.get("PAPERFLY_USERNAME", "C172058")
PAPERFLY_PASSWORD  = os.environ.get("PAPERFLY_PASSWORD", "7420")
DEEPSEEK_API_KEY   = os.environ.get("DEEPSEEK_API_KEY", "")
PAPERFLY_LOGIN_URL = "https://go.paperfly.com.bd/identity/login"
PAPERFLY_ORDER_URL = "https://go.paperfly.com.bd/merchant/order-history"

log.info(f"=== BOT STARTING === TOKEN ends with: ...{BOT_TOKEN[-6:]}")
log.info(f"=== PAPERFLY USER: {PAPERFLY_USERNAME}")


def normalize_phone_local(raw: str) -> str:
    bangla = str.maketrans("\u09e6\u09e7\u09e8\u09e9\u09ea\u09eb\u09ec\u09ed\u09ee\u09ef", "0123456789")
    num = raw.translate(bangla)
    num = re.sub(r"[^\d]", "", num)
    if num.startswith("8801"):  num = "0" + num[3:]
    elif num.startswith("880"): num = "0" + num[3:]
    elif num.startswith("88"):  num = "0" + num[2:]
    return num

def normalize_phone(raw: str) -> str:
    if DEEPSEEK_API_KEY:
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={"model": "deepseek-chat", "messages": [{"role": "user", "content":
                    f"You are a Bangladesh phone number normalizer. Convert to exactly 01XXXXXXXXX format "
                    f"(11 digits, starts with 01). Strip +88/+880/880/88. Convert Bangla digits. "
                    f"Return ONLY the 11-digit number.\n\nInput: {raw}"}], "max_tokens": 20},
                timeout=10,
            )
            result = resp.json()["choices"][0]["message"]["content"].strip()
            if re.match(r"^01\d{9}$", result):
                return result
        except Exception as e:
            log.warning(f"DeepSeek failed: {e}")
    return normalize_phone_local(raw)


async def type_into_field(element, page, text):
    await element.click()
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(150)
    await page.keyboard.type(text, delay=80)
    await page.wait_for_timeout(400)


async def extract_timeline_from_page(pg, order_id, dbg):
    await pg.wait_for_load_state("networkidle")
    await pg.wait_for_timeout(3000)
    if "/identity/login" in pg.url:
        dbg(f"    Redirected to login — session lost")
        return ["Session expired"], None
    dbg(f"    Detail page loaded: {pg.url}")
    try:
        ss_bytes = await pg.screenshot(full_page=True)
        detail_ss_b64 = base64.b64encode(ss_bytes).decode()
    except Exception as e:
        dbg(f"    Screenshot failed: {e}")
        detail_ss_b64 = None
    result = await pg.evaluate("""() => {
        for (const d of document.querySelectorAll('div')) {
            if (d.childElementCount === 0 && d.innerText.trim() === 'Timeline') {
                let p = d.parentElement;
                for (let i = 0; i < 8; i++) {
                    if (p && p.children.length > 1) return { found: true, text: p.innerText };
                    if (p) p = p.parentElement;
                }
                return { found: true, text: d.parentElement ? d.parentElement.innerText : '' };
            }
        }
        return { found: false, text: document.body.innerText };
    }""")
    if result["found"]:
        lines = [l.strip() for l in result["text"].split("\n") if l.strip() and l.strip() != "Timeline"]
        dbg(f"    Timeline: {len(lines)} entries")
        return lines, detail_ss_b64
    return [], detail_ss_b64


async def do_search_and_wait(pg, phone, dbg):
    for _ in range(20):
        await pg.wait_for_timeout(1000)
        mui = await pg.query_selector_all("input.MuiInputBase-input")
        btns = await pg.query_selector_all("button.MuiButton-containedPrimary")
        if len(mui) >= 1 and len(btns) >= 1:
            break
    mui = await pg.query_selector_all("input.MuiInputBase-input")
    if mui:
        await type_into_field(mui[0], pg, phone)
    btns = await pg.query_selector_all("button.MuiButton-containedPrimary")
    for btn in btns:
        if "search" in (await btn.inner_text()).strip().lower():
            await btn.click()
            break
    else:
        if btns:
            await btns[0].click()
    await pg.wait_for_load_state("networkidle")
    await pg.wait_for_timeout(4000)


async def scrape_orders(phone: str, days_back: int, progress_cb=None) -> dict:
    debug_info = []
    orders = []
    screenshot_b64 = None
    normalized_phone = None
    error = None

    def dbg(msg: str):
        debug_info.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        log.info(msg)

    async def progress(msg: str):
        dbg(msg)
        if progress_cb:
            try:
                await progress_cb(msg)
            except Exception as e:
                log.warning(f"progress_cb error: {e}")

    try:
        await progress("🔍 Normalizing phone number...")
        normalized_phone = normalize_phone(phone)
        await progress(f"📱 Phone: {normalized_phone}")
        await progress("🚀 Launching browser...")

        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1366, "height": 900})
            page = await context.new_page()

            # LOGIN
            await progress("🔐 Logging into Paperfly...")
            await page.goto(PAPERFLY_LOGIN_URL)
            await page.wait_for_timeout(2000)
            await page.wait_for_timeout(3000)

            username_input = await page.wait_for_selector("input[type='text'].MuiInputBase-input", timeout=15000)
            await type_into_field(username_input, page, PAPERFLY_USERNAME)
            password_input = await page.wait_for_selector("input[type='password']", timeout=10000)
            await type_into_field(password_input, page, PAPERFLY_PASSWORD)
            await page.wait_for_timeout(500)

            login_btn = None
            for sel in ["button[type='submit']", "button.MuiButton-containedPrimary",
                        "button:has-text('Login')", "button:has-text('Sign In')"]:
                try:
                    login_btn = await page.wait_for_selector(sel, timeout=3000)
                    break
                except Exception:
                    pass
            if not login_btn:
                raise Exception("Could not find login button")

            await login_btn.click()
            await page.wait_for_timeout(2000)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)
            dbg(f"URL after login: {page.url}")

            if "/identity/login" in page.url:
                await password_input.press("Enter")
                await page.wait_for_timeout(3000)
                await page.wait_for_load_state("networkidle")
                if "/identity/login" in page.url:
                    raise Exception("Login failed. Check credentials.")

            dbg(f"Login OK. URL: {page.url}")
            await progress("✅ Logged in! Opening Order History...")

            # NAVIGATE TO ORDER HISTORY
            await page.wait_for_timeout(1500)
            oh_link = None
            for sel in ["text=Order History", ".MuiListItemText-primary:has-text('Order History')",
                        "a[href*='order-history']", "span:has-text('Order History')"]:
                try:
                    oh_link = await page.wait_for_selector(sel, timeout=4000)
                    break
                except Exception:
                    pass
            if oh_link:
                await oh_link.click()
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(2000)
            else:
                await page.goto(PAPERFLY_ORDER_URL)
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(2000)

            # WAIT FOR RENDER
            await progress("⏳ Waiting for page to render...")
            found = False
            for attempt in range(30):
                await page.wait_for_timeout(1000)
                mui = await page.query_selector_all("input.MuiInputBase-input")
                btns = await page.query_selector_all("button.MuiButton-containedPrimary")
                dbg(f"  Poll {attempt+1}/30: mui={len(mui)} btns={len(btns)}")
                if len(mui) >= 1 and len(btns) >= 1:
                    found = True
                    break
            if not found:
                raise Exception("Order history page did not render in time.")

            # CLOSE POPUP
            try:
                close_btn = await page.wait_for_selector(
                    "button[aria-label='close'], div[role='dialog'] button", timeout=3000)
                await close_btn.click()
                await progress("🔲 Closed popup dialog...")
                await page.wait_for_timeout(1000)
            except Exception:
                pass

            # FILL SEARCH FORM
            await progress(f"🔎 Searching orders for {normalized_phone}...")
            all_inputs = await page.query_selector_all("input.MuiInputBase-input")

            phone_input_el = None
            for inp in all_inputs:
                ph = (await inp.get_attribute("placeholder") or "").lower()
                if "phone" in ph or "order" in ph or "search" in ph:
                    phone_input_el = inp
                    break
            if not phone_input_el:
                phone_input_el = all_inputs[0]

            await type_into_field(phone_input_el, page, normalized_phone)

            start_date = (datetime.now() - timedelta(days=days_back)).strftime("%m/%d/%Y")
            dbg(f"Looking for date input, total inputs={len(all_inputs)}")
            for idx, inp in enumerate(all_inputs):
                ph = (await inp.get_attribute("placeholder") or "")
                itype = (await inp.get_attribute("type") or "")
                dbg(f"  input[{idx}] type={itype!r} placeholder={ph!r}")
            date_input_el = None
            for inp in all_inputs:
                ph = (await inp.get_attribute("placeholder") or "").lower()
                if "date" in ph or "from" in ph or "start" in ph or "mm/dd" in ph:
                    date_input_el = inp
                    dbg(f"Found date input by placeholder: {ph!r}")
                    break
            # Only use fallback if it's actually a date-type input
            if not date_input_el and len(all_inputs) > 1:
                itype = (await all_inputs[1].get_attribute("type") or "").lower()
                if itype in ("date", "text", ""):
                    date_input_el = all_inputs[1]
                    dbg(f"Using input[1] as date field (type={itype!r})")
                else:
                    dbg(f"Skipping input[1] as date — type={itype!r}")
            if date_input_el:
                dbg(f"Filling date: {start_date}")
                await type_into_field(date_input_el, page, start_date)
                dbg("Date filled")
            else:
                dbg("No date input found — skipping date filter")

            search_btns = await page.query_selector_all("button.MuiButton-containedPrimary")
            dbg(f"Found {len(search_btns)} search button candidates")
            search_btn = None
            for btn in search_btns:
                txt = (await btn.inner_text()).strip().lower()
                dbg(f"  Button text: {txt!r}")
                if "search" in txt or "find" in txt or "filter" in txt:
                    search_btn = btn
                    break
            if not search_btn:
                try:
                    search_btn = await page.wait_for_selector("button.MuiButton-sizeLarge", timeout=3000)
                    dbg("Using MuiButton-sizeLarge as search btn")
                except Exception:
                    search_btn = search_btns[-1] if search_btns else None
                    dbg("Using last primary button as search btn (fallback)")
            if not search_btn:
                raise Exception("Could not find search button")

            dbg("Clicking search button...")
            await search_btn.click()
            dbg("Search clicked — waiting for results...")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                dbg("networkidle timeout — continuing anyway")
            await page.wait_for_timeout(4000)
            dbg(f"URL after search: {page.url}")

            # EXTRACT TABLE
            await progress("📊 Extracting order data...")
            rows = await page.query_selector_all("table tbody tr")
            dbg(f"Found {len(rows)} rows")

            raw_orders = []
            for i, row in enumerate(rows):
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                order_link = await row.query_selector("a[href*='single-order-history']")
                order_id, order_href = "", ""
                if order_link:
                    order_id = (await order_link.inner_text()).strip()
                    order_href = await order_link.get_attribute("href") or ""
                if not order_id:
                    row_text = await row.inner_text()
                    m = re.search(r"Z-[0-9]{6}-[0-9]{6}-[A-Z0-9]+-[A-Z0-9]+", row_text)
                    if m:
                        order_id = m.group(0)
                first_cell_input = await cells[0].query_selector("input[type='checkbox']")
                offset = 1 if first_cell_input else 0
                async def cell_text(idx):
                    c = cells[idx] if idx < len(cells) else None
                    return (await c.inner_text()).strip() if c else ""
                order_date   = await cell_text(offset + 0)
                if not order_id: order_id = await cell_text(offset + 1)
                status       = await cell_text(offset + 2)
                collected    = await cell_text(offset + 3)
                merchant_oid = await cell_text(offset + 4)
                customer     = await cell_text(offset + 5)
                price        = await cell_text(offset + 6)
                dbg(f"  Row {i+1}: id={order_id!r} status={status!r}")
                if not order_id:
                    continue
                raw_orders.append({
                    "index": i, "order_date": order_date, "order_id": order_id,
                    "order_href": order_href, "status": status, "collected": collected,
                    "merchant_order_id": merchant_oid, "customer_details": customer,
                    "price": price, "timeline": [], "detail_ss_b64": None,
                })

            # FETCH TIMELINES (exact logic from main.py)
            if raw_orders:
                await progress(f"📍 Fetching timelines for {len(raw_orders)} order(s)...")

            for order in raw_orders:
                if not order["order_href"]:
                    dbg(f"  Skipping timeline for {order['order_id']} (no href)")
                    continue
                oid = order["order_id"]
                href = order["order_href"]
                dbg(f"  Getting timeline for {oid}")
                try:
                    # Go back to order history via sidebar (exactly as web version)
                    oh_link = await page.query_selector("text=Order History")
                    if oh_link:
                        await oh_link.click()
                    else:
                        await page.goto(PAPERFLY_ORDER_URL)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(2000)

                    await do_search_and_wait(page, normalized_phone, dbg)
                    dbg(f"    URL after search: {page.url}")

                    order_link_el = None
                    for sel in [f"a[href='{href}']", f"a[href*='{oid}']",
                                 "a[href*='single-order-history']"]:
                        order_link_el = await page.query_selector(sel)
                        if order_link_el:
                            dbg(f"    Found link via: {sel}")
                            break

                    if order_link_el:
                        await order_link_el.click()
                        order["timeline"], order["detail_ss_b64"] = \
                            await extract_timeline_from_page(page, oid, dbg)
                    else:
                        dbg(f"    Link not found, trying direct nav: {href}")
                        await page.goto(f"https://go.paperfly.com.bd{href}")
                        order["timeline"], order["detail_ss_b64"] = \
                            await extract_timeline_from_page(page, oid, dbg)
                except Exception as te:
                    import traceback
                    dbg(f"    Timeline error for {oid}: {te}")
                    dbg(traceback.format_exc()[:300])

            orders = raw_orders
            for o in reversed(orders):
                if o.get("detail_ss_b64"):
                    screenshot_b64 = o["detail_ss_b64"]
                    break

            await browser.close()
            dbg("Done.")

    except Exception as e:
        import traceback
        error = str(e)
        dbg(f"FATAL: {e}")
        dbg(traceback.format_exc()[:500])

    return {"phone": normalized_phone or phone, "orders": orders,
            "screenshot_b64": screenshot_b64, "debug_info": debug_info, "error": error}


STATUS_EMOJI = {
    "delivered": "✅", "return": "🔄", "cancel": "❌",
    "pending": "🕐", "transit": "🚚", "pickup": "📦", "point": "📍",
}

def status_emoji(s: str) -> str:
    s = (s or "").lower()
    for key, emoji in STATUS_EMOJI.items():
        if key in s: return emoji
    return "📋"


def format_result(result: dict) -> str:
    phone  = result.get("phone") or "unknown"
    orders = result.get("orders", [])
    error  = result.get("error")

    if error and not orders:
        return f"❌ *Error*\n`{error}`"
    if not orders:
        return f"📭 No orders found for `{phone}`"

    delivered = sum(1 for o in orders if "deliver" in (o.get("status") or "").lower())
    lines = [f"📞 *{phone}*", f"Total: {len(orders)} | ✅ Delivered: {delivered}", "─" * 30]

    for i, o in enumerate(orders, 1):
        oid      = o.get("order_id", "?")
        status   = o.get("status", "")
        date     = o.get("order_date", "")
        price    = o.get("price", "")
        customer = o.get("customer_details", "")
        timeline = o.get("timeline", [])
        lines.append(f"\n*{i}. {oid}*")
        lines.append(f"{status_emoji(status)} {status}")
        if date:     lines.append(f"🗓 {date}")
        if price:    lines.append(f"💰 {price}")
        if customer: lines.append(f"👤 {customer}")
        if timeline:
            lines.append("📍 *Timeline:*")
            for t in timeline[:8]:
                lines.append(f"• {t}")
    if error:
        lines.append(f"\n⚠️ _{error}_")
    return "\n".join(lines)


async def run_tracking(phone: str, days_back: int, chat_id: int, context):
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔍 Searching `{phone}`...\n⏳ Please wait 1–3 minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )
    progress_lines: list = []

    async def progress_cb(msg: str):
        progress_lines.append(msg)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg.message_id,
                text="\n".join(progress_lines[-8:]), parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    async def keep_typing():
        while True:
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        result = await scrape_orders(phone, days_back=days_back, progress_cb=progress_cb)
    finally:
        typing_task.cancel()

    msg_text = format_result(result)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=status_msg.message_id,
            text=msg_text[:4096], parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=msg_text[:4096], parse_mode=ParseMode.MARKDOWN)

    shots_sent = 0
    for o in result.get("orders", []):
        ss = o.get("detail_ss_b64")
        if ss:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id, photo=base64.b64decode(ss),
                    caption=f"🖼 {o['order_id']} — {o.get('status', '')}",
                )
                shots_sent += 1
            except Exception as e:
                log.warning(f"Screenshot send failed: {e}")
    if shots_sent == 0 and result.get("screenshot_b64"):
        try:
            await context.bot.send_photo(
                chat_id=chat_id, photo=base64.b64decode(result["screenshot_b64"]),
                caption="📋 Search results",
            )
        except Exception:
            pass

    phone_disp = result.get("phone") or phone
    total = len(result.get("orders", []))
    wa_text = f"Paperfly Orders — {phone_disp}\nTotal: {total}"
    wa_url = f"https://wa.me/?text={requests.utils.quote(wa_text)}"
    keyboard = [
        [InlineKeyboardButton("🟢 WhatsApp", url=wa_url)],
        [InlineKeyboardButton("🔄 Search Again", callback_data=f"retrack:{phone[:15]}:90")],
    ]
    await context.bot.send_message(
        chat_id=chat_id, text="Options:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    log.info("run_tracking complete!")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Paperfly Order Tracker*\n\nSend a customer phone number to search their orders.\nExample: `01712345678`",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use:*\n\n"
        "• Send any BD phone number\n"
        "• Bangla digits work too: ০১৭...\n"
        "• +88 prefix stripped automatically\n\n"
        "/start — Welcome\n/help — This help",
        parse_mode=ParseMode.MARKDOWN,
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    bangla = str.maketrans("\u09e6\u09e7\u09e8\u09e9\u09ea\u09eb\u09ec\u09ed\u09ee\u09ef", "0123456789")
    digits = re.sub(r"[^\d]", "", text.translate(bangla))
    if len(digits) < 9:
        await update.message.reply_text("📱 Please send a valid phone number.")
        return
    log.info(f"MESSAGE received: {text!r} from user {update.effective_user.id}")
    await run_tracking(text, 90, chat_id, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data.startswith("retrack:"):
        parts = data.split(":")
        phone = parts[1] if len(parts) > 1 else ""
        days  = int(parts[2]) if len(parts) > 2 else 90
        if phone:
            chat_id = query.message.chat.id
            log.info(f"Re-tracking {phone} for {days} days")
            await run_tracking(phone, days, chat_id, context)

def main():
    log.info("Building application...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    log.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
