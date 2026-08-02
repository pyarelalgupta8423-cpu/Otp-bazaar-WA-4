"""
WhatsApp Number Service
-----------------------
Admin creates country+price services.
User buys → balance deducted → admin gets request → admin replies with number
→ user gets number + Get OTP → admin supplies OTP → user Done → order complete.
If admin does not provide number within 20 minutes → automatic refund.
"""
import re
import time
import html
import logging
from telethon import events, Button
from telethon.errors import MessageNotModifiedError

from database import (
    users_col, wa_services_col, wa_orders_col, is_admin, has_perm,
    update_balance, db as mongo_db, ADMIN_ID,
)
from config import (
    bot, logger, PE_GIFT, PE_LIGHTNING, PE_CHECK, PE_LOCATION, PE_FLOWER,
    P_MONEY, P_INR, P_PHONE, P_WAIT, P_YES, P_NO, P_WARN, P_OTP, P_ACC, P_PKG,
    LOG_CHANNELS, WA_NUMBER_TIMEOUT_SECONDS,
)
from utils.keyboards import style_btn
from utils.states import get_user_lock

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# User: list services
# ─────────────────────────────────────────────

async def show_wa_services(event):
    rows = list(wa_services_col.find({"active": 1}).sort("country_name", 1))
    if not rows:
        msg = (
            f"<blockquote>{PE_FLOWER} <b>𝐖𝐡𝐚𝐭𝐬𝐀𝐩𝐩 𝐒𝐞𝐫𝐯𝐢𝐜𝐞</b></blockquote>\n\n"
            f"<blockquote>{P_WARN} No WhatsApp services available right now. "
            f"Please check back later.</blockquote>"
        )
        if isinstance(event, events.CallbackQuery.Event):
            try:
                await event.edit(msg, buttons=[[style_btn("🔙 Back", "cancel_action", "danger")]])
            except MessageNotModifiedError:
                pass
        else:
            await event.respond(msg)
        return

    # Header: Country | Price | Stock (dummy) — data rows share same callback
    btns = [[
        style_btn("Country", "noop", "primary", icon=5408995930416362034),
        style_btn("Price", "noop", "primary", icon=5409098988156629257),
        style_btn("Stock", "noop", "primary", icon=6129627894349045589),
    ]]
    for r in rows:
        sid = r["id"]
        name = r["country_name"]
        price = r["price"]
        cb = f"wa_sel|{sid}"
        btns.append([
            style_btn(f"📱 {name}", cb, "primary", icon=5408995930416362034),
            style_btn(f"{P_INR}{price}", cb, "primary", icon=5409320020058584473),
            style_btn("Live", cb, "success", icon=5409098988156629257),
        ])
    msg = (
        f"<blockquote>{PE_GIFT} <b>𝐖𝐡𝐚𝐭𝐬𝐀𝐩𝐩 𝐍𝐮𝐦𝐛𝐞𝐫 𝐒𝐞𝐫𝐯𝐢𝐜𝐞</b></blockquote>\n\n"
        f"<blockquote>Select a country (tap Country / Price / Stock — same). "
        f"Admin assigns a live number; OTP on request.</blockquote>"
    )
    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(msg, buttons=btns)
        except MessageNotModifiedError:
            pass
    else:
        await event.respond(msg, buttons=btns)


