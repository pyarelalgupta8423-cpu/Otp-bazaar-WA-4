import asyncio

active_orders = {}
waiting_proof = {}
deposit_input = {}
admin_dep_state = {}
user_spam_cooldown = {}
session_buy_state = {}
custom_dep_amt = {}
user_locks = {}
admin_state = {}


def get_user_lock(uid):
    if uid not in user_locks:
        user_locks[uid] = asyncio.Lock()
    return user_locks[uid]


async def cleanup_expired_orders(max_age_seconds=600):
    """Periodic safety net: refund unpaid orders that outlived AUTO_CANCEL_SECONDS.
    Call this from a background task so memory and stock never leak.
    """
    import time
    from database import users_col, stock_col

    now = time.time()
    expired = []
    for phone, order in list(active_orders.items()):
        if order.get("paid"):
            continue
        if now - order.get("start_time", now) >= max_age_seconds:
            expired.append(phone)
    for phone in expired:
        order = active_orders.pop(phone, None)
        if not order:
            continue
        uid = order.get("uid")
        try:
            await order["client"].disconnect()
        except Exception:
            pass
        if order.get("tmp_from_gridfs"):
            try:
                from utils.gridfs_sessions import cleanup_temp_session

                cleanup_temp_session(order.get("sess"))
            except Exception:
                pass
        try:
            async with get_user_lock(uid):
                users_col.update_one(
                    {"user_id": uid}, {"$inc": {"balance": order["price"]}}
                )
                stock_col.update_one({"phone": phone}, {"$set": {"available": 1}})
        except Exception:
            pass
