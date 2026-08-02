import os
import asyncio
import time
import zipfile
import re
import tempfile
import shutil
from telethon import events, Button, TelegramClient, types
from telethon.errors import MessageNotModifiedError
from database import users_col, stock_col, orders_col, get_flag_by_country_name, db as mongo_db
from config import (
    PE_LOCATION, PE_GIFT, PE_LIGHTNING, PE_CHECK, P_MONEY, P_PKG, P_CARD, P_WARN,
    P_NO, P_YES, P_INR, P_TIME, P_FLAG, P_OTP, P_2FA, P_PHONE, AUTO_CANCEL_SECONDS,
    OTP_REGEX, bot, logger, API_ID, API_HASH,
)
from utils.keyboards import style_btn
from utils.states import active_orders, session_buy_state, get_user_lock
from utils.gridfs_sessions import (
    download_session, get_session_bytes, cleanup_temp_session, delete_session_file,
)


# ─────────────────────────────────────────────
# Country / Year selection (shared by single + bulk)
# ─────────────────────────────────────────────

async def show_countries(event, mode, page):
    limit = 12
    offset = (page - 1) * limit
    # count + min price per country
    pipeline = [
        {"$match": {"available": 1}},
        {"$group": {
            "_id": "$country_name",
            "count": {"$sum": 1},
            "min_price": {"$min": "$price"},
        }},
        {"$sort": {"_id": 1}},
    ]
    rows = [(r["_id"], r["count"], r.get("min_price") or 0) for r in stock_col.aggregate(pipeline)]
    total = len(rows)
    countries = rows[offset:offset + limit]

    if not countries:
        msg = f"{P_WARN} 𝐍ᴏ sᴛᴏᴄᴋ ᴀᴠᴀɪʟᴀʙʟᴇ ᴀᴛ ᴛʜᴇ ᴍᴏᴍᴇɴᴛ. 𝐏ʟᴇᴀsᴇ ᴄʜᴇᴄᴋ ʙᴀᴄᴋ ʟᴀᴛᴇʀ!"
        if isinstance(event, events.CallbackQuery.Event):
            return await event.edit(msg)
        return await event.respond(msg)

    # Header row (dummy) + data rows: Country | Price | Stock — any cell opens same country
    f_btns = [[
        style_btn("Country", "noop", "primary", icon=5408995930416362034),
        style_btn("Price", "noop", "primary", icon=5409098988156629257),
        style_btn("Stock", "noop", "primary", icon=6129627894349045589),
    ]]
    for c_name, count, min_price in countries:
        flag = get_flag_by_country_name(c_name)
        cb = f"bc|{mode}|{c_name}"
        f_btns.append([
            style_btn(f"{flag} {c_name}", cb, "primary", icon=6154249597532248059),
            style_btn(f"{P_INR}{min_price}", cb, "primary", icon=5409320020058584473),
            style_btn(f"{count}", cb, "success", icon=5409098988156629257),
        ])

    nav = []
    if page > 1:
        nav.append(style_btn("𝐏ʀᴇᴠ", f"pg_c|{mode}|{page - 1}", "primary", icon=6129627894349045589))
    if offset + limit < total:
        nav.append(style_btn("𝐍ᴇxᴛ", f"pg_c|{mode}|{page + 1}", "primary", icon=6129732880529628243))
    if nav:
        f_btns.append(nav)

    title = "𝐒ᴇʟᴇᴄᴛ ᴀ 𝐂ᴏᴜɴᴛʀʏ" if mode == "single" else "𝐁ᴜʟᴋ 𝐒ᴇssɪᴏɴs — 𝐒ᴇʟᴇᴄᴛ 𝐂ᴏᴜɴᴛʀʏ"
    msg = (
        f"<blockquote>{PE_LOCATION} <b>{title}</b> (𝐏ᴀɢᴇ {page})</blockquote>\n"
        f"<blockquote>Tap <b>Country</b>, <b>Price</b> or <b>Stock</b> — same result.</blockquote>"
    )
    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(msg, buttons=f_btns)
        except MessageNotModifiedError:
            pass
    else:
        await event.respond(msg, buttons=f_btns)


