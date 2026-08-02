import os
import re
import html
import urllib.parse
import io
import aiohttp
from telethon import events, Button
from telethon.errors import MessageNotModifiedError
from database import users_col, deposits_col, upi_orders_col, custom_payments_col, settings_col, get_usdt_rate, update_balance, to_usd, db as mongo_db
from config import (
    PE_GIFT, PE_LIGHTNING, P_MONEY, P_CARD, P_UPI, P_CW, P_NO, P_YES, P_WARN,
    P_INR, P_USDT, P_KEY, PE_CHECK, P_ACC, P_ID, LOG_CHANNEL_ID, LOG_CHANNELS,
    ADMIN_ID, CWALLET_QR, CWALLET_ID, UPI_ID, UPI_ID_MANUAL, UPI_ID_AUTO,
    FAMPAY_QR_URL, FAMPAY_VERIFY_URL, FAMPAY_API_KEY, bot, logger
)
from utils.keyboards import style_btn
from utils.states import deposit_input, waiting_proof, admin_dep_state, custom_dep_amt, get_user_lock


# ============================================================
# AUTO UPI VERIFICATION (FamPay-style API)
# ============================================================
async def generate_auto_upi_qr(upi_id: str, amount: float) -> dict:
    """Generate QR + order_id via external payment API."""
    if not FAMPAY_QR_URL:
        return {"success": False, "error": "FAMPAY_QR_URL not configured"}
    try:
        url = f"{FAMPAY_QR_URL}?upi={upi_id}&amount={amount}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("status") == "success":
                        qr_data = data.get("data", {})
                        return {
                            "success": True,
                            "order_id": qr_data.get("order_id"),
                            "qr_url": qr_data.get("qr_url"),
                            "upi_id": qr_data.get("upi_id", upi_id),
                            "amount": qr_data.get("amount", amount),
                            "expires_at": qr_data.get("expires_at_ist", ""),
                        }
                    return {"success": False, "error": data.get("message", "QR generation failed")}
                return {"success": False, "error": f"API Error: {response.status}"}
    except Exception as e:
        logger.error(f"Auto UPI QR error: {e}")
        return {"success": False, "error": str(e)}


async def verify_auto_upi_payment(order_id: str) -> dict:
    """Verify payment status via external payment API."""
    if not FAMPAY_VERIFY_URL:
        return {"verified": False, "message": "FAMPAY_VERIFY_URL not configured"}
    try:
        url = f"{FAMPAY_VERIFY_URL}?order_id={order_id}&api_key={FAMPAY_API_KEY}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("status") == "success":
                        payment = data.get("data", {})
                        return {
                            "verified": True,
                            "amount": float(payment.get("amount", 0)),
                            "transaction_id": payment.get("transaction_id", ""),
                            "utr": payment.get("utr", ""),
                            "sender_name": payment.get("sender_name", ""),
                            "payment_time": payment.get("payment_time_ist", ""),
                            "message": "Payment verified"
                        }
                    return {"verified": False, "message": data.get("message", "Payment not received")}
                return {"verified": False, "message": f"API Error: {response.status}"}
    except Exception as e:
        logger.error(f"Auto UPI verify error: {e}")
        return {"verified": False, "message": str(e)}


async def deposit_menu(event):
    btns = [[style_btn(f"𝐀ᴅᴅ 𝐅ᴜɴᴅs by UPI", "depm_UPI", "success", icon=5409271925014801629)],
            [style_btn(f"𝐂ᴡᴀʟʟᴇᴛ (5% 𝐁𝐎𝐍𝐔𝐒)", "depm_Cwallet", "primary", icon=5440627033111557670)]]
    
    customs = list(custom_payments_col.find({}, {"name": 1}))
    for c in customs:
        btns.append([style_btn(f"{c['name']}", f"depm_{c['name']}", "primary", icon=5408832111773757273)])
        
    msg = f"<blockquote>{PE_GIFT} <b>𝐀ᴅᴅ 𝐅ᴜɴᴅs</b>\n\n𝐂ʜᴏᴏsᴇ ʏᴏᴜʀ ᴘʀᴇғᴇʀʀᴇᴅ ᴘᴀʏᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅ ʙᴇʟᴏᴡ:"
    if isinstance(event, events.CallbackQuery.Event):
        try: await event.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass
    else: await event.respond(msg, buttons=btns)


