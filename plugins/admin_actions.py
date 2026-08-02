import logging
logger = logging.getLogger(__name__)
import os
import re
import time
import asyncio
import csv
import zipfile
import shutil
import tempfile
import html
from telethon import events, Button, TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserIsBlockedError, InputUserDeactivatedError
from telethon.tl.functions.account import GetPasswordRequest
from database import (
    users_col, stock_col, settings_col, admins_col, deposits_col, orders_col,
    auto_prices_col, custom_payments_col, custom_countries_col, upi_orders_col,
    wa_services_col, wa_orders_col,
    is_admin, has_perm, ADMIN_ID, get_usdt_rate, COUNTRY_CODES,
    get_flag_by_country_name, get_country_info, update_balance, is_bot_online,
    get_country_description, set_country_description,
    db as mongo_db
)
from config import *
from utils.keyboards import style_btn
from utils.states import admin_state
from plugins.admin import admin_panel_handler

async def detect_account_year(client):
    """Detect account creation year from earliest dialog/message."""
    try:
        me = await client.get_me()
        # Try using the user's own ID creation date approximation
        # Get the earliest message in Saved Messages
        async for msg in client.iter_messages('me', limit=1, reverse=True):
            if msg.date:
                return msg.date.year
        # Fallback: check earliest dialog
        async for dialog in client.iter_dialogs(limit=5):
            if dialog.date:
                return dialog.date.year
    except Exception as _e:
        logger.exception("suppressed error: %s", _e)
    from datetime import datetime
    return datetime.now().year

async def manage_admins_menu(event):
    rows = list(admins_col.find({}, {"user_id": 1}))
    msg = f"{PE_CROWN} <b>Manage Sub-Admins</b>\n\n"
    for r in rows: msg += f"{P_ACC} <code>{r['user_id']}</code>\n"
    btns = [[style_btn("Add Admin", "adm_addadmin", "primary", icon=5409098988156629257), style_btn("Edit Admin", "adm_editadminreq", "primary", icon=5409098988156629257)],
            [style_btn("Back", "adm_adminmain", "danger", icon=6129888444245089008)]]
    await event.edit(msg, buttons=btns)

async def edit_admin_menu(event, target_id):
    row = admins_col.find_one({"user_id": int(target_id)})
    if not row: return await event.answer("Admin not found", alert=True)
    p = ["✅" if row.get(k)==1 else "❌" for k in ("p_add_stock","p_manage_stock","p_stats","p_bal","p_settings")]
    
    btns = [
        [style_btn(f"Add Stock: {p[0]}", f"adm_tglperm|{target_id}|p_add_stock", "primary", icon=5409098988156629257)],
        [style_btn(f"Manage Stock: {p[1]}", f"adm_tglperm|{target_id}|p_manage_stock", "primary", icon=5409098988156629257)],
        [style_btn(f"Stats & Bcast: {p[2]}", f"adm_tglperm|{target_id}|p_stats", "primary", icon=5409098988156629257)],
        [style_btn(f"Bal & Users: {p[3]}", f"adm_tglperm|{target_id}|p_bal", "primary", icon=5409098988156629257)],
        [style_btn(f"Settings: {p[4]}", f"adm_tglperm|{target_id}|p_settings", "primary", icon=5409098988156629257)],
        [style_btn("Remove Admin", f"adm_deladmin|{target_id}", "danger", icon=6129888444245089008)],
        [style_btn("Back", "adm_manageadmins", "danger", icon=6129888444245089008)]
    ]
    await event.edit(f"✏️ <b>Editing Admin:</b> <code>{target_id}</code>", buttons=btns)

async def send_manage_stock_page(event, page):
    limit = 10
    offset = (page - 1) * limit
    rows = [(c,) for c in sorted(stock_col.distinct("country_name"))]
    total = len(rows)
    countries = rows[offset:offset+limit]
    
    btns = []
    for (c,) in countries: 
        flag = get_flag_by_country_name(c)
        btns.append([style_btn(f"{flag} {c}", f"adm_msc|{c}", "primary", icon=5409098988156629257)])
    
    nav = []
    if page > 1: nav.append(style_btn("Prev", f"adm_mspg|{page-1}", "primary", icon=5409098988156629257))
    if offset + limit < total: nav.append(style_btn("Next", f"adm_mspg|{page+1}", "primary", icon=5409098988156629257))
    if nav: btns.append(nav)
    btns.append([style_btn("Back", "adm_adminmain", "danger", icon=6129888444245089008)])
    await event.edit(f"{PE_LOCATION} <b>Manage Stock</b> (Page {page})\nSelect a country to edit its properties:", buttons=btns)

async def send_manage_stock_country(event, c_name):
    years = [(y,) for y in sorted(stock_col.distinct("account_year", {"country_name": c_name}), reverse=True)]
    flag = get_flag_by_country_name(c_name)
    btns = [
        [style_btn("Edit Country Name", f"adm_msedit|name|{c_name}", "primary", icon=5409098988156629257), style_btn("Edit Flag", f"adm_msedit|flag|{c_name}", "primary", icon=5409098988156629257)],
        [style_btn("Edit Common Price (All Years)", f"adm_msedit|cprice|{c_name}", "primary", icon=5409098988156629257)]
    ]
    y_btns = []
    for (y,) in years: y_btns.append(style_btn(f"{y}", f"adm_msedit|yprice|{c_name}|{y}", "primary", icon=5409098988156629257))
    
    for i in range(0, len(y_btns), 3): btns.append(y_btns[i:i+3])
    btns.append([style_btn("Back", "adm_mspg|1", "danger", icon=6129888444245089008)])
    await event.edit(f"{flag} <b>Managing: {c_name}</b>\nSelect an option to edit:", buttons=btns)