async def show_years(event, mode, country):
    years_pipe = [
        {"$match": {"country_name": country, "available": 1}},
        {"$group": {"_id": {"year": "$account_year", "price": "$price"}, "count": {"$sum": 1}}},
        {"$sort": {"_id.year": -1}},
    ]
    years = [
        (r["_id"]["year"], r["count"], r["_id"]["price"])
        for r in stock_col.aggregate(years_pipe)
    ]
    if not years:
        return await event.edit(f"{P_WARN} 𝐍ᴏ sᴛᴏᴄᴋ ʟᴇғᴛ ғᴏʀ {country}.")

    flag = get_flag_by_country_name(country)
    btns = [[
        style_btn("Country", "noop", "primary", icon=5408995930416362034),
        style_btn("Price", "noop", "primary", icon=5409098988156629257),
        style_btn("Stock", "noop", "primary", icon=6129627894349045589),
    ]]
    for y, count, price in years:
        cb = f"by|{mode}|{country}|{y}|{price}"
        # Country column shows year label (variant), all 3 open same buy
        btns.append([
            style_btn(f"{flag} {y}", cb, "primary", icon=5408995930416362034),
            style_btn(f"{P_INR}{price}", cb, "primary", icon=5409320020058584473),
            style_btn(f"{count}", cb, "success", icon=5409098988156629257),
        ])
    btns.append([style_btn("𝐁ᴀᴄᴋ", f"pg_c|{mode}|1", "danger", icon=6129812419028982717)])
    await event.edit(
        f"<blockquote>{flag} <b>{country}</b> — select row (any column)</blockquote>",
        buttons=btns,
    )


# ─────────────────────────────────────────────
# SINGLE BUY (OTP flow + GridFS session)
# ─────────────────────────────────────────────

async def confirm_purchase(event, country, year, price):
    msg = (
        f"<blockquote>{PE_GIFT} <b>𝐂ᴏɴғɪʀᴍ 𝐏ᴜʀᴄʜᴀsᴇ</b>\n\n"
        f"{P_FLAG} 𝐂ᴏᴜɴᴛʀʏ: {country}\n📆 𝐘ᴇᴀʀ: {year}\n"
        f"{P_MONEY} 𝐏ʀɪᴄᴇ: {P_INR}{price}\n\n𝐀ʀᴇ ʏᴏᴜ sᴜʀᴇ?</blockquote>"
    )
    btns = [
        [style_btn("𝐂ᴏɴғɪʀᴍ 𝐁ᴜʏ", f"buy_cf|{country}|{year}|{price}", "success", icon=5409320020058584473)],
        [style_btn("𝐂ᴀɴᴄᴇʟ", "cancel_action", "danger", icon=6129888444245089008)],
    ]
    await event.edit(msg, buttons=btns)


