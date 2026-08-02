import os
import asyncio
import logging
from aiohttp import web
from config import BOT_TOKEN, API_ID, API_HASH, bot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.makedirs("sessions", exist_ok=True)

# Ensure MongoDB indexes + default settings exist BEFORE any handlers run
from database import setup_db
setup_db()
logger.info("MongoDB setup_db() completed (indexes + defaults)")

from plugins import register_all_handlers

PORT = int(os.getenv("PORT", "10000"))


async def health(_request):
    return web.Response(text="Numbott Telethon is running ✅", content_type="text/plain")


async def start_web_server():
    """Minimal HTTP server so platforms treat this as a free web service."""
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server listening on 0.0.0.0:{PORT}")


async def _order_sweeper():
    """Background loop: release stuck unpaid stock / refund timed-out orders."""
    from utils.states import cleanup_expired_orders
    from config import AUTO_CANCEL_SECONDS
    while True:
        try:
            await cleanup_expired_orders(max_age_seconds=AUTO_CANCEL_SECONDS + 30)
        except Exception as e:
            logger.exception("Order sweeper error: %s", e)
        try:
            from plugins.whatsapp import cleanup_expired_wa_orders
            await cleanup_expired_wa_orders()
        except Exception as e:
            logger.exception("WA order sweeper error: %s", e)
        await asyncio.sleep(60)


async def main():
    await start_web_server()
    asyncio.create_task(_order_sweeper())
    print("✅ Numbott Modular (Telethon + MongoDB) STARTED SUCCESSFULLY")
    print(f"🌐 Web service on port {PORT} (free-tier friendly)")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    bot.start(bot_token=BOT_TOKEN)
    register_all_handlers(bot)

    from telethon import events

    @bot.on(events.CallbackQuery)
    async def debug_cb(e):
        logger.warning(f"CALLBACK DATA: {e.data}")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