async def upi_mode_menu(event):
    """UPI → Automatic | Manual submenu"""
    btns = [
        [style_btn("⚡ Automatic (Instant Verify)", "depm_UPI_AUTO", "success", icon=5409271925014801629)],
        [style_btn("📷 Manual (Admin Approve)", "depm_UPI_MANUAL", "primary", icon=5409098988156629257)],
        [style_btn("🔙 Back", "depm_BACK", "danger", icon=6129888444245089008)],
    ]
    msg = (
        f"<blockquote>{P_UPI} <b>UPI Deposit</b></blockquote>\n\n"
        f"<blockquote>⚡ <b>Automatic</b> — Pay → tap Verify → balance auto credit\n"
        f"📷 <b>Manual</b> — Pay → send screenshot → admin approve</blockquote>"
    )
    try:
        await event.edit(msg, buttons=btns)
    except MessageNotModifiedError:
        pass


async def manual_deposit_init(event, method):
    uid = event.sender_id
    deposit_input[uid] = {'step': 'wait_amt', 'method': method}
    label = {
        "UPI_AUTO": "UPI Automatic",
        "UPI_MANUAL": "UPI Manual",
        "Cwallet": "Cwallet",
    }.get(method, method)
    await event.edit(
        f"{P_MONEY} <b>𝐄ɴᴛᴇʀ 𝐃ᴇᴘᴏsɪᴛ 𝐀ᴍᴏᴜɴᴛ (ɪɴ {P_INR})</b>\n"
        f"<i>Method: {label}</i>\n\n"
        f"<i>𝐌ɪɴɪᴍᴜᴍ ᴅᴇᴘᴏsɪᴛ ɪs {P_INR}10.</i>",
        buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]]
    )

async def process_referral_bonus(user_id, amt):
    try:
        row = users_col.find_one({"user_id": user_id}, {"referred_by": 1})
        if not row or not row.get("referred_by"): return
        ref_id = row["referred_by"]
        
        pct_row = settings_col.find_one({"key": "ref_percent"})
        pct = int(pct_row["value"]) if pct_row else 3
        
        bonus = int(amt * (pct / 100))
        if bonus <= 0: return
        
        async with get_user_lock(ref_id):
            update_balance(ref_id, bonus)
            
        try: await bot.send_message(int(ref_id), f"{PE_GIFT} <b>Referral Bonus!</b>\nYour friend deposited {P_INR}{amt}. You received <b>{P_INR}{bonus}</b> ({pct}%) in your balance!")
        except Exception as _e:
            logger.exception("suppressed: %s", _e)
    except Exception as e: logger.error(f"Ref bonus error: {e}")

def get_admin_custom_keypad(dep_id):
    return [
        [style_btn("1", f"dkp|{dep_id}|1", "primary", icon=5375125990118793401), style_btn("2", f"dkp|{dep_id}|2", "primary", icon=5409098988156629257), style_btn("3", f"dkp|{dep_id}|3", "primary", icon=6154249597532248059)],
        [style_btn("4", f"dkp|{dep_id}|4", "primary", icon=5796170975699544141), style_btn("5", f"dkp|{dep_id}|5", "primary", icon=5409320020058584473), style_btn("6", f"dkp|{dep_id}|6", "primary", icon=5409098988156629257)],
        [style_btn("7", f"dkp|{dep_id}|7", "primary", icon=6129779562529168023), style_btn("8", f"dkp|{dep_id}|8", "primary", icon=5355292788923593967), style_btn("9", f"dkp|{dep_id}|9", "primary", icon=5408832111773757273)],
        [style_btn("Del", f"dkp|{dep_id}|del", "danger", icon=6129732880529628243), style_btn("0", f"dkp|{dep_id}|0", "primary", icon=6154249597532248059), style_btn("Confirm", f"dkp|{dep_id}|conf", "success", 5409098988156629257, icon=5409320020058584473)],
        [style_btn("𝐂ᴀɴᴄᴇʟ", f"dkp|{dep_id}|cancel", "danger", icon=6129888444245089008)]
    ]

