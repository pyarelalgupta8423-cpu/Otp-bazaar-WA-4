import logging
logger = logging.getLogger(__name__)
from telethon import events, Button
from telethon.errors import MessageNotModifiedError
from database import users_col, orders_col, to_usd
from config import PE_KISS, PE_CROWN, PE_FLOWER, P_ID, P_MONEY, P_CARD, P_USERS, P_CAL, P_GIFT, P_CART, P_USDT, P_PHONE
from datetime import datetime

async def profile_handler(bot, event):
    uid = event.sender_id
    row = users_col.find_one({"user_id": uid})
    if not row: return await bot.send_message(event.chat_id, "⚠️ Error: Please type /start to initialize your account.")
    
    bal = row.get("balance", 0)
    dep = row.get("total_deposited", 0)
    date = row.get("joined_date", "") or ""
    discount = row.get("discount", 0)
    ref_count = users_col.count_documents({"referred_by": uid})
    me = await bot.get_me()
    bot_username = me.username or ""
    ref_link = f"https://t.me/{bot_username}?start=ref_{uid}" if bot_username else None
    disc_msg = f"\n{P_GIFT} Active Discount: <b>{discount}% OFF</b>" if discount > 0 else ""
    ref_block = (f"{P_USERS} <b>𝐘ᴏᴜʀ 𝐑ᴇғᴇʀʀᴀʟ 𝐋ɪɴᴋ:</b>\n<code>{ref_link}</code>\n\n"
                 if ref_link else
                 f"{P_USERS} <b>Referral Link:</b>\n<i>𝐒ᴇᴛ ᴀ ᴘᴜʙʟɪᴄ ʙᴏᴛ ᴜsᴇʀɴᴀᴍᴇ ᴛᴏ ᴇɴᴀʙʟᴇ ʀᴇғᴇʀʀᴀʟs.</i>\n\n")
    
    msg = (f"<blockquote expandable>{PE_KISS} <b>𝐔sᴇʀ 𝐏ʀᴏғɪʟᴇ</b></blockquote>\n\n"
           f"<blockquote>{P_ID} 𝐔sᴇʀ 𝐈𝐃: <tg-spoiler><code>{uid}</code></tg-spoiler>\n"
           f"{P_MONEY} 𝐁ᴀʟᴀɴᴄᴇ: <tg-spoiler><code>${to_usd(bal):.2f} (₹{bal})</code></tg-spoiler>\n"
           f"{P_CARD} 𝐃ᴇᴘᴏsɪᴛᴇᴅ: <code>${to_usd(dep):.2f} (₹{dep})</code>{disc_msg}\n"
           f"{P_USERS} 𝐑ᴇғᴇʀʀᴇᴅ 𝐔sᴇʀs: <b>{ref_count}</b>\n"
           f"{P_CAL} 𝐉ᴏɪɴᴇᴅ: {date[:10]}</blockquote>\n\n"
           f"<blockquote>{ref_block}</blockquote>\n"
           f"<blockquote><i>(𝐒ʜᴀʀᴇ ᴛʜɪs ʟɪɴᴋ ᴡɪᴛʜ ʏᴏᴜʀ ғʀɪᴇɴᴅs ᴛᴏ ᴇᴀʀɴ ʙᴏɴᴜsᴇs!)</i></blockquote>")
    await bot.send_message(event.chat_id, msg)

