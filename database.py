"""
MongoDB Atlas database layer for Numbott.
Replaces the previous SQLite implementation so data survives redeploys/crashes.
"""
import os
from datetime import datetime
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from config import ADMIN_ID

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "numbott").strip() or "numbott"

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI is required. Set it to your MongoDB Atlas connection string "
        "(e.g. mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority)"
    )

_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
_db = _client[MONGO_DB_NAME]

# Collections
users_col = _db["users"]
settings_col = _db["settings"]
stock_col = _db["stock"]
auto_prices_col = _db["auto_prices"]
deposits_col = _db["deposits"]
upi_orders_col = _db["upi_orders"]
orders_col = _db["orders"]
custom_payments_col = _db["custom_payments"]
admins_col = _db["admins"]
custom_countries_col = _db["custom_countries"]
counters_col = _db["counters"]
wa_services_col = _db["wa_services"]
wa_orders_col = _db["wa_orders"]


def _next_id(name: str) -> int:
    """Atomic auto-increment counter (replaces SQLite AUTOINCREMENT)."""
    doc = counters_col.find_one_and_update(
        {"_id": name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


def setup_db():
    """Create indexes for performance and uniqueness (idempotent)."""
    users_col.create_index("user_id", unique=True)
    settings_col.create_index("key", unique=True)
    stock_col.create_index("phone", unique=True)
    stock_col.create_index([("country_name", ASCENDING), ("available", ASCENDING)])
    stock_col.create_index([("country_name", ASCENDING), ("account_year", ASCENDING), ("price", ASCENDING), ("available", ASCENDING)])
    auto_prices_col.create_index([("country", ASCENDING), ("year", ASCENDING)], unique=True)
    deposits_col.create_index("id", unique=True)
    deposits_col.create_index([("status", ASCENDING)])
    upi_orders_col.create_index("order_id", unique=True)
    orders_col.create_index("id", unique=True)
    orders_col.create_index([("user_id", ASCENDING), ("id", DESCENDING)])
    custom_payments_col.create_index("id", unique=True)
    admins_col.create_index("user_id", unique=True)
    custom_countries_col.create_index("code", unique=True)
    custom_countries_col.create_index("name")
    wa_services_col.create_index("id", unique=True)
    wa_services_col.create_index([("active", ASCENDING), ("country_name", ASCENDING)])
    wa_orders_col.create_index("id", unique=True)
    wa_orders_col.create_index([("status", ASCENDING), ("created_ts", ASCENDING)])
    wa_orders_col.create_index([("user_id", ASCENDING), ("id", DESCENDING)])
    wa_orders_col.create_index("admin_request_msg_id")
    wa_orders_col.create_index("admin_otp_msg_id")

    # Seed default settings if missing
    if settings_col.count_documents({"key": "bot_status"}) == 0:
        settings_col.insert_one({"key": "bot_status", "value": "on"})
    if settings_col.count_documents({"key": "usdt_rate"}) == 0:
        settings_col.insert_one({"key": "usdt_rate", "value": "94.0"})
    if settings_col.count_documents({"key": "support_url"}) == 0:
        settings_col.insert_one({"key": "support_url", "value": "https://t.me/tgtelehelpbot"})
    if settings_col.count_documents({"key": "ref_percent"}) == 0:
        settings_col.insert_one({"key": "ref_percent", "value": "3"})
    if settings_col.count_documents({"key": "upi_revenue"}) == 0:
        settings_col.insert_one({"key": "upi_revenue", "value": "0"})


setup_db()


# ================= HELPER FUNCTIONS =================

def is_bot_online():
    res = settings_col.find_one({"key": "bot_status"})
    return (res or {}).get("value", "on") == "on"


def is_admin(uid):
    if uid == ADMIN_ID:
        return True
    return admins_col.find_one({"user_id": uid}) is not None


def has_perm(uid, perm):
    if uid == ADMIN_ID:
        return True
    row = admins_col.find_one({"user_id": uid})
    return bool(row and row.get(perm) == 1)


def ensure_user(uid):
    users_col.update_one(
        {"user_id": uid},
        {
            "$setOnInsert": {
                "user_id": uid,
                "balance": 0,
                "referred_by": None,
                "total_deposited": 0,
                "joined_date": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "banned": 0,
                "discount": 0,
                "terms_accepted": 0,
            }
        },
        upsert=True,
    )


def get_usdt_rate():
    res = settings_col.find_one({"key": "usdt_rate"})
    try:
        return float(res["value"]) if res else 94.0
    except Exception:
        return 94.0


def get_support_url():
    res = settings_col.find_one({"key": "support_url"})
    url = res["value"] if res and res.get("value") else "https://t.me/tgtelehelpbot"
    if not url.startswith("http"):
        url = "https://" + url.replace("@", "t.me/")
    return url


def to_usd(inr):
    return round(inr / get_usdt_rate(), 2)


def is_user_banned(uid):
    res = users_col.find_one({"user_id": uid}, {"banned": 1})
    return bool(res and res.get("banned") == 1)


def update_balance(uid, amount):
    users_col.update_one({"user_id": uid}, {"$inc": {"balance": amount}})


def get_setting(key, default=None):
    res = settings_col.find_one({"key": key})
    return res["value"] if res else default


def set_setting(key, value):
    settings_col.update_one({"key": key}, {"$set": {"value": str(value)}}, upsert=True)


def get_user(uid, projection=None):
    return users_col.find_one({"user_id": uid}, projection)


def user_exists(uid):
    return users_col.find_one({"user_id": uid}, {"_id": 1}) is not None


COUNTRY_CODES = {
    "1": ("USA/Canada", "🇺🇸"),
    "7": ("Russia", "🇷🇺"),
    "20": ("Egypt", "🇪🇬"),
    "27": ("South Africa", "🇿🇦"),
    "31": ("Netherlands", "🇳🇱"),
    "32": ("Belgium", "🇧🇪"),
    "33": ("France", "🇫🇷"),
    "34": ("Spain", "🇪🇸"),
    "39": ("Italy", "🇮🇹"),
    "44": ("UK", "🇬🇧"),
    "46": ("Sweden", "🇸🇪"),
    "48": ("Poland", "🇵🇱"),
    "49": ("Germany", "🇩🇪"),
    "51": ("Peru", "🇵🇪"),
    "52": ("Mexico", "🇲🇽"),
    "54": ("Argentina", "🇦🇷"),
    "55": ("Brazil", "🇧🇷"),
    "56": ("Chile", "🇨🇱"),
    "57": ("Colombia", "🇨🇴"),
    "58": ("Venezuela", "🇻🇪"),
    "60": ("Malaysia", "🇲🇾"),
    "61": ("Australia", "🇦🇺"),
    "62": ("Indonesia", "🇮🇩"),
    "63": ("Philippines", "🇵🇭"),
    "66": ("Thailand", "🇹🇭"),
    "84": ("Vietnam", "🇻🇳"),
    "86": ("China", "🇨🇳"),
    "90": ("Turkey", "🇹🇷"),
    "91": ("India", "🇮🇳"),
    "92": ("Pakistan", "🇵🇰"),
    "93": ("Afghanistan", "🇦🇫"),
    "94": ("Sri Lanka", "🇱🇰"),
    "95": ("Myanmar", "🇲🇲"),
    "98": ("Iran", "🇮🇷"),
    "212": ("Morocco", "🇲🇦"),
    "213": ("Algeria", "🇩🇿"),
    "234": ("Nigeria", "🇳🇬"),
    "254": ("Kenya", "🇰🇪"),
    "255": ("Tanzania", "🇹🇿"),
    "380": ("Ukraine", "🇺🇦"),
    "880": ("Bangladesh", "🇧🇩"),
    "964": ("Iraq", "🇮🇶"),
    "966": ("Saudi Arabia", "🇸🇦"),
    "971": ("UAE", "🇦🇪"),
    "998": ("Uzbekistan", "🇺🇿"),
}


def get_flag_by_country_name(name):
    for code, (c_name, c_flag) in COUNTRY_CODES.items():
        if c_name == name:
            return c_flag
    try:
        row = custom_countries_col.find_one({"name": name}, {"flag": 1})
        if row:
            return row["flag"]
    except Exception:
        pass
    return "🌍"


def get_country_info(phone):
    phone = str(phone).replace(" ", "").replace("+", "")
    if not phone:
        return "Unknown", "🌍"

    try:
        customs = list(custom_countries_col.find({}))
        customs.sort(key=lambda x: len(x.get("code", "")), reverse=True)
        for c in customs:
            code = c.get("code", "")
            if code and phone.startswith(code):
                return c.get("name", "Unknown"), c.get("flag", "🌍")
    except Exception:
        pass

    for length in (3, 2, 1):
        prefix = phone[:length]
        if prefix in COUNTRY_CODES:
            return COUNTRY_CODES[prefix]
    return "Unknown", "🌍"


# ---- Thin query helpers used by plugins (replaces cur/db) ----

class _DB:
    """Compatibility shim: plugins that did `from database import cur, db` can use these methods."""

    @staticmethod
    def users():
        return users_col

    @staticmethod
    def settings():
        return settings_col

    @staticmethod
    def stock():
        return stock_col

    @staticmethod
    def auto_prices():
        return auto_prices_col

    @staticmethod
    def deposits():
        return deposits_col

    @staticmethod
    def upi_orders():
        return upi_orders_col

    @staticmethod
    def orders():
        return orders_col

    @staticmethod
    def custom_payments():
        return custom_payments_col

    @staticmethod
    def admins():
        return admins_col

    @staticmethod
    def custom_countries():
        return custom_countries_col

    @staticmethod
    def next_id(name):
        return _next_id(name)


# Expose collections directly for convenience
# (plugins will import these instead of cur/db)
db = _DB()
# Keep names some files expect after migration
cur = None  # removed — do not use