# We will skip the automated UPI part in this script to save space if needed, 
# or I can port it directly. The user had a keypad logic for UPI amounts.
def get_keypad():
    return [
        [style_btn("1", b"kp_1", style_type="primary", icon=5408832111773757273), style_btn("2", b"kp_2", style_type="primary", icon=5408832111773757273), style_btn("3", b"kp_3", style_type="primary", icon=6129888444245089008)],
        [style_btn("4", b"kp_4", style_type="primary", icon=6064275556008989746), style_btn("5", b"kp_5", style_type="primary", icon=6129627894349045589), style_btn("6", b"kp_6", style_type="primary", icon=5409320020058584473)],
        [style_btn("7", b"kp_7", style_type="primary", icon=5375125990118793401), style_btn("8", b"kp_8", style_type="primary", icon=6129731974291527294), style_btn("9", b"kp_9", style_type="primary", icon=6170048080679801421)],
        [style_btn("Del", b"kp_del", style_type="danger", icon=6203982793379154737), style_btn("0", b"kp_0", style_type="primary", icon=5408832111773757273), style_btn("Confirm", b"kp_done", style_type="success", icon=6064310143380625195)],
        [style_btn("𝐂ᴀɴᴄᴇʟ", b"cancel_action", style_type="danger", icon=5796170975699544141)]
    ]