async def confirm_wa_buy(event, service_id: int):
    svc = wa_services_col.find_one({"id": int(service_id), "active": 1})
    if not svc:
        return await event.answer("❌ Service not found / inactive.", alert=True)

    name = svc["country_name"]
    price = int(svc["price"])
    msg = (
        f"<blockquote>{PE_GIFT} <b>𝐂𝐨𝐧𝐟𝐢𝐫𝐦 𝐖𝐡𝐚𝐭𝐬𝐀𝐩𝐩 𝐎𝐫𝐝𝐞𝐫</b></blockquote>\n\n"
        f"<blockquote>🌍 <b>Country:</b> {html.escape(str(name))}\n"
        f"{P_MONEY} <b>Price:</b> {P_INR}{price}</blockquote>\n\n"
        f"<blockquote>After confirm, balance will be deducted and your request "
        f"will be sent to admin. If a number is not assigned within "
        f"<b>20 minutes</b>, your money will be <b>auto-refunded</b>.</blockquote>"
    )
    btns = [
        [style_btn(
            f"✅ Confirm ({P_INR}{price})",
            f"wa_cf|{service_id}",
            "success",
            icon=5409320020058584473,
        )],
        [style_btn("❌ Cancel", "cancel_action", "danger", icon=6129888444245089008)],
    ]
    try:
        await event.edit(msg, buttons=btns)
    except MessageNotModifiedError:
        pass


