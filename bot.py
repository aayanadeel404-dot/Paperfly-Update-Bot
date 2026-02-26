"""
Paperfly Tracker — Telegram Bot
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
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.DEBUG,  # DEBUG level to catch everything
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN         = os.environ["TELEGRAM_BOT_TOKEN"]
PAPERFLY_USERNAME = os.environ.get("PAPERFLY_USERNAME", "C172058")
PAPERFLY_PASSWORD = os.environ.get("PAPERFLY_PASSWORD", "7420")
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
PAPERFLY_LOGIN_URL = "https://go.paperfly.com.bd/identity/login"
PAPERFLY_ORDER_URL = "https://go.paperfly.com.bd/merchant/order-history"

log.info(f"=== BOT STARTING === TOKEN ends with: ...{BOT_TOKEN[-6:]}")
log.info(f"=== PAPERFLY USER: {PAPERFLY_USERNAME}")

# ── Phone normalization ─────────────────────────────────────────────────────────
def normalize_phone_local(raw: str) -> str:
    bangla = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
    num = raw.translate(bangla)
    num = re.sub(r"[^\d]", "", num)
    if num.startswith("8801"):   num = "0" + num[3:]
    elif num.startswith("880"):  num = "0" + num[3:]
    elif num.startswith("88"):   num = "0" + num[2:]
    return num

def normalize_phone(raw: str) -> str:
    if DEEPSEEK_API_KEY:
        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content":
                        f"Normalize to 01XXXXXXXXX (11 digits). Strip +88/880/88. Convert Bangla digits. Return ONLY the number.\nInput: {raw}"}],
                    "max_tokens": 20,
                },
                timeout=10,
            )
            result = resp.json()["choices"][0]["message"]["content"].strip()
            if re.match(r"^01\d{9}$", result):
                return result
        except Exception as e:
            log.warning(f"DeepSeek failed: {e}")
    return normalize_phone_local(raw)

# ── Playwright scraper ──────────────────────────────────────────────────────────
async def type_field(el, page, text: str):
    await el.click()
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(150)
    await page.keyboard.type(text, delay=70)
    await page.wait_for_timeout(350)

async def scrape_orders(phone: str, days_back: int, progress_cb=None) -> dict:
    async def progress(msg: str):
        log.info(f"PROGRESS: {msg}")
        if progress_cb:
            try:
                await progress_cb(msg)
            except Exception as e:
                log.warning(f"progress_cb error: {e}")

    orders = []
    screenshot_b64 = None

    try:
        await progress("🔍 Normalizing phone number...")
        normalized = normalize_phone(phone)
        await progress(f"📱 Phone: {normalized}")

        await progress("🚀 Launching browser...")
        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            ctx = await browser.new_context(viewport={"width": 1366, "height": 900})
            page = await ctx.new_page()

            await progress("🔐 Logging into Paperfly...")
            await page.goto(PAPERFLY_LOGIN_URL)
            await page.wait_for_timeout(5000)

            u_inp = await page.wait_for_selector("input[type='text'].MuiInputBase-input", timeout=15000)
            await type_field(u_inp, page, PAPERFLY_USERNAME)

            p_inp = await page.wait_for_selector("input[type='password']", timeout=10000)
            await type_field(p_inp, page, PAPERFLY_PASSWORD)
            await page.wait_for_timeout(400)

            login_btn = None
            for sel in ["button[type='submit']", "button.MuiButton-containedPrimary",
                        "button:has-text('Login')", "button:has-text('Sign In')"]:
                try:
                    login_btn = await page.wait_for_selector(sel, timeout=3000)
                    break
                except Exception:
                    pass

            if not login_btn:
                raise Exception("Login button not found")

            await login_btn.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

            if "/identity/login" in page.url:
                await p_inp.press("Enter")
                await page.wait_for_timeout(3000)
                await page.wait_for_load_state("networkidle")
                if "/identity/login" in page.url:
                    raise Exception("Login failed — check credentials")

            await progress("✅ Logged in! Opening Order History...")

            oh_link = None
            for sel in ["text=Order History", "a[href*='order-history']", "span:has-text('Order History')"]:
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

            await progress("⏳ Waiting for page to render...")
            found = False
            for _ in range(30):
                await page.wait_for_timeout(1000)
                mui = await page.query_selector_all("input.MuiInputBase-input")
                btns = await page.query_selector_all("button.MuiButton-containedPrimary")
                if len(mui) >= 1 and len(btns) >= 1:
                    found = True
                    break

            if not found:
                raise Exception("Order history page did not render in time")

            all_mui = await page.query_selector_all("input.MuiInputBase-input")

            await progress(f"🔎 Searching orders for {normalized}...")
            phone_inp = None
            for inp in all_mui:
                ph = (await inp.get_attribute("placeholder") or "").lower()
                if any(k in ph for k in ["phone", "order", "search"]):
                    phone_inp = inp
                    break
            if not phone_inp:
                phone_inp = all_mui[0]

            await type_field(phone_inp, page, normalized)

            start_date = (datetime.now() - timedelta(days=days_back)).strftime("%m/%d/%Y")
            date_inp = None
            for inp in all_mui:
                ph = (await inp.get_attribute("placeholder") or "").lower()
                if any(k in ph for k in ["date", "from", "start", "mm/dd"]):
                    date_inp = inp
                    break
            if not date_inp and len(all_mui) > 1:
                date_inp = all_mui[1]
            if date_inp:
                await type_field(date_inp, page, start_date)

            search_btns = await page.query_selector_all("button.MuiButton-containedPrimary")
            search_btn = None
            for btn in search_btns:
                txt = (await btn.inner_text()).strip().lower()
                if any(k in txt for k in ["search", "find", "filter"]):
                    search_btn = btn
                    break
            if not search_btn:
                search_btn = search_btns[-1] if search_btns else None

            if not search_btn:
                raise Exception("Search button not found")

            # Dismiss any popup/dialog blocking the page
            try:
                dialog = await page.query_selector("div.MuiDialog-root")
                if dialog:
                    await progress("🔲 Closing popup dialog...")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(1500)
                    backdrop = await page.query_selector("div.MuiBackdrop-root")
                    if backdrop:
                        await backdrop.click(force=True)
                        await page.wait_for_timeout(1000)
            except Exception as de:
                log.warning(f"Dialog dismiss: {de}")

            # Use JS click to bypass overlay issues
            try:
                await page.evaluate("(btn) => btn.click()", search_btn)
                await page.wait_for_timeout(1000)
            except Exception:
                await search_btn.click(timeout=10000)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(4000)

            await progress("📊 Extracting order data...")
            rows = await page.query_selector_all("table tbody tr")

            for i, row in enumerate(rows):
                cells = await row.query_selector_all("td")
                if len(cells) < 2:
                    continue
                order_link = await row.query_selector("a[href*='/merchant/single-order-history/']")
                order_id, order_href = "", ""
                if order_link:
                    order_id = (await order_link.inner_text()).strip()
                    order_href = await order_link.get_attribute("href") or ""
                has_cb = await cells[0].query_selector("input[type='checkbox']")
                offset = 1 if has_cb else 0
                async def ct(idx):
                    c = cells[idx] if idx < len(cells) else None
                    return (await c.inner_text()).strip() if c else ""
                order_date = await ct(offset)
                if not order_id: order_id = await ct(offset + 1)
                status     = await ct(offset + 2)
                collected  = await ct(offset + 3)
                merch_oid  = await ct(offset + 4)
                customer   = await ct(offset + 5)
                price      = await ct(offset + 6)
                if not order_id:
                    continue
                orders.append({
                    "order_id": order_id, "order_date": order_date,
                    "order_href": order_href, "status": status,
                    "collected": collected, "merchant_order_id": merch_oid,
                    "customer_details": customer, "price": price,
                    "timeline": [], "screenshot_b64": None,
                })

            if orders:
                await progress(f"📍 Fetching timelines for {len(orders)} order(s)...")

            async def do_search_again():
                for _ in range(20):
                    await page.wait_for_timeout(1000)
                    m = await page.query_selector_all("input.MuiInputBase-input")
                    b = await page.query_selector_all("button.MuiButton-containedPrimary")
                    if m and b:
                        break
                m = await page.query_selector_all("input.MuiInputBase-input")
                if m:
                    await type_field(m[0], page, normalized)
                b = await page.query_selector_all("button.MuiButton-containedPrimary")
                for btn in b:
                    if "search" in (await btn.inner_text()).lower():
                        await btn.click()
                        break
                else:
                    if b: await b[0].click()
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(4000)

            for oi, order in enumerate(orders):
                if not order["order_href"]:
                    continue
                try:
                    # Directly navigate to order detail page
                    full_url = f"https://go.paperfly.com.bd{order['order_href']}"
                    await page.goto(full_url)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(3000)
                    if True:
                        # Screenshot of the order detail page
                        try:
                            ss = await page.screenshot(full_page=True)
                            order["screenshot_b64"] = base64.b64encode(ss).decode()
                        except Exception:
                            pass
                        result = await page.evaluate("""() => {
                            for (const d of document.querySelectorAll('div')) {
                                if (d.childElementCount === 0 && d.innerText.trim() === 'Timeline') {
                                    let p = d.parentElement;
                                    for (let i = 0; i < 8; i++) {
                                        if (p && p.children.length > 1) return { found: true, text: p.innerText };
                                        if (p) p = p.parentElement;
                                    }
                                }
                            }
                            return { found: false, text: '' };
                        }""")
                        if result["found"]:
                            order["timeline"] = [
                                l.strip() for l in result["text"].split("\n")
                                if l.strip() and l.strip() != "Timeline"
                            ]
                except Exception as te:
                    log.warning(f"Timeline error for {order['order_id']}: {te}")

            await browser.close()

        return {"phone": normalized, "orders": orders, "screenshot_b64": screenshot_b64, "error": None}

    except Exception as e:
        log.error(f"Scrape error: {e}", exc_info=True)
        return {"phone": phone, "orders": [], "screenshot_b64": None, "error": str(e)}


# ── Formatting ──────────────────────────────────────────────────────────────────
STATUS_EMOJI = {
    "delivered": "✅", "return": "🔄", "cancel": "❌",
    "pending": "🕐", "transit": "🚚", "pickup": "📦", "point": "📍",
}

def status_emoji(s: str) -> str:
    s = (s or "").lower()
    for key, emoji in STATUS_EMOJI.items():
        if key in s: return emoji
    return "📋"

def format_order(order: dict, idx: int) -> str:
    lines = [
        f"*{idx}. {order['order_id']}*",
        f"{status_emoji(order['status'])} {order['status']}",
        f"📅 {order['order_date']}",
        f"💰 {order['price']}",
        f"👤 {order['customer_details']}",
    ]
    if order.get("timeline"):
        lines.append("📍 *Timeline:*")
        for t in order["timeline"][:6]:
            lines.append(f"  • {t}")
        if len(order["timeline"]) > 6:
            lines.append(f"  _...+{len(order['timeline'])-6} more_")
    return "\n".join(lines)

def format_full_result(data: dict) -> str:
    orders = data["orders"]
    phone = data["phone"]
    if not orders:
        return f"📭 No orders found for `{phone}`"
    counts: dict[str, int] = {}
    for o in orders:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    summary = [f"Total: *{len(orders)}*"] + [f"{status_emoji(k)} {k}: *{v}*" for k, v in counts.items()]
    lines = [f"📱 *{phone}*", " | ".join(summary), "─" * 30]
    for i, order in enumerate(orders, 1):
        lines.append(format_order(order, i))
        if i < len(orders): lines.append("─" * 20)
    return "\n".join(lines)

def whatsapp_url(data: dict) -> str:
    import urllib.parse
    lines = [f"Paperfly Orders — {data['phone']}", f"Total: {len(data['orders'])}"]
    for o in data["orders"]:
        lines += [f"\n📦 {o['order_id']} | {o['status']}", f"📅 {o['order_date']}"]
        for t in o.get("timeline", [])[:3]:
            lines.append(f"  • {t}")
    return f"https://wa.me/?text={urllib.parse.quote(chr(10).join(lines))}"


# ── Handlers ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info(f"CMD /start from user {update.effective_user.id}")
    await update.message.reply_text(
        "👋 *Welcome to Paperfly Tracker Bot!*\n\n"
        "Just send a phone number to track orders:\n`01712345678`\n\n"
        "Or use: `/track 01712345678`\n\nType /help for more.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Help*\n\n"
        "`/track <phone>` — 90 day range\n"
        "`/track <phone> <days>` — custom range\n\n"
        "Or just send a phone number directly!",
        parse_mode=ParseMode.MARKDOWN,
    )

async def run_tracking(update: Update, phone: str, days_back: int):
    log.info(f"run_tracking called: phone={phone} days={days_back}")
    msg = await update.message.reply_text(
        f"🔍 Searching `{phone}`...\n⏳ Please wait 1–3 minutes.",
        parse_mode=ParseMode.MARKDOWN,
    )
    log.info("Initial message sent, starting scrape...")

    last_text = [""]
    last_edit = [0.0]
    MAJOR = ["Normalizing", "Launching", "Logging", "Logged in",
             "Waiting", "Searching", "Extracting", "Fetching"]

    async def on_progress(text: str):
        new_text = f"{last_text[0]}\n{text}".strip()
        last_text[0] = new_text
        now = asyncio.get_event_loop().time()
        if any(k in text for k in MAJOR) or (now - last_edit[0]) >= 5:
            last_edit[0] = now
            try:
                await msg.edit_text(new_text, parse_mode=ParseMode.MARKDOWN)
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning(f"edit_text failed: {e}")

    stop_typing = asyncio.Event()
    async def keep_typing():
        while not stop_typing.is_set():
            try:
                await update.message.chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    log.info("Typing task started, calling scrape_orders...")

    data = None
    try:
        data = await asyncio.wait_for(
            scrape_orders(phone, days_back, progress_cb=on_progress),
            timeout=300,
        )
        log.info(f"scrape_orders done: {len(data.get('orders', []))} orders, error={data.get('error')}")
    except asyncio.TimeoutError:
        log.error("scrape_orders TIMED OUT after 300s")
        await msg.edit_text(
            f"⏰ *Timed out* after 5 minutes.\nTry: `/track {phone} 30`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as e:
        log.exception(f"CRASH in scrape_orders: {e}")
        await msg.edit_text(
            f"❌ *Crashed:*\n`{str(e)}`\n\nPlease try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    finally:
        stop_typing.set()
        typing_task.cancel()

    if data["error"]:
        err_short = str(data["error"])[:300]
        await msg.edit_text(f"❌ *Error:*\n`{err_short}`", parse_mode=ParseMode.MARKDOWN)
        return

    result_text = format_full_result(data)

    import io
    if len(result_text) <= 4000:
        await msg.edit_text(result_text, parse_mode=ParseMode.MARKDOWN)
    else:
        await msg.edit_text(f"📱 *{data['phone']}* — {len(data['orders'])} orders", parse_mode=ParseMode.MARKDOWN)

    # Send screenshot + detail for each order
    for i, order in enumerate(data["orders"], 1):
        chunk = format_order(order, i)
        if order.get("screenshot_b64"):
            try:
                await update.message.reply_photo(
                    photo=io.BytesIO(base64.b64decode(order["screenshot_b64"])),
                    caption=chunk[:1024],
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception as e:
                log.warning(f"Order screenshot failed: {e}")
                if len(chunk) <= 4000:
                    await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        elif len(data["orders"]) > 1:
            if len(chunk) <= 4000:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 WhatsApp", url=whatsapp_url(data))],
        [InlineKeyboardButton("🔄 Search Again", callback_data=f"retrack:{phone}:{days_back}")],
    ])
    await update.message.reply_text("Options:", reply_markup=keyboard)
    log.info("run_tracking complete!")

async def cmd_track(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info(f"CMD /track args={ctx.args}")
    if not ctx.args:
        await update.message.reply_text("Usage: `/track <phone> [days]`", parse_mode=ParseMode.MARKDOWN)
        return
    phone = ctx.args[0]
    days_back = 90
    if len(ctx.args) >= 2:
        try: days_back = int(ctx.args[1])
        except ValueError: pass
    await run_tracking(update, phone, days_back)

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    log.info(f"MESSAGE received: '{text}' from user {update.effective_user.id}")
    cleaned = re.sub(r"[^\d০১২৩৪৫৬৭৮৯+]", "", text)
    if len(cleaned) >= 10:
        await run_tracking(update, text, 90)
    else:
        await update.message.reply_text(
            "Send a phone number, e.g. `01712345678`\nOr use /help",
            parse_mode=ParseMode.MARKDOWN,
        )

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if (query.data or "").startswith("retrack:"):
        _, phone, days = query.data.split(":", 2)
        await run_tracking(update, phone, int(days))

# ── Main ────────────────────────────────────────────────────────────────────────
def main():
    log.info("Building application...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