def register_deposit(bot):
    @bot.on(events.NewMessage(pattern=r"(?i)^(💳 𝐃ᴇᴘᴏsɪᴛ|💳 Deposit)$"))
    async def msg_deposit(e):
        await deposit_menu(e)

    @bot.on(events.CallbackQuery(pattern=r"^depm_(.+)$"))
    async def cb_manual_dep(e):
        method = e.pattern_match.group(1)
        if isinstance(method, (bytes, bytearray)):
            method = method.decode()

        if method == "UPI":
            return await upi_mode_menu(e)
        if method == "BACK":
            return await deposit_menu(e)

        await manual_deposit_init(e, method)

    # ---------- AUTO UPI VERIFY BUTTON ----------
    @bot.on(events.CallbackQuery(pattern=rb"^auto_verify\|(.+)$"))
    async def cb_auto_verify(e):
        uid = e.sender_id
        try:
            g = e.pattern_match.group(1)
            order_id = g.decode() if isinstance(g, (bytes, bytearray)) else str(g)
        except Exception:
            order_id = e.data.decode().split("|", 1)[-1] if e.data else ""

        logger.info(f"AUTO VERIFY clicked by {uid} order={order_id}")

        row = upi_orders_col.find_one({"order_id": order_id})
        if not row:
            return await e.answer("❌ Order not found. Generate a new QR.", alert=True)
        owner_id, amount, status = row["user_id"], row["amount"], row["status"]
        if int(owner_id) != int(uid):
            return await e.answer("⛔ This is not your order.", alert=True)
        if status == "verified":
            return await e.answer("✅ Already verified & credited.", alert=True)

        # Answer ONCE only (Telegram allows a single answer per callback)
        try:
            await e.answer("⏳ Checking payment...", alert=False)
        except Exception:
            pass

        try:
            result = await verify_auto_upi_payment(order_id)
        except Exception as ex:
            logger.error(f"verify_auto_upi_payment crashed: {ex}")
            result = {"verified": False, "message": f"API error: {ex}"}

        if result.get("verified"):
            credited = int(float(result.get("amount") or amount))
            async with get_user_lock(uid):
                from pymongo import ReturnDocument
                claimed = upi_orders_col.find_one_and_update(
                    {"order_id": order_id, "status": "pending"},
                    {"$set": {"status": "verified"}},
                )
                if not claimed:
                    try:
                        await bot.send_message(uid, "✅ Already verified & credited.")
                    except Exception:
                        pass
                    return

                prev_row = users_col.find_one_and_update(
                    {"user_id": uid},
                    {"$inc": {"balance": credited, "total_deposited": credited}},
                    return_document=ReturnDocument.BEFORE,
                )
                prev_bal = (prev_row or {}).get("balance", 0)
                dep_id = mongo_db.next_id("deposits")
                deposits_col.insert_one({
                    "id": dep_id, "user_id": uid, "amount": credited,
                    "method_name": "UPI-Auto", "status": "approved",
                    "date": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                })

            await process_referral_bonus(uid, credited)

            user_msg = (
                f"<blockquote>{PE_CHECK} <b>Payment Verified!</b>\n\n"
                f"{P_MONEY} <b>Amount Added:</b> {P_INR}{credited}\n"
                f"📉 <b>Previous:</b> {P_INR}{prev_bal}\n"
                f"📈 <b>New Balance:</b> {P_INR}{prev_bal + credited}\n"
                f"🧾 <b>UTR:</b> <code>{result.get('utr') or 'N/A'}</code></blockquote>"
            )
            try:
                await e.edit(user_msg, buttons=None)
            except Exception as edit_err:
                logger.warning(f"edit failed: {edit_err}")
                try:
                    await bot.send_message(uid, user_msg)
                except Exception:
                    pass

            log_msg = (
                f"{PE_CHECK} <b>AUTO UPI VERIFIED</b>\n"
                f"{P_ACC} User: <code>{uid}</code>\n"
                f"{P_MONEY} Amount: <b>{P_INR}{credited}</b>\n"
                f"🧾 Order: <code>{order_id}</code>\n"
                f"UTR: <code>{result.get('utr') or 'N/A'}</code>"
            )
            for log_ch in LOG_CHANNELS:
                try:
                    await bot.send_message(log_ch, log_msg)
                except Exception:
                    pass
        else:
            # Cannot answer() again — send visible message instead
            fail_msg = result.get("message") or "Payment not received yet. Pay first, then try again."
            logger.info(f"AUTO VERIFY failed for {order_id}: {fail_msg}")
            try:
                await bot.send_message(
                    uid,
                    f"❌ <b>Payment not verified</b>\n\n"
                    f"{fail_msg}\n\n"
                    f"🧾 Order: <code>{order_id}</code>\n"
                    f"💡 Pay exact amount, wait 10–20 sec, then tap Verify again.",
                    buttons=[[style_btn("✅ 𝐓ʀʏ 𝐀ɢᴀɪɴ", f"auto_verify|{order_id}", "success", icon=5409098988156629257)]]
                )
            except Exception as send_err:
                logger.error(f"Failed to notify user of verify fail: {send_err}")

    @bot.on(events.NewMessage(func=lambda e: e.sender_id in deposit_input and deposit_input[e.sender_id]['step'] == 'wait_amt'))
    async def msg_wait_amt(e):
        uid = e.sender_id
        text = e.text or ""
        try:
            amt = int(re.sub(r'[^\d]', '', text))
            if amt < 10: return await e.reply(f"{P_WARN} Minimum Deposit is ₹10.")
            method = deposit_input[uid]['method']
            deposit_input.pop(uid)
            
            rate = get_usdt_rate()
            usdt_amt = round(amt / rate, 2)
            rate_text = f"<blockquote>{P_MONEY} <b>𝐀ᴍᴏᴜɴᴛ ᴛᴏ 𝐏ᴀʏ:</b> {P_INR}{amt} (~{P_USDT}{usdt_amt} USDT)\n💱 <i>𝐄xᴄʜᴀɴɢᴇ 𝐑ᴀᴛᴇ: {P_INR}{rate} = $1</i></blockquote>"
            
            if method == "Cwallet":
                waiting_proof[uid] = {'amount': amt, 'method': method}
                msg = (f"<blockquote>{P_CARD} <b>𝐌ᴇᴛʜᴏᴅ:</b> {method}\n\n🚀 <b>𝐀ᴅᴅʀᴇss / 𝐈𝐃:</b>\n<code>{CWALLET_ID}</code></blockquote>\n"
                       f"{rate_text}\n"
                       f"<blockquote>👉 <b>𝐒ᴇɴᴅ 𝐏ʀᴏᴏғ:</b>\n𝐏ʟᴇᴀsᴇ sᴇɴᴅ ᴛʜᴇ 𝐓ʀᴀɴsᴀᴄᴛɪᴏɴ 𝐇ᴀsʜ (𝐋ɪɴᴋ) ᴏʀ ᴀ 𝐒ᴄʀᴇᴇɴsʜᴏᴛ ᴏғ ᴛʜᴇ ᴘᴀʏᴍᴇɴᴛ ɴᴏᴡ.</blockquote>")
                try: await bot.send_file(uid, CWALLET_QR, caption=msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                except Exception as _e:
                    logger.exception("Cwallet QR send failed: %s", _e)
                    await bot.send_message(uid, msg + f"\n\n🔗 QR Link: {CWALLET_QR}", buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
            elif method == "UPI_AUTO":
                # ---------- AUTOMATIC UPI (API QR + Verify) ----------
                auto_upi = UPI_ID_AUTO or UPI_ID
                if not auto_upi:
                    return await e.reply(f"{P_WARN} UPI_ID_AUTO not set in .env")
                if not (FAMPAY_QR_URL and FAMPAY_VERIFY_URL):
                    return await e.reply(f"{P_WARN} Auto UPI API not configured (FAMPAY_QR_URL / FAMPAY_VERIFY_URL).")

                qr_result = await generate_auto_upi_qr(auto_upi, amt)
                if not qr_result.get("success"):
                    return await e.reply(
                        f"{P_NO} <b>QR generation failed</b>\n{qr_result.get('error', 'Unknown error')}\n\n"
                        f"Try <b>Manual UPI</b> or contact admin."
                    )

                order_id = str(qr_result["order_id"])
                upi_orders_col.update_one(
                    {"order_id": order_id},
                    {"$set": {
                        "order_id": order_id, "user_id": uid, "amount": amt, "status": "pending",
                        "date": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    }},
                    upsert=True,
                )
                expires = qr_result.get("expires_at") or "15 min"
                display_upi = qr_result.get("upi_id") or auto_upi
                msg = (
                    f"<blockquote>{P_UPI} <b>𝐌ᴇᴛʜᴏᴅ:</b> UPI Automatic</blockquote>\n"
                    f"{rate_text}\n"
                    f"<blockquote>🆔 <b>UPI ID:</b> <code>{display_upi}</code>\n"
                    f"🧾 <b>Order ID:</b> <code>{order_id}</code>\n"
                    f"⏰ <b>Expires:</b> {expires}</blockquote>\n\n"
                    f"<blockquote>👉 Exact amount pay karo, phir <b>✅ Verify Payment</b> dabao.\n"
                    f"Balance auto credit ho jayega.</blockquote>"
                )
                verify_btns = [
                    [style_btn("✅ 𝐕ᴇʀɪғʏ 𝐏ᴀʏᴍᴇɴᴛ", f"auto_verify|{order_id}", "success", icon=5409098988156629257)],
                    [Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]
                ]
                if qr_result.get("qr_url"):
                    try:
                        await bot.send_file(uid, qr_result["qr_url"], caption=msg, buttons=verify_btns)
                    except Exception:
                        await bot.send_message(uid, msg + f"\n\n🔗 QR: {qr_result['qr_url']}", buttons=verify_btns)
                else:
                    try:
                        import qrcode
                        upi_url = f"upi://pay?pa={auto_upi}&am={amt}&tn={order_id}"
                        qr = qrcode.QRCode(version=1, box_size=10, border=4)
                        qr.add_data(upi_url)
                        qr.make(fit=True)
                        img = qr.make_image(fill_color="black", back_color="white")
                        qr_file = io.BytesIO()
                        qr_file.name = "upi_auto_qr.png"
                        img.save(qr_file, "PNG")
                        qr_file.seek(0)
                        await bot.send_file(uid, qr_file, caption=msg, buttons=verify_btns)
                    except Exception as ex:
                        logger.error(f"Local auto QR fail: {ex}")
                        await bot.send_message(uid, msg, buttons=verify_btns)

            elif method == "UPI_MANUAL":
                # ---------- MANUAL UPI (QR + screenshot + admin) ----------
                manual_upi = UPI_ID_MANUAL or UPI_ID
                if not manual_upi:
                    return await e.reply(f"{P_WARN} UPI_ID_MANUAL not set in .env")

                waiting_proof[uid] = {'amount': amt, 'method': "UPI_MANUAL"}
                upi_url = f"upi://pay?pa={manual_upi}&am={amt}"
                msg = (
                    f"<blockquote>{P_UPI} <b>𝐌ᴇᴛʜᴏᴅ:</b> UPI Manual</blockquote>\n"
                    f"{rate_text}\n"
                    f"<blockquote>🆔 <b>UPI ID:</b>\n<code>{manual_upi}</code></blockquote>\n"
                    f"<blockquote>👉 Pay karke clear <b>Screenshot</b> bhejo.\n"
                    f"Admin approve karega tab balance add hoga.</blockquote>"
                )
                try:
                    import qrcode
                    qr = qrcode.QRCode(version=1, box_size=10, border=4)
                    qr.add_data(upi_url)
                    qr.make(fit=True)
                    img = qr.make_image(fill_color="black", back_color="white")
                    qr_file = io.BytesIO()
                    qr_file.name = "upi_manual_qr.png"
                    img.save(qr_file, "PNG")
                    qr_file.seek(0)
                    await bot.send_file(uid, qr_file, caption=msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
                except Exception as ex:
                    logger.error(f"Manual UPI QR fail: {ex}")
                    await bot.send_message(uid, msg, buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])

            else:
                waiting_proof[uid] = {'amount': amt, 'method': method}
                row = custom_payments_col.find_one({"name": method})
                if row:
                    cap = f"<blockquote>{row.get('caption','')}</blockquote>\n{rate_text}\n<blockquote>👇 <b>𝐀ғᴛᴇʀ ᴘᴀʏɪɴɢ, sᴇɴᴅ ᴀ ᴄʟᴇᴀʀ 𝐒ᴄʀᴇᴇɴsʜᴏᴛ ʜᴇʀᴇ:</b></blockquote>"
                    btns = [[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]]
                    if row.get("qr_file_id") and os.path.exists(row["qr_file_id"]): 
                        try:
                            await bot.send_file(e.chat_id, row["qr_file_id"], caption=cap, buttons=btns)
                        except Exception as _e:
                            logger.exception("custom payment file send failed: %s", _e)
                            await e.reply(cap, buttons=btns)
                    else: await e.reply(cap, buttons=btns)
                else: await e.reply(f"{P_CARD} <b>{method} Deposit</b>{rate_text}\n\n👇 Send Screenshot here:", buttons=[[Button.inline("❌ 𝐂ᴀɴᴄᴇʟ", "cancel_action")]])
        except ValueError: await e.respond(f"{P_NO} Please enter a valid number in {P_INR} (INR).")

    @bot.on(events.NewMessage(func=lambda e: e.sender_id in waiting_proof and (e.photo or (e.text and "http" in e.text))))
    async def msg_wait_proof(e):
        uid = e.sender_id
        info = waiting_proof.pop(uid)
        final_amt = info['amount']
        if info['method'] == "Cwallet": final_amt = int(final_amt * 1.05)
        
        dep_id = mongo_db.next_id("deposits")
        deposits_col.insert_one({
            "id": dep_id, "user_id": uid, "amount": final_amt,
            "method_name": info["method"], "status": "pending",
            "date": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        })
        await e.reply(f"{PE_GIFT} 𝐃ᴇᴘᴏsɪᴛ ʀᴇǫᴜᴇsᴛ sᴜʙᴍɪᴛᴛᴇᴅ! 𝐏ʟᴇᴀsᴇ ᴡᴀɪᴛ ғᴏʀ ᴀᴅᴍɪɴ ᴀᴘᴘʀᴏᴠᴀʟ.")
        cap = f"{PE_LIGHTNING} <b>𝐍ᴇᴡ 𝐃ᴇᴘᴏsɪᴛ 𝐑ᴇǫᴜᴇsᴛ</b>\n{P_ACC} 𝐔sᴇʀ: <code>{uid}</code>\n{P_MONEY} 𝐑ᴇǫᴜᴇsᴛ: <b>{P_INR}{info['amount']}</b>\n{P_CARD} 𝐌ᴇᴛʜᴏᴅ: {info['method']}\n{P_ID} 𝐑ᴇғ: <code>{dep_id}</code>"
        btns = [[style_btn(f"𝐀ᴄᴄᴇᴘᴛ (₹{final_amt})", f"dep_acc|{dep_id}|{uid}|{info['method']}|exact|{final_amt}", "success", icon=5409098988156629257), 
                 style_btn("𝐑ᴇᴊᴇᴄᴛ", f"dep_rej|{dep_id}|{uid}", "danger", icon=5409119256107297715)],
                [style_btn("𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ", f"dep_acc|{dep_id}|{uid}|{info['method']}|custom|0", "primary", icon=5409098988156629257)]]
        
        try:
            for log_ch in LOG_CHANNELS:
                try:
                    if e.photo: await bot.send_message(log_ch, cap, file=e.media, buttons=btns)
                    else: await bot.send_message(log_ch, cap + f"\n🔗 Hash: {html.escape(e.text)}", buttons=btns)
                except Exception as _e:
                    logger.exception("suppressed: %s", _e)
        except Exception as log_err:
            try:
                if e.photo: await bot.send_message(ADMIN_ID, f"⚠️ <b>LOG CHANNEL ERROR</b>\n\n{cap}", file=e.media, buttons=btns)
                else: await bot.send_message(ADMIN_ID, f"⚠️ <b>LOG CHANNEL ERROR</b>\n\n{cap}\n🔗 Hash: {html.escape(e.text)}", buttons=btns)
            except Exception as admin_err: logger.error(f"Failed to log deposit: {admin_err}")

    @bot.on(events.CallbackQuery(pattern=r"^dep_acc\|"))
    async def cb_dep_acc(e):
        p = e.data.decode().split("|")
        dep_id, t_uid, method, a_type = p[1], int(p[2]), p[3], p[4]
        # ATOMIC claim of pending deposit (prevents double-approve)
        claimed = deposits_col.find_one_and_update(
            {"id": int(dep_id), "status": "pending"},
            {"$set": {"status": "processing"}},
        )
        if not claimed:
            return await e.edit(f"{P_WARN} Already processed.")
        
        if a_type == "exact":
            amt = int(p[5])
            async with get_user_lock(t_uid):
                from pymongo import ReturnDocument
                prev_row = users_col.find_one_and_update(
                    {"user_id": t_uid},
                    {"$inc": {"balance": amt, "total_deposited": amt}},
                    return_document=ReturnDocument.BEFORE,
                )
                prev_bal = (prev_row or {}).get("balance", 0)
                deposits_col.update_one(
                    {"id": int(dep_id)},
                    {"$set": {"status": "approved", "amount": amt}},
                )
            
            await process_referral_bonus(t_uid, amt)
            
            user_msg = (f"<blockquote>{PE_CHECK} <b>Deposit Approved!</b>\n\n{P_MONEY} <b>Amount Added:</b> ${to_usd(amt):.2f} ({P_INR}{amt})\n"
                        f"📉 <b>𝐏ʀᴇᴠious 𝐁ᴀʟᴀɴᴄᴇ:</b> ${to_usd(prev_bal):.2f} ({P_INR}{prev_bal})\n📈 <b>New 𝐁ᴀʟᴀɴᴄᴇ:</b> ${to_usd(prev_bal+amt):.2f} ({P_INR}{prev_bal+amt})</blockquote>")
            await bot.send_message(int(t_uid), user_msg)
            try: await e.edit(f"{PE_CHECK} <b>INSTANT CREDITED {P_INR}{amt} TO {t_uid}</b>")
            except MessageNotModifiedError: pass
            
        elif a_type == "custom":
            custom_dep_amt[int(dep_id)] = "0"
            await e.edit(f"{P_KEY} <b>Enter 𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ for User {t_uid}:</b>\n\n{P_MONEY} 0", buttons=get_admin_custom_keypad(int(dep_id)))
            
    @bot.on(events.CallbackQuery(pattern=r"^dep_rej\|"))
    async def cb_dep_rej(e):
        uid = e.sender_id
        p = e.data.decode().split("|")
        dep_id, t_uid = p[1], int(p[2])
        claimed = deposits_col.find_one_and_update(
            {"id": int(dep_id), "status": "pending"},
            {"$set": {"status": "rejecting"}},
        )
        if not claimed:
            return await e.edit(f"{P_WARN} Already processed.")
        admin_dep_state[uid] = {"target_uid": t_uid, "dep_id": dep_id, "step": "wait_reason", "msg_id": e.message_id}
        await bot.send_message(uid, f"{P_WARN} Reply to this message with the REASON for rejecting user <code>{t_uid}</code>:")
        try:
            await e.answer("Check your bot PMs to enter the reason.", alert=True)
        except Exception as ex:
            logger.exception("dep_rej answer failed: %s", ex)

    @bot.on(events.NewMessage(func=lambda e: e.sender_id in admin_dep_state and admin_dep_state[e.sender_id]['step'] == 'wait_reason'))
    async def msg_admin_rej_reason(e):
        uid = e.sender_id
        st = admin_dep_state[uid]
        t_uid, dep_id, msg_id = st['target_uid'], st['dep_id'], st['msg_id']
        deposits_col.update_one({"id": int(dep_id)}, {"$set": {"status": "rejected"}})
        
        try:
            await bot.edit_message(LOG_CHANNEL_ID, msg_id, f"{P_NO} <b>REJECTED USER {t_uid}</b>\nReason: {html.escape(e.text)}")
            for log_ch in LOG_CHANNELS:
                if log_ch != LOG_CHANNEL_ID:
                    try:
                        await bot.send_message(log_ch, f"{P_NO} <b>REJECTED USER {t_uid}</b>\nReason: {html.escape(e.text)}")
                    except Exception as _e:
                        logger.exception("reject log channel failed: %s", _e)
        except Exception as _e:
            logger.exception("reject message edit failed: %s", _e)
        
        await bot.send_message(int(t_uid), f"{P_NO} <b>Deposit 𝐑ᴇᴊᴇᴄᴛed!</b>\n📋 Reason: {html.escape(e.text)}")
        await e.reply(f"{P_YES} 𝐑ᴇᴊᴇᴄᴛion reason sent.")
        admin_dep_state.pop(uid)

    @bot.on(events.CallbackQuery(pattern=r"^dkp\|"))
    async def cb_dkp(e):
        uid = e.sender_id
        _, dep_id, action = e.data.decode().split("|")
        dep_id = int(dep_id)
        row = deposits_col.find_one({"id": dep_id})
        if not row or row.get("status") not in ("pending", "processing"):
            return await e.edit(f"{P_WARN} Already processed.")
        t_uid, method, orig_amt = row["user_id"], row["method_name"], row["amount"]
        
        curr = custom_dep_amt.get(dep_id, "0")
        
        if action.isdigit():
            if curr == "0": curr = action
            else: curr += action
            if len(curr) > 7: curr = curr[:7]
        elif action == "del": curr = curr[:-1] or "0"
        elif action == "cancel":
            btns = [[style_btn(f"𝐀ᴄᴄᴇᴘᴛ (₹{orig_amt})", f"dep_acc|{dep_id}|{t_uid}|{method}|exact|{orig_amt}", "success", icon=6147460667281511517), 
                     style_btn("𝐑ᴇᴊᴇᴄᴛ", f"dep_rej|{dep_id}|{t_uid}", "danger", icon=6129888444245089008)],
                    [style_btn("𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ", f"dep_acc|{dep_id}|{t_uid}|{method}|custom|0", "primary", icon=5796170975699544141)]]
            return await e.edit(f"{PE_LIGHTNING} <b>𝐍ᴇᴡ 𝐃ᴇᴘᴏsɪᴛ 𝐑ᴇǫᴜᴇsᴛ</b>\n{P_ACC} 𝐔sᴇʀ: <code>{t_uid}</code>\n{P_MONEY} 𝐑ᴇǫᴜᴇsᴛ: <b>{P_INR}{orig_amt}</b>\n{P_CARD} 𝐌ᴇᴛʜᴏᴅ: {method}\n{P_ID} 𝐑ᴇғ: <code>{dep_id}</code>", buttons=btns)
        elif action == "conf":
            amt = int(curr)
            if amt <= 0: return await e.answer("Amount must be > 0", alert=True)
            
            async with get_user_lock(t_uid):
                from pymongo import ReturnDocument
                # ATOMIC: only approve if still pending/processing
                claimed = deposits_col.find_one_and_update(
                    {"id": dep_id, "status": {"$in": ["pending", "processing"]}},
                    {"$set": {"status": "approved", "amount": amt}},
                )
                if not claimed:
                    return await e.edit(f"{P_WARN} Already processed.")
                prev_row = users_col.find_one_and_update(
                    {"user_id": t_uid},
                    {"$inc": {"balance": amt, "total_deposited": amt}},
                    return_document=ReturnDocument.BEFORE,
                )
                prev_bal = (prev_row or {}).get("balance", 0)
                
            await process_referral_bonus(t_uid, amt)
            await e.edit(f"{PE_CHECK} <b>APPROVED {P_INR}{amt} TO {t_uid} (𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ)</b>")
            await bot.send_message(int(t_uid), f"{PE_CHECK} <b>Deposit Approved!</b>\n{P_MONEY} Amount Added: {P_INR}{amt}\n📉 Old: {P_INR}{prev_bal} | 📈 New: {P_INR}{prev_bal+amt}")
            return

        custom_dep_amt[dep_id] = curr
        await e.edit(f"{P_KEY} <b>Enter 𝐂ᴜsᴛᴏᴍ 𝐀ᴍᴏᴜɴᴛ for User {t_uid}:</b>\n\n{P_MONEY} {curr}", buttons=get_admin_custom_keypad(dep_id))