async def process_purchase(event, country, year, price_str):
    """Single account buy — reserve stock, download session from GridFS, listen for OTP."""
    uid, price = event.sender_id, int(price_str)

    async with get_user_lock(uid):
        disc_row = users_col.find_one({"user_id": uid}, {"discount": 1})
        discount = disc_row.get("discount", 0) if disc_row else 0
        final_price = price if discount == 0 else int(price * (100 - discount) / 100)

        from pymongo import ReturnDocument
        row = stock_col.find_one_and_update(
            {
                "country_name": country,
                "account_year": int(year),
                "price": price,
                "available": 1,
            },
            {"$set": {"available": 0}},
            return_document=ReturnDocument.AFTER,
        )
        if not row:
            return await event.answer("❌ Out of stock!", alert=True)

        phone = row["phone"]
        twofa_pass = row.get("twofa", "None")
        gridfs_id = row.get("gridfs_id")
        local_sess = row.get("session_file")  # legacy fallback

        bal_res = users_col.update_one(
            {"user_id": uid, "balance": {"$gte": final_price}},
            {"$inc": {"balance": -final_price}},
        )
        if bal_res.modified_count == 0:
            stock_col.update_one({"phone": phone}, {"$set": {"available": 1}})
            return await event.answer("❌ Insufficient Balance!", alert=True)

    await event.edit(
        f"{PE_LIGHTNING} <b>𝐏ʀᴏᴄᴇssɪɴɢ ʏᴏᴜʀ ᴏʀᴅᴇʀ...</b>\n"
        f"𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ ᴡʜɪʟᴇ ᴡᴇ ɪɴɪᴛɪᴀʟɪᴢᴇ ᴛʜᴇ sᴇssɪᴏɴ."
    )

    # Resolve session path: prefer GridFS, fallback to local file
    sess_base = None
    tmp_from_gridfs = False
    try:
        if gridfs_id:
            sess_base = download_session(gridfs_id, phone)
            tmp_from_gridfs = True
        elif local_sess and os.path.exists(
            local_sess if local_sess.endswith(".session") else local_sess + ".session"
        ):
            sess_base = local_sess[:-8] if local_sess.endswith(".session") else local_sess
        else:
            raise FileNotFoundError("No session binary found (GridFS or local)")
    except Exception as e:
        logger.error("Session resolve error for %s: %s", phone, e)
        async with get_user_lock(uid):
            users_col.update_one({"user_id": uid}, {"$inc": {"balance": final_price}})
            stock_col.update_one({"phone": phone}, {"$set": {"available": 1}})
        return await event.edit(f"{P_NO} <b>Error loading session file.</b> Money refunded.")

    client = TelegramClient(sess_base, API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise Exception("Session expired or not authorized")
    except Exception as e:
        logger.error(f"Client init error: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        if tmp_from_gridfs:
            cleanup_temp_session(sess_base)
        async with get_user_lock(uid):
            users_col.update_one({"user_id": uid}, {"$inc": {"balance": final_price}})
            stock_col.update_one({"phone": phone}, {"$set": {"available": 1}})
        return await event.edit(f"{P_NO} <b>Error initializing account.</b> Money refunded.")

    c_icon = get_flag_by_country_name(country)
    actual_year = int(year)
    msg = (
        f"<blockquote expandable>{PE_LIGHTNING} <b>𝐎ʀᴅᴇʀ 𝐀ᴄᴛɪᴠᴇ!</b>\n\n"
        f"{P_PHONE} <b>𝐏ʜᴏɴᴇ:</b> <code>{phone}</code>\n"
        f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {c_icon} {country}\n\n"
        f"🔻 <b>𝐈ɴsᴛʀᴜᴄᴛɪᴏɴs:</b>\n"
        f"1. 𝐎ᴘᴇɴ 𝐓ᴇʟᴇɢʀᴀᴍ & 𝐀ᴅᴅ 𝐀ᴄᴄᴏᴜɴᴛ\n"
        f"2. 𝐄ɴᴛᴇʀ ᴛʜᴇ ɴᴜᴍʙᴇʀ ᴀʙᴏᴠᴇ.\n"
        f"3. ⏳ <b>𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ!</b> 𝐓ʜᴇ ʙᴏᴛ ɪs ᴀᴄᴛɪᴠᴇʟʏ ʟɪsᴛᴇɴɪɴɢ ғᴏʀ ʏᴏᴜʀ 𝐎𝐓𝐏 "
        f"ᴀɴᴅ ᴡɪʟʟ sᴇɴᴅ ɪᴛ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴏɴᴄᴇ 𝐓ᴇʟᴇɢʀᴀᴍ ᴅᴇʟɪᴠᴇʀs ɪᴛ.\n\n"
        f"<i>𝐍ᴏᴛᴇ: 𝐈ғ ɴᴏ 𝐎𝐓𝐏 ɪs ʀᴇᴄᴇɪᴠᴇᴅ ᴡɪᴛʜɪɴ 10 ᴍɪɴᴜᴛᴇs, ᴛʜᴇ ʙᴏᴛ ᴡɪʟʟ "
        f"ᴀᴜᴛᴏ-ᴄᴀɴᴄᴇʟ ᴀɴᴅ ʀᴇғᴜɴᴅ ʏᴏᴜʀ ʙᴀʟᴀɴᴄᴇ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ.</i></blockquote>"
    )

    sent_msg = await event.edit(msg)

    active_orders[phone] = {
        "uid": uid,
        "client": client,
        "sess": sess_base,
        "tmp_from_gridfs": tmp_from_gridfs,
        "gridfs_id": gridfs_id,
        "start_time": time.time(),
        "paid": False,
        "price": final_price,
        "country": country,
        "year": actual_year,
        "c_icon": c_icon,
        "twofa": twofa_pass,
        "msg_id": sent_msg.id,
    }
    asyncio.create_task(auto_otp_task(phone))


async def _refund_and_release(phone, order):
    """Refund balance and return stock when an unpaid order expires or fails."""
    uid = order["uid"]
    try:
        await order["client"].disconnect()
    except Exception:
        pass
    if order.get("tmp_from_gridfs"):
        cleanup_temp_session(order.get("sess"))
    async with get_user_lock(uid):
        if not order.get("paid"):
            users_col.update_one({"user_id": uid}, {"$inc": {"balance": order["price"]}})
            stock_col.update_one({"phone": phone}, {"$set": {"available": 1}})


async def auto_otp_task(phone):
    if phone not in active_orders:
        return

    order = active_orders[phone]
    client = order["client"]
    start_time = order["start_time"]
    uid = order["uid"]
    msg_id = order["msg_id"]
    got_otp = False

    try:
        while time.time() - start_time < AUTO_CANCEL_SECONDS:
            if phone not in active_orders:
                return
            try:
                try:
                    peer = await client.get_input_entity(777000)
                except Exception:
                    peer = types.InputPeerUser(user_id=777000, access_hash=0)
                msgs = await client.get_messages(peer, limit=5)
                code = None
                for m in msgs:
                    if m.date.timestamp() > start_time - 10:
                        if (
                            m.message
                            and re.search(OTP_REGEX, m.message)
                            and "Login detected" not in m.message
                        ):
                            code = re.search(OTP_REGEX, m.message).group()
                            break

                if code:
                    if not order["paid"]:
                        order["paid"] = True
                        async with get_user_lock(uid):
                            oid = mongo_db.next_id("orders")
                            orders_col.insert_one({
                                "id": oid,
                                "user_id": uid,
                                "country": order["country"],
                                "year": order["year"],
                                "price": order["price"],
                                "phone": phone,
                                "otp": code,
                                "mode": "single",
                                "date": __import__("datetime").datetime.utcnow().strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                ),
                            })
                            # Remove from stock permanently after successful OTP
                            stock_doc = stock_col.find_one({"phone": phone})
                            if stock_doc and stock_doc.get("gridfs_id"):
                                delete_session_file(stock_doc["gridfs_id"])
                            stock_col.delete_one({"phone": phone})

                    got_otp = True
                    twofa_text = (
                        f"{P_2FA} <b>2FA:</b> <code>{order['twofa']}</code>"
                        if order["twofa"] != "None"
                        else "🔓 <b>2FA:</b> <code>Disabled (No Password)</code>"
                    )
                    msg_text = (
                        f"<blockquote>{PE_CHECK} <b>𝐋ᴀᴛᴇsᴛ 𝐎𝐓𝐏 𝐅ᴇᴛᴄʜᴇᴅ!</b>\n\n"
                        f"{P_PHONE} <b>𝐏ʜᴏɴᴇ:</b> <code>{phone}</code>\n"
                        f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {order['c_icon']} {order['country']}\n"
                        f"{P_OTP} <b>𝐎𝐓𝐏:</b> <code><tg-spoiler>{code}</tg-spoiler></code>\n"
                        f"{twofa_text}</blockquote>"
                    )
                    try:
                        await bot.edit_message(
                            uid,
                            msg_id,
                            msg_text,
                            buttons=[
                                [Button.inline("🔄 𝐆ᴇᴛ 𝐎𝐓𝐏 𝐀ɢᴀɪɴ", f"get_otp_again|{phone}")],
                                [
                                    style_btn(
                                        "🚪 𝐅ɪɴɪsʜ & 𝐋ᴏɢᴏᴜᴛ",
                                        f"logout_bot|{phone}",
                                        "danger",
                                        icon=6129627894349045589,
                                    )
                                ],
                            ],
                        )
                    except MessageNotModifiedError:
                        pass
                    return
            except Exception as ex:
                logger.error(f"OTP fetch error for {phone}: {ex}")
            await asyncio.sleep(6)

        # Timeout
        if phone in active_orders and not active_orders[phone].get("paid"):
            order = active_orders.pop(phone, None)
            if order:
                await _refund_and_release(phone, order)
                try:
                    await bot.edit_message(
                        uid,
                        msg_id,
                        f"{P_TIME} <b>𝐎ʀᴅᴇʀ 𝐄xᴘɪʀᴇᴅ!</b>\n"
                        f"𝐓ʜᴇ 10-ᴍɪɴᴜᴛᴇ ʟɪᴍɪᴛ ғᴏʀ <code>{phone}</code> ʀᴀɴ ᴏᴜᴛ. "
                        f"𝐘ᴏᴜʀ ᴍᴏɴᴇʏ ({P_INR}{order['price']}) ʜᴀs ʙᴇᴇɴ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ʀᴇғᴜɴᴅᴇᴅ.",
                    )
                except Exception:
                    pass
    except Exception as fatal:
        logger.error(f"auto_otp_task fatal for {phone}: {fatal}")
        if phone in active_orders and not active_orders[phone].get("paid"):
            order = active_orders.pop(phone, None)
            if order:
                await _refund_and_release(phone, order)
    finally:
        if not got_otp and phone in active_orders and not active_orders[phone].get("paid"):
            order = active_orders.pop(phone, None)
            if order:
                await _refund_and_release(phone, order)


# ─────────────────────────────────────────────
# BULK BUY — quantity picker + ZIP download
# ─────────────────────────────────────────────

async def show_bulk_qty(event, country, year, price, available):
    """Let buyer pick how many sessions they want (max min(available, 20))."""
    flag = get_flag_by_country_name(country)
    max_qty = min(int(available), 20)
    presets = [1, 2, 5, 10, 20]
    qty_btns = []
    row = []
    for q in presets:
        if q <= max_qty:
            row.append(
                style_btn(
                    f"x{q}",
                    f"bulk_qty|{country}|{year}|{price}|{q}",
                    "primary",
                    icon=5408995930416362034,
                )
            )
            if len(row) == 3:
                qty_btns.append(row)
                row = []
    if row:
        qty_btns.append(row)

    # Custom quantity via text input path
    qty_btns.append([
        style_btn(
            "✏️ Custom Qty",
            f"bulk_custom|{country}|{year}|{price}|{max_qty}",
            "success",
            icon=5409098988156629257,
        )
    ])
    qty_btns.append([
        style_btn("𝐁ᴀᴄᴋ", f"bc|bulk|{country}", "danger", icon=6129812419028982717)
    ])

    total_example = price  # 1 unit
    msg = (
        f"<blockquote>{PE_GIFT} <b>𝐁ᴜʟᴋ 𝐒ᴇssɪᴏɴ 𝐁ᴜʏ</b></blockquote>\n\n"
        f"<blockquote>{flag} <b>{country}</b> · {year}\n"
        f"{P_MONEY} 𝐏ʀɪᴄᴇ / session: {P_INR}{price}\n"
        f"{P_PKG} 𝐀ᴠᴀɪʟᴀʙʟᴇ: <b>{available}</b> (max {max_qty} per order)</blockquote>\n\n"
        f"<blockquote>Select quantity — you will receive a <b>ZIP</b> of "
        f"<code>.session</code> files.</blockquote>"
    )
    await event.edit(msg, buttons=qty_btns)


async def confirm_bulk(event, country, year, price, qty):
    flag = get_flag_by_country_name(country)
    total = int(price) * int(qty)
    msg = (
        f"<blockquote>{PE_GIFT} <b>𝐂ᴏɴғɪʀᴍ 𝐁ᴜʟᴋ 𝐏ᴜʀᴄʜᴀsᴇ</b></blockquote>\n\n"
        f"<blockquote>{flag} {country} · {year}\n"
        f"{P_PKG} Quantity: <b>{qty}</b>\n"
        f"{P_MONEY} Unit: {P_INR}{price}\n"
        f"{P_MONEY} <b>Total: {P_INR}{total}</b></blockquote>\n\n"
        f"<blockquote>You will get a ZIP with {qty} × <code>.session</code> files.</blockquote>"
    )
    btns = [
        [
            style_btn(
                f"✅ Buy {qty} for {P_INR}{total}",
                f"bulk_cf|{country}|{year}|{price}|{qty}",
                "success",
                icon=5409320020058584473,
            )
        ],
        [style_btn("𝐂ᴀɴᴄᴇʟ", "cancel_action", "danger", icon=6129888444245089008)],
    ]
    await event.edit(msg, buttons=btns)


async def process_bulk_purchase(event, country, year, price_str, qty_str):
    """
    Reserve N available sessions, deduct balance, package into ZIP from GridFS,
    send to user, mark sold + delete GridFS entries.
    """
    uid = event.sender_id
    price = int(price_str)
    qty = int(qty_str)
    if qty < 1 or qty > 20:
        return await event.answer("❌ Quantity must be 1–20", alert=True)

    async with get_user_lock(uid):
        # Bulk buy: no user discount — fixed unit price
        unit = price
        total = unit * qty

        # Check balance first
        bal_row = users_col.find_one({"user_id": uid}, {"balance": 1})
        bal = (bal_row or {}).get("balance", 0)
        if bal < total:
            return await event.answer(
                f"❌ Insufficient Balance! Need {P_INR}{total}, have {P_INR}{bal}",
                alert=True,
            )

        # Atomically claim up to `qty` documents
        claimed = []
        for _ in range(qty):
            from pymongo import ReturnDocument
            doc = stock_col.find_one_and_update(
                {
                    "country_name": country,
                    "account_year": int(year),
                    "price": price,
                    "available": 1,
                },
                {"$set": {"available": 0}},
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                break
            claimed.append(doc)

        if len(claimed) < qty:
            # Release any partial claim
            for d in claimed:
                stock_col.update_one({"phone": d["phone"]}, {"$set": {"available": 1}})
            return await event.answer(
                f"❌ Only {len(claimed)} left in stock (requested {qty})",
                alert=True,
            )

        # Deduct balance
        bal_res = users_col.update_one(
            {"user_id": uid, "balance": {"$gte": total}},
            {"$inc": {"balance": -total}},
        )
        if bal_res.modified_count == 0:
            for d in claimed:
                stock_col.update_one({"phone": d["phone"]}, {"$set": {"available": 1}})
            return await event.answer("❌ Insufficient Balance!", alert=True)

    await event.edit(
        f"{PE_LIGHTNING} <b>Building your ZIP...</b>\n"
        f"Packaging {qty} session files from GridFS."
    )

    tmp_dir = tempfile.mkdtemp(prefix="bulk_zip_")
    zip_path = os.path.join(tmp_dir, f"{country}_{year}_{qty}sessions.zip")
    phones_ok = []
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for doc in claimed:
                phone = doc["phone"]
                gridfs_id = doc.get("gridfs_id")
                data = None
                if gridfs_id:
                    try:
                        data = get_session_bytes(gridfs_id)
                    except Exception as e:
                        logger.error("GridFS read fail %s: %s", phone, e)
                if data is None:
                    # legacy local fallback
                    local = doc.get("session_file")
                    if local:
                        p = local if local.endswith(".session") else local + ".session"
                        if os.path.isfile(p):
                            with open(p, "rb") as f:
                                data = f.read()
                if data is None:
                    logger.warning("No binary for %s — skipping in ZIP", phone)
                    # release this one back
                    stock_col.update_one({"phone": phone}, {"$set": {"available": 1}})
                    continue
                zf.writestr(f"{phone}.session", data)
                phones_ok.append(phone)

        if not phones_ok:
            # full failure — refund everything
            async with get_user_lock(uid):
                users_col.update_one({"user_id": uid}, {"$inc": {"balance": total}})
                for d in claimed:
                    stock_col.update_one({"phone": d["phone"]}, {"$set": {"available": 1}})
            return await event.edit(
                f"{P_NO} <b>Could not package any sessions.</b> Full refund issued."
            )

        # Partial success: refund for missing ones
        missing = qty - len(phones_ok)
        refund_amt = missing * unit
        if refund_amt > 0:
            async with get_user_lock(uid):
                users_col.update_one({"user_id": uid}, {"$inc": {"balance": refund_amt}})

        # Record orders + permanently remove sold stock + GridFS
        async with get_user_lock(uid):
            for phone in phones_ok:
                oid = mongo_db.next_id("orders")
                orders_col.insert_one({
                    "id": oid,
                    "user_id": uid,
                    "country": country,
                    "year": int(year),
                    "price": unit,
                    "phone": phone,
                    "otp": None,
                    "mode": "bulk",
                    "date": __import__("datetime").datetime.utcnow().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                })
                doc = next((d for d in claimed if d["phone"] == phone), None)
                if doc and doc.get("gridfs_id"):
                    delete_session_file(doc["gridfs_id"])
                # also wipe local mirror if any
                local = (doc or {}).get("session_file")
                if local:
                    for ext in [".session", ".session-wal", ".session-shm", ".session-journal"]:
                        p = (local if local.endswith(".session") else local + ".session")
                        base = p[:-8] if p.endswith(".session") else p
                        fp = base + ext if not ext.startswith(base) else base
                        # simpler cleanup
                    try:
                        pth = local if local.endswith(".session") else local + ".session"
                        if os.path.isfile(pth):
                            os.remove(pth)
                    except Exception:
                        pass
                stock_col.delete_one({"phone": phone})

        flag = get_flag_by_country_name(country)
        caption = (
            f"<blockquote>{PE_CHECK} <b>𝐁ᴜʟᴋ 𝐎ʀᴅᴇʀ 𝐃ᴇʟɪᴠᴇʀᴇᴅ!</b></blockquote>\n\n"
            f"<blockquote>{flag} {country} · {year}\n"
            f"{P_PKG} Sessions: <b>{len(phones_ok)}</b>\n"
            f"{P_MONEY} Charged: {P_INR}{unit * len(phones_ok)}"
        )
        if refund_amt:
            caption += f"\n♻️ Refunded (missing files): {P_INR}{refund_amt}"
        caption += "</blockquote>\n\n<blockquote>ZIP contains one <code>.session</code> per account.</blockquote>"

        await bot.send_file(
            uid,
            zip_path,
            caption=caption,
            force_document=True,
        )
        try:
            await event.edit(
                f"{P_YES} <b>ZIP sent!</b> Check the document above.\n"
                f"Delivered: {len(phones_ok)}/{qty}"
            )
        except Exception:
            pass

    except Exception as e:
        logger.exception("Bulk ZIP error: %s", e)
        async with get_user_lock(uid):
            users_col.update_one({"user_id": uid}, {"$inc": {"balance": total}})
            for d in claimed:
                stock_col.update_one({"phone": d["phone"]}, {"$set": {"available": 1}})
        try:
            await event.edit(f"{P_NO} <b>Bulk order failed.</b> Full refund issued.\n<code>{e}</code>")
        except Exception:
            pass
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
# Handlers registration
# ─────────────────────────────────────────────

def register_buy(bot):
    @bot.on(events.CallbackQuery(pattern=rb"^noop$"))
    async def cb_noop(e):
        try:
            await e.answer("Select a row below", alert=False)
        except Exception:
            pass

    @bot.on(events.NewMessage(pattern=r"(?i)^(🛒 𝐁ᴜʏ 𝐀ᴄᴄᴏᴜɴᴛ|🛒 Buy Account)$"))
    async def msg_buy_single(e):
        await show_countries(e, "single", 1)

    @bot.on(events.NewMessage(pattern=r"(?i)^(📁 𝐁ᴜʏ 𝐒ᴇssɪᴏɴs|📁 Buy Sessions|📦 Bulk Sessions)$"))
    async def msg_buy_bulk(e):
        await show_countries(e, "bulk", 1)

    @bot.on(events.CallbackQuery(pattern=r"^bc\|(.+)\|(.+)$"))
    async def cb_bc(e):
        p = e.pattern_match
        mode = p.group(1).decode() if isinstance(p.group(1), bytes) else p.group(1)
        country = p.group(2).decode() if isinstance(p.group(2), bytes) else p.group(2)
        await show_years(e, mode, country)

    @bot.on(events.CallbackQuery(pattern=r"^pg_c\|(.+)\|(\d+)$"))
    async def cb_pg_c(e):
        p = e.pattern_match
        mode = p.group(1).decode() if isinstance(p.group(1), bytes) else p.group(1)
        page = int(p.group(2).decode() if isinstance(p.group(2), bytes) else p.group(2))
        await show_countries(e, mode, page)

    @bot.on(events.CallbackQuery(pattern=r"^by\|(.+)\|(.+)\|(\d+)\|(\d+)$"))
    async def cb_by(e):
        p = e.pattern_match
        mode = p.group(1).decode() if isinstance(p.group(1), bytes) else p.group(1)
        country = p.group(2).decode() if isinstance(p.group(2), bytes) else p.group(2)
        year = p.group(3).decode() if isinstance(p.group(3), bytes) else p.group(3)
        price = p.group(4).decode() if isinstance(p.group(4), bytes) else p.group(4)

        if mode == "bulk":
            # count available for this slice
            avail = stock_col.count_documents({
                "country_name": country,
                "account_year": int(year),
                "price": int(price),
                "available": 1,
            })
            await show_bulk_qty(e, country, year, price, avail)
        else:
            await confirm_purchase(e, country, year, price)

    @bot.on(events.CallbackQuery(pattern=r"^bulk_qty\|(.+)\|(\d+)\|(\d+)\|(\d+)$"))
    async def cb_bulk_qty(e):
        p = e.pattern_match
        country = p.group(1).decode() if isinstance(p.group(1), bytes) else p.group(1)
        year = p.group(2).decode() if isinstance(p.group(2), bytes) else p.group(2)
        price = p.group(3).decode() if isinstance(p.group(3), bytes) else p.group(3)
        qty = p.group(4).decode() if isinstance(p.group(4), bytes) else p.group(4)
        await confirm_bulk(e, country, year, price, qty)

    @bot.on(events.CallbackQuery(pattern=r"^bulk_custom\|(.+)\|(\d+)\|(\d+)\|(\d+)$"))
    async def cb_bulk_custom(e):
        p = e.pattern_match
        country = p.group(1).decode() if isinstance(p.group(1), bytes) else p.group(1)
        year = p.group(2).decode() if isinstance(p.group(2), bytes) else p.group(2)
        price = p.group(3).decode() if isinstance(p.group(3), bytes) else p.group(3)
        max_qty = int(p.group(4).decode() if isinstance(p.group(4), bytes) else p.group(4))
        uid = e.sender_id
        session_buy_state[uid] = {
            "step": "bulk_qty",
            "country": country,
            "year": year,
            "price": price,
            "max_qty": max_qty,
        }
        await e.edit(
            f"{P_MONEY} <b>Enter quantity</b> (1–{max_qty}):\n"
            f"<i>Reply with a number</i>",
            buttons=[[Button.inline("❌ Cancel", "cancel_action")]],
        )

    @bot.on(
        events.NewMessage(
            func=lambda e: e.sender_id in session_buy_state
            and session_buy_state.get(e.sender_id, {}).get("step") == "bulk_qty"
        )
    )
    async def msg_bulk_custom_qty(e):
        uid = e.sender_id
        st = session_buy_state.pop(uid, None)
        if not st:
            return
        try:
            qty = int(re.sub(r"[^\d]", "", e.text or ""))
        except Exception:
            return await e.reply(f"{P_NO} Invalid number.")
        max_qty = st.get("max_qty", 20)
        if qty < 1 or qty > max_qty:
            return await e.reply(f"{P_WARN} Quantity must be between 1 and {max_qty}.")
        # Fake a callback-style confirm
        class FakeEvent:
            sender_id = uid
            chat_id = e.chat_id

            async def edit(self, text, buttons=None):
                await bot.send_message(uid, text, buttons=buttons)

            async def answer(self, *a, **k):
                pass

        await confirm_bulk(
            FakeEvent(), st["country"], st["year"], st["price"], qty
        )

    @bot.on(events.CallbackQuery(pattern=r"^buy_cf\|(.+)\|(\d+)\|(\d+)$"))
    async def cb_buy_cf(e):
        p = e.pattern_match
        country = p.group(1).decode() if isinstance(p.group(1), bytes) else p.group(1)
        year = p.group(2).decode() if isinstance(p.group(2), bytes) else p.group(2)
        price = p.group(3).decode() if isinstance(p.group(3), bytes) else p.group(3)
        await process_purchase(e, country, year, price)

    @bot.on(events.CallbackQuery(pattern=r"^bulk_cf\|(.+)\|(\d+)\|(\d+)\|(\d+)$"))
    async def cb_bulk_cf(e):
        p = e.pattern_match
        country = p.group(1).decode() if isinstance(p.group(1), bytes) else p.group(1)
        year = p.group(2).decode() if isinstance(p.group(2), bytes) else p.group(2)
        price = p.group(3).decode() if isinstance(p.group(3), bytes) else p.group(3)
        qty = p.group(4).decode() if isinstance(p.group(4), bytes) else p.group(4)
        await process_bulk_purchase(e, country, year, price, qty)

    @bot.on(events.CallbackQuery(pattern=r"^get_otp_again\|(.+)$"))
    async def cb_get_otp_again(e):
        phone = e.pattern_match.group(1).decode()
        if phone not in active_orders:
            return await e.answer("⚠️ Session expired.", alert=True)
        await e.answer("🔄 Fetching latest OTP...")
        order = active_orders[phone]
        client = order["client"]
        uid = order["uid"]
        msg_id = order["msg_id"]
        try:
            try:
                peer = await client.get_input_entity(777000)
            except Exception:
                peer = types.InputPeerUser(user_id=777000, access_hash=0)
            msgs = await client.get_messages(peer, limit=5)
            code = None
            for m in msgs:
                if m.date.timestamp() > order["start_time"] - 10:
                    if (
                        m.message
                        and re.search(OTP_REGEX, m.message)
                        and "Login detected" not in m.message
                    ):
                        code = re.search(OTP_REGEX, m.message).group()
                        break
            if code:
                if not order["paid"]:
                    order["paid"] = True
                    async with get_user_lock(uid):
                        oid = mongo_db.next_id("orders")
                        orders_col.insert_one({
                            "id": oid,
                            "user_id": uid,
                            "country": order["country"],
                            "year": order["year"],
                            "price": order["price"],
                            "phone": phone,
                            "otp": code,
                            "mode": "single",
                            "date": __import__("datetime")
                            .datetime.utcnow()
                            .strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        stock_doc = stock_col.find_one({"phone": phone})
                        if stock_doc and stock_doc.get("gridfs_id"):
                            delete_session_file(stock_doc["gridfs_id"])
                        stock_col.delete_one({"phone": phone})
                twofa_text = (
                    f"{P_2FA} <b>2FA:</b> <code>{order['twofa']}</code>"
                    if order["twofa"] != "None"
                    else "🔓 <b>2FA:</b> <code>Disabled (No Password)</code>"
                )
                msg_text = (
                    f"<blockquote>{PE_CHECK} <b>𝐋ᴀᴛᴇsᴛ 𝐎𝐓𝐏 𝐅ᴇᴛᴄʜᴇᴅ!</b>\n\n"
                    f"{P_PHONE} <b>𝐏ʜᴏɴᴇ:</b> <code>{phone}</code>\n"
                    f"{P_FLAG} <b>𝐂ᴏᴜɴᴛʀʏ:</b> {order['c_icon']} {order['country']}\n"
                    f"{P_OTP} <b>𝐎𝐓𝐏:</b> <code><tg-spoiler>{code}</tg-spoiler></code>\n"
                    f"{twofa_text}</blockquote>"
                )
                try:
                    await bot.edit_message(
                        uid,
                        msg_id,
                        msg_text,
                        buttons=[
                            [Button.inline("🔄 𝐆ᴇᴛ 𝐎𝐓𝐏 𝐀ɢᴀɪɴ", f"get_otp_again|{phone}")],
                            [
                                style_btn(
                                    "🚪 𝐅ɪɴɪsʜ & 𝐋ᴏɢᴏᴜᴛ",
                                    f"logout_bot|{phone}",
                                    "danger",
                                    icon=6129627894349045589,
                                )
                            ],
                        ],
                    )
                except MessageNotModifiedError:
                    pass
            else:
                await e.answer(
                    "⚠️ No new OTP found yet. Try again in a few seconds.", alert=True
                )
        except Exception as ex:
            logger.error(f"Manual OTP fetch error for {phone}: {ex}")
            await e.answer("❌ Error fetching OTP. Check logs.", alert=True)

    @bot.on(events.CallbackQuery(pattern=r"^logout_bot\|(.+)$"))
    async def cb_logout_bot(e):
        phone = e.pattern_match.group(1).decode()
        if phone in active_orders:
            order = active_orders.pop(phone)
            try:
                await order["client"].log_out()
            except Exception:
                pass
            try:
                await order["client"].disconnect()
            except Exception:
                pass
            if order.get("tmp_from_gridfs"):
                cleanup_temp_session(order.get("sess"))
            else:
                sess = order.get("sess")
                if sess:
                    for ext in [
                        ".session",
                        ".session-wal",
                        ".session-shm",
                        ".session-journal",
                    ]:
                        p = sess + ext
                        if os.path.exists(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
            await e.edit(f"{P_YES} <b>Session Finished & Logged out successfully.</b>")
        else:
            await e.answer(
                "⚠️ No active order found or already logged out.", alert=True
            )