async def stats_handler(bot, event, is_callback=False):
    uid = event.sender_id
    row = users_col.find_one({"user_id": uid}, {"total_deposited": 1})
    if not row: return
    dep = row.get("total_deposited", 0)
    pipeline = [{"$match": {"user_id": uid}}, {"$group": {"_id": None, "count": {"$sum": 1}, "spent": {"$sum": "$price"}}}]
    o_agg = list(orders_col.aggregate(pipeline))
    total_orders = o_agg[0]["count"] if o_agg else 0
    spent = o_agg[0]["spent"] if o_agg else 0
    ref_count = users_col.count_documents({"referred_by": uid})
    
    msg = (f"<blockquote>{PE_CROWN} <b>𝐌ʏ 𝐒ᴛᴀᴛɪsᴛɪᴄs</b></blockquote>\n\n"
           f"<blockquote>{P_CART} <b>𝐀ᴄᴄᴏᴜɴᴛs 𝐁ᴏᴜɢʜᴛ:</b> {total_orders}\n"
           f"{P_USERS} <b>𝐑ᴇғᴇʀʀᴀʟs:</b> {ref_count}\n"
           f"{P_MONEY} <b>𝐓ᴏᴛᴀʟ 𝐒ᴘᴇɴᴛ:</b>\n${to_usd(spent):.2f}\n"
           f"{P_CARD} <b>𝐓ᴏᴛᴀʟ 𝐃ᴇᴘᴏsɪᴛᴇᴅ:</b>\n<tg-spoiler>${to_usd(dep):.2f}</tg-spoiler></blockquote>")
    
    btns = [[Button.inline("𝐕ɪᴇᴡ 𝐏ᴜʀᴄʜᴀsᴇ 𝐋ᴏɢs", "page_purchases_1")], [Button.inline("𝐑ᴇғᴇʀʀᴀʟ 𝐋ᴏɢs", "view_referrals")]]
    if is_callback:
        try: await event.edit(msg, buttons=btns)
        except MessageNotModifiedError: pass
    else: await bot.send_message(event.chat_id, msg, buttons=btns)

async def send_purchase_page(event, uid, page):
    limit = 5
    offset = (page - 1) * limit
    total = orders_col.count_documents({"user_id": uid})
    rows = [(r.get("phone"), r.get("date")) for r in orders_col.find({"user_id": uid}).sort("id", -1).skip(offset).limit(limit)]
    
    msg = f"<blockquote>{PE_FLOWER} <b>𝐏ᴜʀᴄʜᴀsᴇ 𝐇ɪsᴛᴏʀʏ</b>\n𝐏ᴀɢᴇ {page}\n\n"
    if not rows: msg += "𝐍ᴏ ᴘᴜʀᴄʜᴀsᴇs ғᴏᴜɴᴅ."
    else:
        for ph, d in rows:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d %H:%M:%S")
                d_str = dt.strftime("%a %b %d %H:%M:%S %Y")
            except Exception as _e:
                logger.exception("suppressed error: %s", _e)
                d_str = d
            msg += f"<blockquote>{P_PHONE} {ph}\n{P_CAL} {d_str}\n────────────────</blockquote>\n"
            
    nav = []
    if page > 1: nav.append(Button.inline("𝐏ʀᴇᴠ", f"page_purchases_{page-1}"))
    nav.append(Button.inline("𝐁ᴀᴄᴋ", "back_to_stats"))
    if offset + limit < total: nav.append(Button.inline("𝐍ᴇxᴛ", f"page_purchases_{page+1}"))
    await event.edit(msg, buttons=[nav] if nav else None)

def register_profile(bot):
    @bot.on(events.NewMessage(pattern=r"(?i)^(👤 𝐏ʀᴏғɪʟᴇ|👤 Profile)$"))
    async def msg_profile(e):
        await profile_handler(bot, e)

    @bot.on(events.NewMessage(pattern=r"(?i)^(My Stats)$"))
    async def msg_stats(e):
        await stats_handler(bot, e)

    @bot.on(events.CallbackQuery(pattern=r"^page_purchases_(\d+)$"))
    async def cb_purchase_page(e):
        page = int(e.pattern_match.group(1).decode())
        await send_purchase_page(e, e.sender_id, page)

    @bot.on(events.CallbackQuery(pattern=r"^back_to_stats$"))
    async def cb_back_to_stats(e):
        await stats_handler(bot, e, is_callback=True)

    @bot.on(events.CallbackQuery(pattern=r"^view_referrals$"))
    async def cb_view_referrals(e):
        refs_count = users_col.count_documents({"referred_by": e.sender_id})
        await e.answer(f"👥 You have referred {refs_count} user(s).", alert=True)