async def send_autoprice_page(event, page):
    limit = 10
    offset = (page - 1) * limit
    c_list = set([c[0] for c in COUNTRY_CODES.values()])
    for c in stock_col.distinct("country_name"): c_list.add(c)
    
    for c in custom_countries_col.distinct("name"): c_list.add(c)

    c_list = sorted(list(c_list))
    total = len(c_list)
    countries = c_list[offset:offset+limit]
    
    btns = []
    for c in countries: 
        flag = get_flag_by_country_name(c)
        btns.append([style_btn(f"{flag} {c}", f"adm_apc|{c}", "primary", icon=5409098988156629257)])
        
    nav = []
    if page > 1: nav.append(style_btn("Prev", f"adm_appg|{page-1}", "primary", icon=5409098988156629257))
    if offset + limit < total: nav.append(style_btn("Next", f"adm_appg|{page+1}", "primary", icon=5409098988156629257))
    if nav: btns.append(nav)
    btns.append([style_btn("Add Custom Country", "adm_ap_add_country", "primary", icon=5409098988156629257)])
    btns.append([style_btn("Back", "adm_adminmain", "danger", icon=6129888444245089008)])
    await event.edit(f"{PE_LIGHTNING} <b>Auto Price Setup</b> (Page {page})\nSelect a country to set fixed prices:", buttons=btns)

async def send_autoprice_country(event, c_name):
    flag = get_flag_by_country_name(c_name)
    btns = [[style_btn("Set Common Price", f"adm_apset|{c_name}|Common", "primary", icon=5409098988156629257)]]
    y_btns = []
    for y in range(2024, 1999, -1): y_btns.append(style_btn(f"{y}", f"adm_apset|{c_name}|{y}", "primary", icon=5409098988156629257))
    for i in range(0, len(y_btns), 4): btns.append(y_btns[i:i+4])
    btns.append([style_btn("Back", "adm_appg|1", "danger", icon=6129888444245089008)])
    await event.edit(f"{flag} <b>Auto Price: {c_name}</b>\nSelect 'Common' for default price, or specific years:", buttons=btns)