async def process_wa_buy(event, service_id: int):
    uid = event.sender_id
    svc = wa_services_col.find_one({"id": int(service_id), "active": 1})
    if not svc:
        return await event.answer("❌ Service not available.", alert=True)

    name = svc["country_name"]
    price = int(svc["price"])

    async with get_user_lock(uid):
        bal_res = users_col.update_one(
            {"user_id": uid, "balance": {"$gte": price}},
            {"$inc": {"balance": -price}},
        )
        if bal_res.modified_count == 0:
            return await event.answer("❌ Insufficient Balance!", alert=True)

        oid = mongo_db.next_id("wa_orders")
        now = time.time()
        wa_orders_col.insert_one({
            "id": oid,
            "user_id": uid,
            "country_name": name,
            "price": price,
            "status": "pending_number",
            "phone": None,
            "otp": None,
            "admin_request_msg_id": None,
            "admin_request_chat_id": None,
            "admin_otp_msg_id": None,
            "admin_otp_chat_id": None,
            "user_msg_id": None,
            "created_ts": now,
            "deadline_ts": now + WA_NUMBER_TIMEOUT_SECONDS,
            "created_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        })

    wait_msg = (
        f"<blockquote>{P_WAIT} <b>Finding the best number for you…</b></blockquote>\n\n"
        f"<blockquote>🌍 Country: <b>{html.escape(str(name))}</b>\n"
        f"{P_MONEY} Paid: {P_INR}{price}\n"
        f"🧾 Order: <code>#{oid}</code></blockquote>\n\n"
        f"<blockquote>Please wait while admin assigns a number.\n"
        f"<i>Don't worry — if we can't find a number within 20 minutes, "
        f"your money will be automatically returned.</i></blockquote>"
    )
    try:
        sent = await event.edit(wait_msg, buttons=None)
        user_msg_id = sent.id if hasattr(sent, "id") else event.message_id
    except Exception:
        sent = await bot.send_message(uid, wait_msg)
        user_msg_id = sent.id

    wa_orders_col.update_one({"id": oid}, {"$set": {"user_msg_id": user_msg_id}})

    # Notify admin(s)
    admin_text = (
        f"{PE_LIGHTNING} <b>WHATSAPP NUMBER REQUEST</b>\n\n"
        f"{P_ACC} User: <code>{uid}</code>\n"
        f"🌍 Country: <b>{html.escape(str(name))}</b>\n"
        f"{P_MONEY} Price: {P_INR}{price}\n"
        f"🧾 Order: <code>#{oid}</code>\n"
        f"⏰ Reply within <b>20 minutes</b> or user is auto-refunded.\n\n"
        f"<b>➜ Reply to THIS message with the phone number</b>\n"
        f"<i>(Example: +919876543210)</i>"
    )

    targets = []
    if ADMIN_ID:
        targets.append(ADMIN_ID)
    for ch in LOG_CHANNELS:
        if ch and ch not in targets:
            targets.append(ch)

    primary_msg_id = None
    primary_chat = None
    for target in targets:
        try:
            m = await bot.send_message(target, admin_text)
            if primary_msg_id is None:
                primary_msg_id = m.id
                primary_chat = target
        except Exception as e:
            log.error("WA admin notify failed to %s: %s", target, e)

    if primary_msg_id is not None:
        wa_orders_col.update_one(
            {"id": oid},
            {"$set": {
                "admin_request_msg_id": primary_msg_id,
                "admin_request_chat_id": primary_chat,
            }},
        )


# ─────────────────────────────────────────────
# Admin replies with NUMBER (reply to request msg)
# ─────────────────────────────────────────────

async def handle_admin_number_reply(event):
    """Admin replied to a WA number-request message with a phone number."""
    if not is_admin(event.sender_id):
        return
    if not event.is_reply:
        return

    reply_id = event.reply_to_msg_id
    order = wa_orders_col.find_one({
        "admin_request_msg_id": reply_id,
        "status": "pending_number",
    })
    if not order:
        # Also allow matching if admin replied in another log channel —
        # search by recent pending is not safe; stick to msg id.
        return

    raw = (event.text or "").strip()
    # Extract phone-like token
    phone = re.sub(r"[^\d+]", "", raw)
    if len(re.sub(r"\D", "", phone)) < 8:
        return await event.reply(
            f"{P_WARN} Invalid number. Reply again with a valid phone "
            f"(e.g. <code>+919876543210</code>)."
        )

    oid = order["id"]
    uid = order["user_id"]
    country = order["country_name"]

    # Atomic claim
    claimed = wa_orders_col.find_one_and_update(
        {"id": oid, "status": "pending_number"},
        {"$set": {"status": "number_sent", "phone": phone}},
    )
    if not claimed:
        return await event.reply(f"{P_WARN} Order already processed.")

    user_text = (
        f"<blockquote>{PE_CHECK} <b>𝐖𝐡𝐚𝐭𝐬𝐀𝐩𝐩 𝐍𝐮𝐦𝐛𝐞𝐫 𝐀𝐬𝐬𝐢𝐠𝐧𝐞𝐝!</b></blockquote>\n\n"
        f"<blockquote>🌍 Country: <b>{html.escape(str(country))}</b>\n"
        f"{P_PHONE} Number: <code>{html.escape(phone)}</code>\n"
        f"🧾 Order: <code>#{oid}</code></blockquote>\n\n"
        f"<blockquote>1. Open WhatsApp → add this number / use for verification.\n"
        f"2. When OTP is needed, tap <b>Get OTP</b> below.\n"
        f"3. After you receive OTP and finish, tap <b>Done</b>.</blockquote>"
    )
    btns = [
        [style_btn("🔢 Get OTP", f"wa_otp|{oid}", "primary", icon=5409098988156629257)],
        [style_btn("✅ Done", f"wa_done|{oid}", "success", icon=5409320020058584473)],
    ]
    try:
        await bot.send_message(uid, user_text, buttons=btns)
    except Exception as e:
        log.error("Failed to send number to user %s: %s", uid, e)
        # rollback status so admin can retry? keep number_sent and notify
        await event.reply(f"{P_WARN} Number saved but failed to message user: {e}")
        return

    await event.reply(
        f"{P_YES} Number <code>{html.escape(phone)}</code> sent to user "
        f"<code>{uid}</code> (Order #{oid})."
    )


# ─────────────────────────────────────────────
# User: Get OTP
# ─────────────────────────────────────────────

async def request_wa_otp(event, order_id: int):
    uid = event.sender_id
    order = wa_orders_col.find_one({"id": int(order_id)})
    if not order:
        return await event.answer("❌ Order not found.", alert=True)
    if int(order["user_id"]) != int(uid):
        return await event.answer("⛔ Not your order.", alert=True)
    if order["status"] not in ("number_sent", "otp_requested", "otp_sent"):
        return await event.answer(
            f"⚠️ Order status: {order['status']}. Cannot request OTP now.",
            alert=True,
        )
    if not order.get("phone"):
        return await event.answer("❌ No number assigned yet.", alert=True)

    phone = order["phone"]
    oid = order["id"]

    wa_orders_col.update_one(
        {"id": oid},
        {"$set": {"status": "otp_requested"}},
    )

    admin_text = (
        f"{P_OTP} <b>WHATSAPP OTP REQUEST</b>\n\n"
        f"{P_ACC} User: <code>{uid}</code>\n"
        f"{P_PHONE} Number: <code>{html.escape(str(phone))}</code>\n"
        f"🌍 Country: <b>{html.escape(str(order['country_name']))}</b>\n"
        f"🧾 Order: <code>#{oid}</code>\n\n"
        f"<b>➜ Reply to THIS message with the OTP code</b>"
    )

    targets = []
    if ADMIN_ID:
        targets.append(ADMIN_ID)
    for ch in LOG_CHANNELS:
        if ch and ch not in targets:
            targets.append(ch)

    primary_msg_id = None
    primary_chat = None
    for target in targets:
        try:
            m = await bot.send_message(target, admin_text)
            if primary_msg_id is None:
                primary_msg_id = m.id
                primary_chat = target
        except Exception as e:
            log.error("WA OTP notify failed to %s: %s", target, e)

    if primary_msg_id is not None:
        wa_orders_col.update_one(
            {"id": oid},
            {"$set": {
                "admin_otp_msg_id": primary_msg_id,
                "admin_otp_chat_id": primary_chat,
            }},
        )

    try:
        await event.answer("⏳ OTP request sent to admin…", alert=False)
    except Exception:
        pass
    try:
        await event.edit(
            f"<blockquote>{P_WAIT} <b>OTP request sent to admin</b></blockquote>\n\n"
            f"<blockquote>{P_PHONE} Number: <code>{html.escape(str(phone))}</code>\n"
            f"🧾 Order: <code>#{oid}</code>\n\n"
            f"Please wait — OTP will appear here once admin replies.</blockquote>",
            buttons=[
                [style_btn("🔢 Get OTP Again", f"wa_otp|{oid}", "primary", icon=5409098988156629257)],
                [style_btn("✅ Done", f"wa_done|{oid}", "success", icon=5409320020058584473)],
            ],
        )
    except Exception:
        await bot.send_message(
            uid,
            f"{P_WAIT} OTP request sent for <code>{html.escape(str(phone))}</code>. Please wait.",
        )


# ─────────────────────────────────────────────
# Admin replies with OTP
# ─────────────────────────────────────────────

async def handle_admin_otp_reply(event):
    if not is_admin(event.sender_id):
        return
    if not event.is_reply:
        return

    reply_id = event.reply_to_msg_id
    order = wa_orders_col.find_one({
        "admin_otp_msg_id": reply_id,
        "status": {"$in": ["otp_requested", "otp_sent", "number_sent"]},
    })
    if not order:
        return

    otp = (event.text or "").strip()
    # Prefer digit code
    m = re.search(r"\b(\d{4,8})\b", otp)
    if m:
        otp = m.group(1)
    if not otp or len(otp) < 3:
        return await event.reply(f"{P_WARN} Invalid OTP. Reply again with the code.")

    oid = order["id"]
    uid = order["user_id"]
    phone = order.get("phone") or ""

    wa_orders_col.update_one(
        {"id": oid},
        {"$set": {"status": "otp_sent", "otp": otp}},
    )

    user_text = (
        f"<blockquote>{PE_CHECK} <b>𝐖𝐡𝐚𝐭𝐬𝐀𝐩𝐩 𝐎𝐓𝐏 𝐑𝐞𝐜𝐞𝐢𝐯𝐞𝐝!</b></blockquote>\n\n"
        f"<blockquote>{P_PHONE} Number: <code>{html.escape(str(phone))}</code>\n"
        f"{P_OTP} OTP: <code><tg-spoiler>{html.escape(otp)}</tg-spoiler></code>\n"
        f"🧾 Order: <code>#{oid}</code></blockquote>\n\n"
        f"<blockquote>Enter this OTP in WhatsApp. When finished, tap <b>Done</b>.</blockquote>"
    )
    btns = [
        [style_btn("🔢 Get OTP Again", f"wa_otp|{oid}", "primary", icon=5409098988156629257)],
        [style_btn("✅ Done", f"wa_done|{oid}", "success", icon=5409320020058584473)],
    ]
    try:
        await bot.send_message(uid, user_text, buttons=btns)
    except Exception as e:
        log.error("Failed to send OTP to user %s: %s", uid, e)
        return await event.reply(f"{P_WARN} OTP saved but failed to message user: {e}")

    await event.reply(f"{P_YES} OTP sent to user <code>{uid}</code> (Order #{oid}).")


# ─────────────────────────────────────────────
# User: Done
# ─────────────────────────────────────────────

async def complete_wa_order(event, order_id: int):
    uid = event.sender_id
    order = wa_orders_col.find_one({"id": int(order_id)})
    if not order:
        return await event.answer("❌ Order not found.", alert=True)
    if int(order["user_id"]) != int(uid):
        return await event.answer("⛔ Not your order.", alert=True)
    if order["status"] == "completed":
        return await event.answer("✅ Already completed.", alert=True)
    if order["status"] == "refunded":
        return await event.answer("♻️ Order was refunded.", alert=True)
    if order["status"] not in ("number_sent", "otp_requested", "otp_sent"):
        return await event.answer(f"⚠️ Cannot complete (status: {order['status']}).", alert=True)

    oid = order["id"]
    claimed = wa_orders_col.find_one_and_update(
        {"id": oid, "status": {"$in": ["number_sent", "otp_requested", "otp_sent"]}},
        {"$set": {"status": "completed"}},
    )
    if not claimed:
        return await event.answer("⚠️ Already processed.", alert=True)

    try:
        await event.edit(
            f"<blockquote>{PE_CHECK} <b>Order Completed!</b></blockquote>\n\n"
            f"<blockquote>{P_PHONE} Number: <code>{html.escape(str(order.get('phone') or ''))}</code>\n"
            f"🌍 {html.escape(str(order.get('country_name') or ''))}\n"
            f"🧾 Order: <code>#{oid}</code>\n\n"
            f"Thank you for using WhatsApp service.</blockquote>",
            buttons=None,
        )
    except Exception:
        await bot.send_message(uid, f"{P_YES} Order #{oid} marked completed. Thank you!")

    admin_text = (
        f"{P_YES} <b>WA ORDER COMPLETED</b>\n"
        f"{P_ACC} User: <code>{uid}</code>\n"
        f"{P_PHONE} Number: <code>{html.escape(str(order.get('phone') or ''))}</code>\n"
        f"🧾 Order: <code>#{oid}</code>"
    )
    targets = [ADMIN_ID] if ADMIN_ID else []
    for ch in LOG_CHANNELS:
        if ch and ch not in targets:
            targets.append(ch)
    for target in targets:
        try:
            await bot.send_message(target, admin_text)
        except Exception:
            pass

    try:
        await event.answer("✅ Order completed!", alert=False)
    except Exception:
        pass


# ─────────────────────────────────────────────
# Timeout refund (called from background sweeper)
# ─────────────────────────────────────────────

async def cleanup_expired_wa_orders():
    """Refund pending_number orders past deadline_ts."""
    now = time.time()
    expired = list(wa_orders_col.find({
        "status": "pending_number",
        "deadline_ts": {"$lte": now},
    }))
    for order in expired:
        oid = order["id"]
        uid = order["user_id"]
        price = int(order.get("price") or 0)
        claimed = wa_orders_col.find_one_and_update(
            {"id": oid, "status": "pending_number"},
            {"$set": {"status": "refunded"}},
        )
        if not claimed:
            continue
        if price > 0:
            async with get_user_lock(uid):
                update_balance(uid, price)
        try:
            await bot.send_message(
                uid,
                f"<blockquote>{P_WARN} <b>WhatsApp order timed out</b></blockquote>\n\n"
                f"<blockquote>Order <code>#{oid}</code> — admin did not assign a number "
                f"within 20 minutes.\n"
                f"{P_MONEY} <b>{P_INR}{price}</b> has been refunded to your balance.</blockquote>",
            )
        except Exception:
            pass
        # Notify admin
        for target in ([ADMIN_ID] if ADMIN_ID else []) + list(LOG_CHANNELS):
            if not target:
                continue
            try:
                await bot.send_message(
                    target,
                    f"{P_WARN} WA Order #{oid} auto-refunded (no number in 20 min). "
                    f"User <code>{uid}</code> got {P_INR}{price} back.",
                )
            except Exception:
                pass


# ─────────────────────────────────────────────
# Admin service management UI helpers
# ─────────────────────────────────────────────

async def wa_services_admin_menu(event):
    rows = list(wa_services_col.find({}).sort("country_name", 1))
    msg = f"{PE_GIFT} <b>WhatsApp Services</b>\n\n"
    if not rows:
        msg += "<i>No services yet. Add one.</i>\n"
    else:
        for r in rows:
            st = "🟢" if r.get("active", 1) == 1 else "🔴"
            msg += (
                f"{st} ID <code>{r['id']}</code> — "
                f"<b>{html.escape(str(r['country_name']))}</b> "
                f"{P_INR}{r['price']}\n"
            )
    btns = [
        [style_btn("➕ Add Service", "adm_wa_add", "success", icon=5409098988156629257)],
        [style_btn("🗑 Delete Service", "adm_wa_del", "danger", icon=6129888444245089008)],
        [style_btn("Back", "adm_adminmain", "danger", icon=6129888444245089008)],
    ]
    try:
        await event.edit(msg, buttons=btns)
    except Exception:
        await bot.send_message(event.chat_id, msg, buttons=btns)


# ─────────────────────────────────────────────
# Register handlers
# ─────────────────────────────────────────────

def register_whatsapp(bot):
    @bot.on(events.CallbackQuery(pattern=rb"^noop$"))
    async def cb_noop_wa(e):
        try:
            await e.answer("Select a row below", alert=False)
        except Exception:
            pass

    @bot.on(events.NewMessage(pattern=r"(?i)^(📱 𝐖𝐡𝐚𝐭𝐬𝐀𝐩𝐩|📱 WhatsApp|WhatsApp)$"))
    async def msg_wa(e):
        await show_wa_services(e)

    @bot.on(events.CallbackQuery(pattern=r"^wa_sel\|(\d+)$"))
    async def cb_wa_sel(e):
        sid = int(e.pattern_match.group(1))
        await confirm_wa_buy(e, sid)

    @bot.on(events.CallbackQuery(pattern=r"^wa_cf\|(\d+)$"))
    async def cb_wa_cf(e):
        sid = int(e.pattern_match.group(1))
        await process_wa_buy(e, sid)

    @bot.on(events.CallbackQuery(pattern=r"^wa_otp\|(\d+)$"))
    async def cb_wa_otp(e):
        oid = int(e.pattern_match.group(1))
        await request_wa_otp(e, oid)

    @bot.on(events.CallbackQuery(pattern=r"^wa_done\|(\d+)$"))
    async def cb_wa_done(e):
        oid = int(e.pattern_match.group(1))
        await complete_wa_order(e, oid)

    # Admin reply router: number OR otp
    @bot.on(events.NewMessage(func=lambda e: e.is_reply and e.sender_id and is_admin(e.sender_id)))
    async def admin_wa_replies(e):
        # Try number assignment first, then OTP
        reply_id = e.reply_to_msg_id
        if not reply_id:
            return
        if wa_orders_col.find_one({"admin_request_msg_id": reply_id, "status": "pending_number"}):
            await handle_admin_number_reply(e)
            return
        if wa_orders_col.find_one({
            "admin_otp_msg_id": reply_id,
            "status": {"$in": ["otp_requested", "otp_sent", "number_sent"]},
        }):
            await handle_admin_otp_reply(e)
            return
