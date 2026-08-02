from telethon import events, types
from telethon.errors import MessageNotModifiedError
from database import users_col, settings_col, ensure_user, is_user_banned, is_bot_online, is_admin
from utils.keyboards import get_persistent_menu, get_terms_buttons, get_join_buttons
from utils.helpers import check_channel_joined
from config import PE_FLOWER, PE_LOCATION, P_OFF, PE_HEART, PE_GIFT, P_GIFT, P_GLOBE, P_INR
from utils.states import session_buy_state, deposit_input

async def send_main_menu(bot, event, uid):
    me = await bot.get_me()
    pct_row = settings_col.find_one({"key": "ref_percent"})
    pct = pct_row["value"] if pct_row else "3"
    bal_row = users_col.find_one({"user_id": uid}, {"balance": 1})
    bal = bal_row.get("balance", 0) if bal_row else 0
    bot_username = me.username or ""
    PFP_URL = "assets/image.jpg"
    msg = (f"<blockquote>{PE_HEART} <b>𝐖ᴇʟᴄᴏᴍᴇ ᴛᴏo OTP BAZAAR BOT!</b></blockquote>\n\n"
           f"<blockquote>{PE_GIFT} <b>𝐏ʀᴇᴍɪᴜᴍ sᴇʀᴠɪᴄᴇs:</b> 𝐁ᴜʏ ᴀᴄᴄᴏᴜɴᴛs, sᴇssɪᴏɴs, ᴀɴᴅ ᴛᴏᴘ ᴜᴘ ɪɴsᴛᴀɴᴛʟʏ.</blockquote>\n\n"
           f"<blockquote>{P_GIFT} <b>𝐑ᴇғᴇʀ & 𝐄ᴀʀɴ:</b>\n𝐈ɴᴠɪᴛᴇ ғʀɪᴇɴᴅs ᴀɴᴅ ᴇᴀʀɴ {pct}% ᴏғ ᴛʜᴇɪʀ ᴅᴇᴘᴏsɪᴛs!\n"
           f"{P_GLOBE} <code>https://t.me/{bot_username}?start=ref_{uid}</code></blockquote>\n\n"
           f"<blockquote>💰 <b>𝐁ᴀʟᴀɴᴄᴇ:</b> {P_INR}{bal}</blockquote>")
    
    f = await bot.upload_file(PFP_URL)
    media = types.InputMediaUploadedPhoto(file=f)
    await bot.send_file(uid, media, caption=msg, buttons=get_persistent_menu(uid))

def register_start(bot):
    @bot.on(events.NewMessage(pattern=r"(?i)^(/start|🏠 𝐒ᴛᴀʀᴛ)"))
    async def handle_start(e):
        try:
            uid = e.sender_id
            if not uid: return
            
            is_new = users_col.find_one({"user_id": uid}, {"_id": 1}) is None
            
            ensure_user(uid)
            if is_user_banned(uid): return

            if not is_bot_online() and not is_admin(uid):
                return await e.respond(f"{P_OFF} <b>Bot is currently under maintenance.</b> Please try again later.")
            
            session_buy_state.pop(uid, None)
            deposit_input.pop(uid, None)

            text = e.text or ""
            if len(text.split()) > 1:
                start_param = text.split()[1]
                if start_param.startswith("ref_"):
                    ref = start_param.replace("ref_", "").strip()
                    # Anti-abuse: only first-time users, never self-ref, referrer must exist,
                    # and referred_by is only set when still None (never overwrite).
                    if ref.isdigit():
                        ref_id = int(ref)
                        if ref_id != uid and is_new:
                            referrer = users_col.find_one({"user_id": ref_id}, {"_id": 1})
                            if referrer:
                                users_col.update_one(
                                    {
                                        "user_id": uid,
                                        "$or": [
                                            {"referred_by": None},
                                            {"referred_by": {"$exists": False}},
                                        ],
                                    },
                                    {"$set": {"referred_by": ref_id}},
                                )

            PFP_URL = "assets/image.jpg"
            is_joined = await check_channel_joined(bot, uid, is_admin)
            if not is_joined:
                from utils.helpers import get_unjoined_channels
                from telethon import Button
                from utils.keyboards import style_btn
                
                unjoined = await get_unjoined_channels(bot, uid)
                remaining = len(unjoined)
                msg = f"<blockquote>{PE_FLOWER} <b>𝐘ᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴏᴜʀ ᴄʜᴀɴɴᴇʟs ғɪʀsᴛ!</b></blockquote>\n<blockquote>{PE_LOCATION} {remaining} ᴄʜᴀɴɴᴇʟ(s) ʀᴇᴍᴀɪɴɪɴɢ. 𝐉ᴏɪɴ ᴀɴᴅ ᴛᴀᴘ <b>𝐕ᴇʀɪғʏ 𝐉ᴏɪɴᴇᴅ</b>.</blockquote>"
                
                buttons = [[Button.url(f"📢 Join Channel {idx}", url)] for url, idx in unjoined]
                buttons.append([style_btn("𝐕ᴇʀɪғʏ 𝐉ᴏɪɴᴇᴅ", b"verify_join", "success", icon=6129627894349045589)])
                
                f = await bot.upload_file(PFP_URL)
                media = types.InputMediaUploadedPhoto(file=f, spoiler=True)
                return await bot.send_file(e.chat_id, media, caption=msg, buttons=buttons)

            row = users_col.find_one({"user_id": uid}, {"terms_accepted": 1})
            terms_acc = row.get("terms_accepted", 0) if row else 0
            if not terms_acc:
                msg = f"<blockquote>{PE_FLOWER} <b>𝐓ᴇʀᴍs & 𝐂ᴏɴᴅɪᴛɪᴏɴs</b></blockquote>\n<blockquote>𝐏ʟᴇᴀsᴇ ʀᴇᴀᴅ ᴀɴᴅ ᴀᴄᴄᴇᴘᴛ ᴏᴜʀ 𝐓ᴇʀᴍs & 𝐂ᴏɴᴅɪᴛɪᴏɴs ʙᴇғᴏʀᴇ ᴜsɪɴɢ ᴛʜᴇ ʙᴏᴛ.</blockquote>"
                return await e.respond(msg, buttons=get_terms_buttons())

            await send_main_menu(bot, e, uid)
        except Exception as ex: 
            print(f"Start Error: {ex}")