async def admin_actions(event):
    data_full = event.data.decode()
    if not data_full.startswith("adm_"): return
    uid = event.sender_id
    action_data = data_full[4:]
    chat = event.chat_id
    
    if action_data == "adminmain":
        await event.delete()
        class FakeEvent: chat_id = chat; sender_id = uid
        return await admin_panel_handler(FakeEvent())

    if action_data == "togglebot" and has_perm(uid, 'p_settings'):
        new_status = 'off' if is_bot_online() else 'on'
        settings_col.update_one({"key": "bot_status"}, {"$set": {"value": new_status}}, upsert=True)
        await event.answer(f"Bot turned {new_status.upper()}", alert=True)
        class FakeEvent: chat_id = chat; sender_id = uid
        await admin_panel_handler(FakeEvent())
        await event.delete()
        return

    elif action_data == "stats" and has_perm(uid, 'p_stats'):
        u = users_col.count_documents({})
        s = stock_col.count_documents({"available": 1})
        r_row = settings_col.find_one({"key": "upi_revenue"})
        r = r_row["value"] if r_row else "0"
        bal_agg = list(users_col.aggregate([{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]))
        total_bal = bal_agg[0]["total"] if bal_agg else 0
        o_agg = list(orders_col.aggregate([{"$group": {"_id": None, "count": {"$sum": 1}, "spent": {"$sum": "$price"}}}]))
        total_orders = o_agg[0]["count"] if o_agg else 0
        total_spent = o_agg[0]["spent"] if o_agg else 0
        
        msg = (f"{P_STATS} <b>ADVANCED STATS</b>\n\n{P_USERS} <b>Total Users:</b> {u}\n{P_PKG} <b>Accounts in Stock:</b> {s}\n"
               f"{P_MONEY} <b>Total UPI Revenue:</b> {P_INR}{r}\n\n{P_CARD} <b>Overall Users Balance:</b> {P_INR}{total_bal}\n"
               f"{P_CART} <b>Total Accounts Sold:</b> {total_orders}\n{P_USDT} <b>Overall Sales Amount:</b> {P_INR}{total_spent}")
        return await event.edit(msg, buttons=[[style_btn("Back", "adm_adminmain", "danger", icon=6129888444245089008)]])

    elif action_data == "payments" and has_perm(uid, 'p_settings'):
        btns = [
            [style_btn("Add Payment Method", "adm_addpay", "primary", icon=5409098988156629257)],
            [style_btn("Remove Payment Method", "adm_delpay", "danger", icon=6129888444245089008)],
            [style_btn("Back to Admin", "adm_adminmain", "danger", icon=6129888444245089008)]
        ]
        return await event.edit(f"{P_CARD} <b>Manage Payment Methods</b>", buttons=btns)

    elif action_data == "manageadmins" and uid == ADMIN_ID:
        return await manage_admins_menu(event)

    elif action_data.startswith("tglperm|") and uid == ADMIN_ID:
        _, t_id, p_name = action_data.split("|")
        adm = admins_col.find_one({"user_id": int(t_id)})
        cur_val = (adm or {}).get(p_name, 0)
        admins_col.update_one({"user_id": int(t_id)}, {"$set": {p_name: 0 if cur_val == 1 else 1}})
        return await edit_admin_menu(event, t_id)
        
    elif action_data.startswith("deladmin|") and uid == ADMIN_ID:
        t_id = action_data.split("|")[1]
        admins_col.delete_one({"user_id": int(t_id)})
        await event.answer("✅ Admin Removed", alert=True)
        return await manage_admins_menu(event)

    elif action_data == "managestock" and has_perm(uid, 'p_manage_stock'): return await send_manage_stock_page(event, 1)
    elif action_data.startswith("mspg|") and has_perm(uid, 'p_manage_stock'): return await send_manage_stock_page(event, int(action_data.split("|")[1]))
    elif action_data.startswith("msc|") and has_perm(uid, 'p_manage_stock'): return await send_manage_stock_country(event, action_data.split("|")[1])
    elif action_data == "autoprice" and has_perm(uid, 'p_manage_stock'): return await send_autoprice_page(event, 1)
    elif action_data.startswith("appg|") and has_perm(uid, 'p_manage_stock'): return await send_autoprice_page(event, int(action_data.split("|")[1]))
    elif action_data.startswith("apc|") and has_perm(uid, 'p_manage_stock'): return await send_autoprice_country(event, action_data.split("|")[1])
        
    elif action_data == "backupusr" and has_perm(uid, 'p_settings'):
        all_users = list(users_col.find({}, {"_id": 0}))
        fields = ["user_id", "balance", "referred_by", "total_deposited", "joined_date", "banned", "discount", "terms_accepted"]
        with open("users_backup.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(fields)
            for u in all_users:
                w.writerow([u.get(k, "") for k in fields])
        await bot.send_file(chat, "users_backup.csv", caption=f"{P_USERS} <b>Users Backup CSV</b>")
        os.remove("users_backup.csv")
        return await event.answer("✅ Backup Generated!", alert=True)

    elif action_data == "exportall" and has_perm(uid, 'p_manage_stock'):
        # Export ALL available sessions as a ZIP (GridFS + local fallback)
        # (zipfile/shutil/tempfile are module-level — do NOT re-import here:
        #  local import makes zipfile a function-local name and breaks addzip)
        from utils.gridfs_sessions import get_session_bytes

        docs = list(stock_col.find({"available": 1}))
        if not docs:
            return await event.answer("❌ No available stock to export.", alert=True)

        await event.answer(f"⏳ Packaging {len(docs)} sessions...", alert=False)
        tmp_dir = tempfile.mkdtemp(prefix="admin_export_")
        zip_path = os.path.join(tmp_dir, f"all_available_{len(docs)}sessions.zip")
        packed = 0
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for doc in docs:
                    phone = str(doc.get("phone", ""))
                    data = None
                    gridfs_id = doc.get("gridfs_id")
                    if gridfs_id:
                        try:
                            data = get_session_bytes(gridfs_id)
                        except Exception as e:
                            logger.error("exportall GridFS fail %s: %s", phone, e)
                    if data is None:
                        local = doc.get("session_file")
                        if local:
                            path = local if str(local).endswith(".session") else str(local) + ".session"
                            if os.path.isfile(path):
                                with open(path, "rb") as f:
                                    data = f.read()
                    if data is None:
                        continue
                    # folder by country for convenience
                    country = doc.get("country_name") or "Unknown"
                    year = doc.get("account_year") or "na"
                    zf.writestr(f"{country}/{year}/{phone}.session", data)
                    packed += 1

            if packed == 0:
                await bot.send_message(chat, f"{P_NO} No session binaries found (GridFS/local empty).")
                return

            await bot.send_file(
                chat,
                zip_path,
                caption=(
                    f"{P_PKG} <b>Export All Available Sessions</b>\n"
                    f"Packed: <b>{packed}</b> / {len(docs)}\n"
                    f"<i>Stock is NOT deleted — this is a backup only.</i>"
                ),
                force_document=True,
            )
        except Exception as e:
            logger.exception("exportall error: %s", e)
            await bot.send_message(chat, f"{P_NO} Export failed: <code>{e}</code>")
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
        return


    elif action_data == "waservices" and has_perm(uid, 'p_manage_stock'):
        from plugins.whatsapp import wa_services_admin_menu
        return await wa_services_admin_menu(event)

    elif action_data.startswith("wa_delsvc|") and has_perm(uid, 'p_manage_stock'):
        sid = int(action_data.split("|")[1])
        wa_services_col.delete_one({"id": sid})
        await event.answer("✅ Service deleted", alert=True)
        from plugins.whatsapp import wa_services_admin_menu
        return await wa_services_admin_menu(event)

    async with bot.conversation(chat, timeout=600) as conv:
        async def get_reply(txt, *, allow_file=False):
            """Ignore menu taps / empty messages so junk is not saved to DB."""
            MENU_NOISE = {
                "🛒 𝐁ᴜʏ 𝐀ᴄᴄᴏᴜɴᴛ", "📁 𝐁ᴜʏ 𝐒ᴇssɪᴏɴs", "📱 𝐖𝐡𝐚𝐭𝐬𝐀𝐩𝐩", "💳 𝐃ᴇᴘᴏsɪᴛ",
                "📦 𝐌ʏ 𝐎ʀᴅᴇʀs", "👤 𝐏ʀᴏғɪʟᴇ", "💰 𝐁ᴀʟᴀɴᴄᴇ", "📊 𝐒ᴛᴏᴄᴋ",
                "🎁 𝐑ᴇғᴇʀ", "📩 𝐒ᴜᴘᴘᴏʀᴛ", "🏠 𝐒ᴛᴀʀᴛ", "🔐 𝐀ᴅᴍɪɴ 𝐏ᴀɴᴇʟ",
                "🛒 Buy Account", "📁 Buy Sessions", "📱 WhatsApp", "💳 Deposit",
                "📦 My Orders", "👤 Profile", "💰 Balance", "📊 Stock",
                "🎁 Refer", "📩 Support", "🏠 Start", "🔐 Admin Panel",
            }
            while True:
                await conv.send_message(txt + "\n\n<i>(Type /cancel to abort)</i>")
                resp = await conv.get_response()
                raw = (resp.text or "").strip()
                if raw.lower() in ("/cancel", "cancel"):
                    raise ValueError("Cancelled")
                if raw in MENU_NOISE or raw.startswith("🔐"):
                    await conv.send_message(
                        f"{P_WARN} Menu button ignored. Send the requested value as text, or /cancel."
                    )
                    continue
                if allow_file and resp.file:
                    return resp
                if not raw:
                    await conv.send_message(f"{P_WARN} Empty. Send text or /cancel.")
                    continue
                return resp

        try:
            if action_data == "ap_add_country" and has_perm(uid, 'p_manage_stock'):
                code = (await get_reply(f"{P_PHONE} <b>Enter Country Calling Code (without +):</b>\n<i>Example: 91</i>")).text.replace("+", "").strip()
                flag = html.escape((await get_reply(f"{P_FLAG} <b>Enter Country Flag Emoji:</b>\n<i>Example: 🇮🇳</i>")).text.strip())
                name = html.escape((await get_reply(f"{P_GLOBE} <b>Enter Country Name:</b>\n<i>Example: India</i>")).text.strip())
                
                custom_countries_col.update_one({"code": code}, {"$set": {"code": code, "name": name, "flag": flag}}, upsert=True)
                await conv.send_message(f"{P_YES} <b>Custom Country Added Successfully!</b>\n{flag} {name} (+{code})\n\n<i>It will now automatically be recognized when adding stock!</i>")

            elif action_data == "userinfo" and has_perm(uid, 'p_stats'):
                t_uid = int((await get_reply(f"{P_ACC} <b>Enter User ID:</b>")).text)
                u_row = users_col.find_one({"user_id": t_uid})
                if not u_row: return await conv.send_message(f"{P_NO} User not found.")
                
                o_agg = list(orders_col.aggregate([{"$match": {"user_id": t_uid}}, {"$group": {"_id": None, "count": {"$sum": 1}, "spent": {"$sum": "$price"}}}]))
                up_agg = list(upi_orders_col.aggregate([{"$match": {"user_id": t_uid, "status": {"$in": ["success", "verified"]}}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]))
                
                bal = u_row.get("balance", 0)
                dep = u_row.get("total_deposited", 0)
                joined = u_row.get("joined_date", "")
                is_banned = u_row.get("banned", 0)
                disc = u_row.get("discount", 0)
                o_count = o_agg[0]["count"] if o_agg else 0
                o_spent = o_agg[0]["spent"] if o_agg else 0
                u_upi = up_agg[0]["total"] if up_agg else 0
                
                msg = (f"{P_ACC} <b>USER INFO:</b> <code>{t_uid}</code>\n\n"
                       f"{P_MONEY} Balance: {P_INR}{bal}\n"
                       f"{P_CARD} Total Deposited: {P_INR}{dep}\n"
                       f"{P_UPI} UPI Deposited: {P_INR}{u_upi}\n"
                       f"{P_CART} Total Orders: {o_count}\n"
                       f"{P_USDT} Total Spent: {P_INR}{o_spent}\n"
                       f"{P_GIFT} Discount: {disc}%\n"
                       f"{P_CAL} Joined: {joined}\n"
                       f"{P_OFF} Banned: {'Yes' if is_banned else 'No'}")
                await conv.send_message(msg)

            elif action_data == "addadmin" and uid == ADMIN_ID:
                new_ad = int((await get_reply(f"{P_ACC} <b>Enter User ID for new Admin:</b>")).text)
                admins_col.update_one({"user_id": new_ad}, {"$setOnInsert": {"user_id": new_ad, "p_add_stock": 0, "p_manage_stock": 0, "p_stats": 0, "p_bal": 0, "p_settings": 0}}, upsert=True)
                await conv.send_message(f"{P_YES} Admin added!")
                class FakeEvent: 
                    async def edit(self, text, buttons): await bot.send_message(chat, text, buttons=buttons)
                    async def answer(self, txt, alert): pass
                await edit_admin_menu(FakeEvent(), new_ad)
                
            elif action_data == "editadminreq" and uid == ADMIN_ID:
                t_id = int((await get_reply(f"{P_ACC} <b>Enter User ID to edit:</b>")).text)
                class FakeEvent: 
                    async def edit(self, text, buttons): await bot.send_message(chat, text, buttons=buttons)
                    async def answer(self, txt, alert): pass
                await edit_admin_menu(FakeEvent(), t_id)

            elif action_data.startswith("msedit|") and has_perm(uid, 'p_manage_stock'):
                parts = action_data.split("|")
                action, c_name = parts[1], parts[2]
                
                if action == "name":
                    new_name = html.escape((await get_reply(f"{P_DOC} <b>Enter NEW Name for {c_name}:</b>")).text)
                    stock_col.update_many({"country_name": c_name}, {"$set": {"country_name": new_name}})
                    auto_prices_col.update_many({"country": c_name}, {"$set": {"country": new_name}})
                    await conv.send_message(f"{P_YES} Country '{c_name}' successfully renamed to '{new_name}'!")
                    
                elif action == "flag":
                    new_flag = html.escape((await get_reply(f"{P_FLAG} <b>Enter NEW Flag Emoji for {c_name}:</b>")).text)
                    stock_col.update_many({"country_name": c_name}, {"$set": {"country_icon": new_flag}})
                    await conv.send_message(f"{P_YES} Flag updated to {new_flag} for '{c_name}'!")
                    
                elif action == "cprice":
                    new_p = int((await get_reply(f"{P_MONEY} <b>Enter NEW Common Price for all {c_name} accounts:</b>")).text)
                    stock_col.update_many({"country_name": c_name}, {"$set": {"price": new_p}})
                    await conv.send_message(f"{P_YES} All existing '{c_name}' accounts updated to {P_INR}{new_p}!")
                    
                elif action == "yprice":
                    year = parts[3]
                    new_p = int((await get_reply(f"{P_MONEY} <b>Enter NEW Price for {c_name} ({year}):</b>")).text)
                    stock_col.update_many({"country_name": c_name, "account_year": int(year)}, {"$set": {"price": new_p}})
                    await conv.send_message(f"{P_YES} All existing '{c_name}' ({year}) accounts updated to {P_INR}{new_p}!")
                    
            elif action_data.startswith("apset|") and has_perm(uid, 'p_manage_stock'):
                parts = action_data.split("|")
                c_name, year = parts[1], parts[2]
                new_p = int((await get_reply(f"{P_ASST} <b>Enter Auto-Price for {c_name} ({year}):</b>\n<i>(Enter 0 to remove this auto-price)</i>")).text)
                if new_p == 0:
                    auto_prices_col.delete_one({"country": c_name, "year": str(year)})
                    await conv.send_message(f"{P_YES} Auto-Price for {c_name} ({year}) removed!")
                else:
                    auto_prices_col.update_one({"country": c_name, "year": str(year)}, {"$set": {"country": c_name, "year": str(year), "price": new_p}}, upsert=True)
                    await conv.send_message(f"{P_YES} Auto-Price for {c_name} ({year}) set to {P_INR}{new_p}! Incoming accounts will use this price automatically.")

            elif action_data == "addpay" and has_perm(uid, 'p_settings'):
                name = html.escape((await get_reply(f"{P_CARD} <b>Enter Payment Method Name:</b>\n<i>(e.g., Binance Pay, TRX)</i>")).text)
                qr_msg = await get_reply(f"📸 <b>Send QR Code Image:</b>\n<i>(Or type <code>skip</code> if no QR needed)</i>")
                qr_path = ""
                if qr_msg.photo:
                    qr_path = f"qr_{int(time.time())}.jpg"
                    await bot.download_media(qr_msg, qr_path)
                
                cap_msg = (await get_reply(f"{P_DOC} <b>Enter Payment Caption:</b>\n<i>(Use <code>text</code> to make wallet IDs or UPI copyable)</i>")).text
                cap_msg = html.escape(cap_msg).replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>")
                pid = mongo_db.next_id("custom_payments")
                custom_payments_col.insert_one({"id": pid, "name": name, "caption": cap_msg, "qr_file_id": qr_path})
                await conv.send_message(f"{P_YES} Payment Method '{name}' added successfully!")

            elif action_data == "delpay" and has_perm(uid, 'p_settings'):
                rows = list(custom_payments_col.find({}, {"id": 1, "name": 1}))
                if not rows: return await conv.send_message(f"{P_NO} No custom payment methods.")
                msg = f"{P_DOC} <b>Reply with the ID of the method to delete:</b>\n\n"
                for r in rows: msg += f"ID: {r.get('id')} - {r.get('name')}\n"
                del_id = (await get_reply(msg)).text
                try:
                    del_id = int(del_id)
                    doc = custom_payments_col.find_one({"id": del_id})
                    if doc and doc.get("qr_file_id") and os.path.exists(doc["qr_file_id"]): os.remove(doc["qr_file_id"])
                    custom_payments_col.delete_one({"id": del_id})
                    await conv.send_message(f"{P_YES} Deleted!")
                except: await conv.send_message(f"{P_NO} Invalid ID.")

            elif action_data == "addzip" and has_perm(uid, 'p_add_stock'):
                resp = await get_reply(f"{P_PKG} <b>Send the ZIP file containing <code>.session</code> files:</b>")
                if not resp.file or not resp.file.name.endswith('.zip'): return await conv.send_message(f"{P_NO} Invalid file.")
                
                await conv.send_message(f"{P_WAIT} <b>Extracting & Scanning Accounts...</b>")
                zip_path = await bot.download_media(resp, "temp_sessions.zip")
                if not zip_path or not os.path.isfile(zip_path):
                    return await conv.send_message(f"{P_NO} Failed to download ZIP file.")
                extracted_dir = f"temp_extracted_{int(time.time())}"
                os.makedirs(extracted_dir, exist_ok=True)
                try:
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        zip_ref.extractall(extracted_dir)
                except zipfile.BadZipFile:
                    try:
                        os.remove(zip_path)
                    except Exception:
                        pass
                    return await conv.send_message(f"{P_NO} Invalid or corrupted ZIP file.")

                groups = {}
                for file in os.listdir(extracted_dir):
                    if not file.endswith(".session"): continue
                    sess_path = os.path.join(extracted_dir, file)
                    clean_path = sess_path[:-8]
                    try:
                        client = TelegramClient(clean_path, API_ID, API_HASH)
                        await client.connect()
                        if not await client.is_user_authorized(): await client.disconnect(); continue
                        me = await client.get_me()
                        phone = getattr(me, 'phone', None)
                        if not phone: await client.disconnect(); continue
                        
                        c_name, c_icon = get_country_info(phone)
                        pwd = await client(GetPasswordRequest())
                        has_2fa = pwd.has_password
                        year = await detect_account_year(client)
                        await client.disconnect()

                        key = (c_name, year, has_2fa)
                        if key not in groups: groups[key] = []
                        groups[key].append({"phone": phone, "path": clean_path, "c_icon": c_icon})
                    except Exception as e: logger.error(f"Scan error: {e}")

                for key in list(groups.keys()):
                    if key[0] == "Unknown":
                        sample_phone = groups[key][0]["phone"]
                        await conv.send_message(f"{P_WARN} <b>Country not recognized for +{sample_phone}!</b>")
                        new_icon = html.escape((await get_reply(f"{P_FLAG} <b>Enter Country Flag Emoji:</b>\n<i>Example: 🇮🇳</i>")).text)
                        new_name = html.escape((await get_reply(f"{P_GLOBE} <b>Enter Country Name:</b>\n<i>Example: India</i>")).text)
                        new_key = (new_name, key[1], key[2])
                        groups[new_key] = groups.pop(key)
                        for acc in groups[new_key]: acc["c_icon"] = new_icon

                success = 0
                for (c_name, year, has_2fa), accs in groups.items():
                    c_icon = accs[0]["c_icon"]
                    twofa_pass = "None"
                    if has_2fa: twofa_pass = html.escape((await get_reply(f"{P_2FA} <b>Enter 2FA Password for {len(accs)}x {c_name} accounts:</b>")).text)

                    auto_row = auto_prices_col.find_one({"country": c_name, "year": str(year)})
                    if not auto_row: auto_row = auto_prices_col.find_one({"country": c_name, "year": "Common"})
                    auto_row = (auto_row["price"],) if auto_row else None

                    if auto_row:
                        price = auto_row[0]
                        await conv.send_message(f"⚡ <b>Auto-Price Applied:</b> {len(accs)}x {c_name} ({year}) at {P_INR}{price}.")
                    else:
                        ep = stock_col.find_one({"country_name": c_name}, {"price": 1})
                        existing_price = (ep["price"],) if ep else None
                        if existing_price:
                            price = existing_price[0]
                            await conv.send_message(f"⚡ <b>Auto-Added:</b> {len(accs)}x {c_name} at {P_INR}{price} (Copied from DB).")
                        else:
                            price = int((await get_reply(f"📌 Found {len(accs)}x {c_name} ({year}).\n{P_MONEY} Enter Price (₹):")).text)

                    type_raw = (await get_reply(
                        f"{P_PKG} <b>Account Type</b>? Reply: Fresh / Aged / Spam"
                    )).text.strip().lower()
                    type_map = {"fresh": "Fresh", "aged": "Aged", "spam": "Spam", "f": "Fresh", "a": "Aged", "s": "Spam"}
                    acc_type = type_map.get(type_raw)
                    if not acc_type:
                        await conv.send_message(f"{P_WARN} Unknown type — using <b>Fresh</b>.")
                        acc_type = "Fresh"

                    from utils.gridfs_sessions import store_session_file
                    for acc in accs:
                        # Prefer extracted path; fall back to moved local path
                        sess_src = acc["path"] + ".session"
                        if not os.path.exists(sess_src):
                            # ensure file exists next to path
                            for ext in [".session"]:
                                p = acc["path"] + ext
                                if os.path.exists(p):
                                    sess_src = p
                                    break
                        try:
                            gridfs_id = store_session_file(acc["phone"], sess_src)
                        except Exception as store_err:
                            logger.error("GridFS store failed for %s: %s", acc["phone"], store_err)
                            continue
                        # Optional local mirror (helps single-machine debugging)
                        os.makedirs("sessions", exist_ok=True)
                        perm_base = f"sessions/{acc['phone']}"
                        try:
                            if os.path.exists(sess_src) and os.path.abspath(sess_src) != os.path.abspath(perm_base + ".session"):
                                shutil.copy2(sess_src, perm_base + ".session")
                        except Exception:
                            pass
                        stock_col.update_one(
                            {"phone": acc["phone"]},
                            {"$set": {
                                "phone": acc["phone"],
                                "gridfs_id": gridfs_id,
                                "session_file": perm_base + ".session",  # fallback for old code paths
                                "country_name": c_name, "country_icon": c_icon,
                                "account_year": year, "category": "Good", "account_type": acc_type, "price": price,
                                "available": 1, "twofa": twofa_pass,
                                "added_date": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                            }},
                            upsert=True
                        )
                        success += 1
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
                try:
                    shutil.rmtree(extracted_dir)
                except Exception:
                    pass
                await conv.send_message(f"{P_YES} <b>Bulk Interactive Upload Complete!</b>\n{P_ON} Added: {success}\n📦 Sessions stored in MongoDB GridFS")

            elif action_data == "addstock" and has_perm(uid, 'p_add_stock'):
                phone = (await get_reply(f"{P_PHONE} Enter Phone (+919999...):")).text.replace(" ", "").replace("+", "")
                os.makedirs("sessions", exist_ok=True)
                sp = f"sessions/{phone}"
                client = TelegramClient(sp, API_ID, API_HASH)
                await client.connect()
                sreq = await client.send_code_request(phone)
                
                twofa_pass = "None"
                try: 
                    await client.sign_in(phone, (await get_reply(f"{P_OTP} OTP:")).text, phone_code_hash=sreq.phone_code_hash)
                except SessionPasswordNeededError: 
                    twofa_pass = html.escape((await get_reply(f"{P_2FA} 2FA Pass required. Enter it now:")).text)
                    await client.sign_in(password=twofa_pass)
                
                c_name, c_icon = get_country_info(phone)
                
                if c_name == "Unknown":
                    await conv.send_message(f"{P_WARN} <b>Country not recognized for +{phone}!</b>")
                    c_icon = html.escape((await get_reply(f"{P_FLAG} <b>Enter Country Flag Emoji:</b>\n<i>Example: 🇮🇳</i>")).text)
                    c_name = html.escape((await get_reply(f"{P_GLOBE} <b>Enter Country Name:</b>\n<i>Example: India</i>")).text)
                
                auto_year = await detect_account_year(client)
                await client.disconnect()
                
                year = int((await get_reply(f"{P_CAL} Detected Year: <b>{auto_year}</b>\nReply with Year to confirm or change:")).text)
                auto_row = auto_prices_col.find_one({"country": c_name, "year": str(year)})
                if not auto_row: auto_row = auto_prices_col.find_one({"country": c_name, "year": "Common"})
                auto_row = (auto_row["price"],) if auto_row else None

                if auto_row:
                    price = auto_row[0]
                    await conv.send_message(f"⚡ <b>Auto-Price Applied:</b> {P_INR}{price} for {c_name} ({year})")
                else:
                    ep = stock_col.find_one({"country_name": c_name}, {"price": 1})
                    existing_price = (ep["price"],) if ep else None
                    if existing_price:
                        price = existing_price[0]
                        await conv.send_message(f"⚡ <b>Auto-detected Price:</b> {P_INR}{price} for {c_name}")
                    else:
                        price = int((await get_reply(f"{P_MONEY} Price (₹):")).text)

                    type_raw = (await get_reply(
                        f"{P_PKG} <b>Account Type</b>? Reply: Fresh / Aged / Spam"
                    )).text.strip().lower()
                type_map = {"fresh": "Fresh", "aged": "Aged", "spam": "Spam", "f": "Fresh", "a": "Aged", "s": "Spam"}
                acc_type = type_map.get(type_raw) or "Fresh"
                if type_raw not in type_map:
                    await conv.send_message(f"{P_WARN} Unknown type — using <b>Fresh</b>.")

                from utils.gridfs_sessions import store_session_file
                try:
                    gridfs_id = store_session_file(phone, sp + ".session")
                except Exception as store_err:
                    logger.error("GridFS store failed for single add: %s", store_err)
                    return await conv.send_message(f"{P_NO} Failed to store session in GridFS: {store_err}")
                
                stock_col.update_one(
                    {"phone": phone},
                    {"$set": {
                        "phone": phone,
                        "gridfs_id": gridfs_id,
                        "session_file": sp + ".session",
                        "country_name": c_name, "country_icon": c_icon,
                        "account_year": year, "category": "Good", "account_type": acc_type, "price": price,
                        "available": 1, "twofa": twofa_pass,
                        "added_date": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    }},
                    upsert=True
                )
                await conv.send_message(f"{P_YES} Added! Session stored in MongoDB GridFS.")

            elif action_data == "supporturl" and has_perm(uid, 'p_settings'):
                raw = (await get_reply("🔗 Enter new Support URL (must start with http:// or https://):")).text
                url = re.sub(r"<[^>]+>", "", raw or "")
                url = html.unescape(url).strip()
                url = url.split()[0] if url else ""
                if url.startswith("@"):
                    url = "https://t.me/" + url.lstrip("@")
                if not url.startswith("http"):
                    url = "https://" + url.replace("@", "t.me/")
                if not url.startswith(("http://", "https://")):
                    await conv.send_message(f"{P_NO} Invalid URL. Example: https://t.me/yoursupport")
                else:
                    settings_col.update_one({"key": "support_url"}, {"$set": {"value": url}}, upsert=True)
                    await conv.send_message(f"{P_YES} Support URL updated: <code>{html.escape(url)}</code>")


            elif action_data == "countrydesc" and has_perm(uid, 'p_manage_stock'):
                c_name = (await get_reply(
                    f"{P_GLOBE} Enter Country Name for description (e.g. India)"
                )).text.strip()
                c_name = html.unescape(re.sub(r"<[^>]+>", "", c_name)).strip()
                old = get_country_description(c_name)
                prompt = f"{P_DOC} Description for {html.escape(c_name)} (shown on stock page)"
                if old:
                    prompt = prompt + "\nCurrent:\n" + old + "\n\nSend new text or clear"
                else:
                    prompt = prompt + "\nSend description text:"
                desc = (await get_reply(prompt)).text
                if desc.strip().lower() == "clear":
                    settings_col.delete_one({"key": f"country_desc:{c_name}"})
                    await conv.send_message(f"{P_YES} Description cleared for {html.escape(c_name)}")
                else:
                    set_country_description(c_name, desc.strip())
                    await conv.send_message(f"{P_YES} Description saved for {html.escape(c_name)}")

            elif action_data == "bcast" and has_perm(uid, 'p_stats'):
                txt = (await get_reply(f"{P_DOC} <b>Message (Supports HTML & tg-emoji tags):</b>")).text
                btn_name = (await get_reply(f"🔘 <b>Button Name (or 'skip'):</b>")).text
                url = (await get_reply("🔗 <b>URL:</b>")).text if btn_name.lower() != 'skip' else None
                btns = [[Button.url(btn_name, url)]] if url else None
                users = [(u["user_id"],) for u in users_col.find({}, {"user_id": 1})]
                s, f = 0, 0
                total = len(users)
                status_msg = await conv.send_message(f"{P_TG} Broadcasting to {total} users...")
                for idx, (u_id,) in enumerate(users):
                    try: 
                        await bot.send_message(int(u_id), txt, buttons=btns, parse_mode='html')
                        s += 1
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 1)
                        try:
                            await bot.send_message(int(u_id), txt, buttons=btns, parse_mode='html')
                            s += 1
                        except Exception:
                            f += 1
                    except (UserIsBlockedError, InputUserDeactivatedError):
                        # User blocked bot or account deleted — skip, never stop broadcast
                        f += 1
                    except Exception:
                        f += 1
                    if (idx + 1) % 50 == 0:
                        try: await status_msg.edit(f"{P_TG} Broadcasting... {idx+1}/{total} (✅ {s} | ❌ {f})")
                        except Exception as _e:
                            logger.exception("suppressed error: %s", _e)
                    await asyncio.sleep(0.05) 
                await conv.send_message(f"{P_YES} Done! Sent: {s} | Failed: {f} | Total: {total}")

            elif action_data == "bal" and has_perm(uid, 'p_bal'):
                t_uid = int((await get_reply(f"{P_ACC} <b>User ID:</b>")).text)
                amt = int((await get_reply(f"{P_MONEY} <b>Amount (Negative to deduct):</b>")).text)
                update_balance(t_uid, amt)
                await conv.send_message(f"{P_YES} Added {P_INR}{amt} to {t_uid}.")
                
            elif action_data == "discount" and has_perm(uid, 'p_settings'):
                t_uid = int((await get_reply(f"{P_ACC} <b>User ID:</b>")).text)
                pct = int((await get_reply(f"{P_GIFT} <b>Discount % (0 to remove):</b>")).text)
                users_col.update_one({"user_id": t_uid}, {"$set": {"discount": pct}})
                await conv.send_message(f"{P_YES} User {t_uid} has {pct}% discount.")
                
            elif action_data == "refpct" and has_perm(uid, 'p_settings'):
                pct = int((await get_reply(f"{P_USERS} <b>New Referral %:</b>")).text)
                settings_col.update_one({"key": "ref_percent"}, {"$set": {"value": str(pct)}}, upsert=True)
                await conv.send_message(f"{P_YES} Ref revenue set to {pct}%.")

            elif action_data == "usdtrate" and has_perm(uid, 'p_settings'):
                r = float((await get_reply(f"{P_USDT} <b>New USDT Rate (INR):</b>")).text)
                settings_col.update_one({"key": "usdt_rate"}, {"$set": {"value": str(r)}}, upsert=True)
                await conv.send_message(f"{P_YES} Rate set to {r}.")

            elif action_data == "restoreusr" and has_perm(uid, 'p_settings'):
                resp = await get_reply(f"📤 <b>Send the <code>users_backup.csv</code> file:</b>")
                if not resp.file or not resp.file.name.endswith('.csv'): return await conv.send_message(f"{P_NO} Invalid file.")
                await bot.download_media(resp, "temp_restore.csv")
                with open("temp_restore.csv", "r", encoding="utf-8") as f:
                    reader = csv.reader(f); next(reader); count = 0
                    for row in reader:
                        try:
                            users_col.update_one(
                                {"user_id": int(row[0])},
                                {"$set": {
                                    "user_id": int(row[0]),
                                    "balance": int(row[1]),
                                    "referred_by": int(row[2]) if row[2] else None,
                                    "total_deposited": int(row[3]),
                                    "joined_date": row[4],
                                    "banned": int(row[5]),
                                    "discount": int(row[6]),
                                    "terms_accepted": int(row[7]),
                                }},
                                upsert=True
                            )
                            count += 1
                        except Exception as _e:
                            logger.exception("suppressed error: %s", _e)
                os.remove("temp_restore.csv")
                await conv.send_message(f"{P_YES} Restored {count} users.")


            elif action_data == "wa_add" and has_perm(uid, 'p_manage_stock'):
                country = html.escape((await get_reply(
                    f"{P_GLOBE} <b>Enter Country Name:</b>\n<i>Example: India</i>"
                )).text.strip())
                price = int((await get_reply(
                    f"{P_MONEY} <b>Enter Price (₹):</b>"
                )).text.strip())
                if price <= 0:
                    await conv.send_message(f"{P_NO} Price must be > 0")
                else:
                    sid = mongo_db.next_id("wa_services")
                    wa_services_col.insert_one({
                        "id": sid,
                        "country_name": country,
                        "price": price,
                        "active": 1,
                    })
                    await conv.send_message(
                        f"{P_YES} WhatsApp service added!\n"
                        f"ID: <code>{sid}</code> | {country} | {P_INR}{price}"
                    )

            elif action_data == "wa_del" and has_perm(uid, 'p_manage_stock'):
                rows = list(wa_services_col.find({}).sort("id", 1))
                if not rows:
                    await conv.send_message(f"{P_NO} No WhatsApp services to delete.")
                else:
                    listing = f"{P_DOC} <b>Reply with Service ID to delete:</b>\n\n"
                    for r in rows:
                        listing += (
                            f"ID <code>{r['id']}</code> — {r['country_name']} "
                            f"{P_INR}{r['price']}\n"
                        )
                    del_id = int((await get_reply(listing)).text.strip())
                    res = wa_services_col.delete_one({"id": del_id})
                    if res.deleted_count:
                        await conv.send_message(f"{P_YES} Deleted service ID {del_id}")
                    else:
                        await conv.send_message(f"{P_NO} Service ID not found.")

            elif action_data == "ban" and has_perm(uid, 'p_bal'):
                t_uid = int((await get_reply(f"{P_ACC} <b>User ID:</b>")).text)
                prev = users_col.find_one({"user_id": t_uid}, {"banned": 1})
                if not prev:
                    return await conv.send_message(f"{P_NO} User not found.")
                ns = 0 if prev.get("banned") == 1 else 1
                # Atomic: only flip if still at expected value
                res = users_col.update_one({"user_id": t_uid, "banned": prev.get("banned", 0)}, {"$set": {"banned": ns}})
                if res.modified_count == 0:
                    return await conv.send_message(f"{P_WARN} Ban state changed concurrently, try again.")
                await conv.send_message(f"User {t_uid} is {'Banned 🚫' if ns == 1 else 'Unbanned ✅'}.")

        except ValueError: await conv.send_message(f"{P_NO} Cancelled.")
        except Exception as e: await conv.send_message(f"{P_NO} Error: {e}")

def register_admin_actions(bot):
    @bot.on(events.CallbackQuery(pattern=r"^adm_"))
    async def cb_admin_actions(e):
        if is_admin(e.sender_id):
            await admin_actions(e)
