"""Telegram bot: one bot for the whole class (python-telegram-bot v21, polling).

    python -m src.bot

Thin layer over src/bot_logic.py. Secrets come from .env only and are never logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

import truststore
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src import db
from src.agent import make_agent
from src.bot_logic import SEGMENT_LABELS, BotService
from src.engine.state import Deps

log = logging.getLogger("offer_bot")

COMMANDS = [
    ("start", "Become a customer and see who you are"),
    ("myoffer", "Check if there's an offer for you now"),
    ("persona", "Switch to a different customer"),
    ("why", "Why did I get (or not get) my last offer?"),
    ("history", "Your last 5 offers"),
    ("timetravel", "Demo: jump N days ahead, e.g. /timetravel 4"),
    ("stop", "Stop receiving offers"),
    ("resume", "Start receiving offers again"),
    ("whoami", "Show your Telegram chat id"),
    ("help", "What this bot can do"),
]
HELP = ("🛍 <b>Offer Engine demo bot</b>\n\n"
        "You play a (synthetic) customer of an online store. Our engine decides whether an offer "
        "would really change what you do, picks one within strict rules, and sends it here.\n\n"
        + "\n".join(f"/{c} – {d}" for c, d in COMMANDS))


def service(context: ContextTypes.DEFAULT_TYPE) -> BotService:
    return context.application.bot_data["service"]


_db_lock = threading.Lock()


async def call(fn, *args):
    """Run engine work in a worker thread (AI calls take seconds) without blocking other chats.

    One lock serialises database access, since SQLite allows one writer at a time.
    """
    def run():
        with _db_lock:
            return fn(*args)
    return await asyncio.to_thread(run)


def offer_buttons(decision_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Claim 🎁", callback_data=f"claim:{decision_id}"),
        InlineKeyboardButton("Not now", callback_data=f"dismiss:{decision_id}"),
    ], [InlineKeyboardButton("Why this offer?", callback_data=f"why:{decision_id}")]])


def persona_buttons() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(label, callback_data=f"persona:{seg}")] for seg, label in SEGMENT_LABELS.items()]
    rows.append([InlineKeyboardButton("🎲 Surprise me", callback_data="persona:any")])
    return InlineKeyboardMarkup(rows)


async def reply(update: Update, text: str, markup=None) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ---------------------------------------------------------------- commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc, chat_id = service(context), update.effective_chat.id
    cid = await call(svc.linked_customer, chat_id)
    if cid:
        await reply(update, await call(svc.who_card, cid))
        return
    await reply(update, HELP + "\n\n<b>First, who do you want to be?</b>", persona_buttons())


async def persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "<b>Who do you want to be?</b> Pick a shopper type:", persona_buttons())


async def myoffer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text, decision_id = await call(service(context).my_offer, update.effective_chat.id)
    await reply(update, text, offer_buttons(decision_id) if decision_id else None)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = await call(service(context).set_opt_in, update.effective_chat.id, False)
    await reply(update, "🔕 Done. You won't get offers any more. /resume to turn them back on."
                if ok else "Tap /start first.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = await call(service(context).set_opt_in, update.effective_chat.id, True)
    await reply(update, "🔔 Offers are back on. Try /myoffer." if ok else "Tap /start first.")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, f"Your chat id is <code>{update.effective_chat.id}</code>. "
                        "Put it in ADMIN_CHAT_IDS in .env to get admin commands.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, HELP)


async def why(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, await call(service(context).why_last, update.effective_chat.id))


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, await call(service(context).history, update.effective_chat.id))


async def timetravel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await reply(update, "Usage: /timetravel 4 (jump 4 days ahead), /timetravel 0 to come back.")
        return
    await reply(update, await call(service(context).time_travel, update.effective_chat.id, int(context.args[0])))


TICK_SECONDS = 600


async def ticker(app: Application) -> None:
    """Every 10 minutes: expire offers, deliver offers held back by quiet hours, push follow-ups."""
    svc: BotService = app.bot_data["service"]
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            pushes = await call(svc.tick)
            for chat_id, card, decision_id in pushes:
                await app.bot.send_message(chat_id, card, parse_mode=ParseMode.HTML,
                                           reply_markup=offer_buttons(decision_id))
            if pushes:
                log.info("Scheduler pushed %d offer(s).", len(pushes))
        except Exception:  # keep the ticker alive whatever happens
            log.exception("Scheduler tick failed")


# ---------------------------------------------------------------- admin (ADMIN_CHAT_IDS only)
def admin_buttons(paused: bool) -> InlineKeyboardMarkup:
    toggle = ("▶️ Resume all offers", "admin:resume") if paused else ("⏸ Pause all offers", "admin:pause")
    return InlineKeyboardMarkup([[InlineKeyboardButton(toggle[0], callback_data=toggle[1]),
                                  InlineKeyboardButton("🔄 Refresh", callback_data="admin:refresh")]])


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = service(context)
    if not svc.is_admin(update.effective_chat.id):
        await reply(update, "This command is for admins only.")
        return
    text = await call(svc.stats_text)
    await reply(update, text, admin_buttons(await call(db.is_paused, svc.conn)))


async def audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = service(context)
    if not svc.is_admin(update.effective_chat.id):
        await reply(update, "This command is for admins only.")
        return
    if not context.args:
        await reply(update, "Usage: /audit C00123")
        return
    await reply(update, await call(svc.audit_text, context.args[0]))


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE, paused: bool = True) -> None:
    svc = service(context)
    if not svc.is_admin(update.effective_chat.id):
        await reply(update, "This command is for admins only.")
        return
    await call(svc.set_paused, paused)
    await reply(update, "⏸ All offers are paused (kill switch on)." if paused else "▶️ Offers are running again.")


async def unpause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await pause(update, context, paused=False)


# ---------------------------------------------------------------- buttons
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    svc, chat_id = service(context), update.effective_chat.id
    action, _, arg = (query.data or "").partition(":")

    if action == "admin":
        if not svc.is_admin(chat_id):
            await query.answer("Admins only.")
            return
        if arg in ("pause", "resume"):
            await call(svc.set_paused, arg == "pause")
        await query.answer({"pause": "Paused", "resume": "Resumed"}.get(arg, "Refreshed"))
        paused = await call(db.is_paused, svc.conn)
        await query.edit_message_text(await call(svc.stats_text), parse_mode=ParseMode.HTML,
                                      reply_markup=admin_buttons(paused))
        return

    if action == "persona":
        await query.answer("Finding you a customer…")
        cid = await call(svc.link, chat_id, None if arg == "any" else arg)
        if cid is None:
            await query.message.reply_text("Sorry, no free customer of that type. Pick another.",
                                           reply_markup=persona_buttons())
            return
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(await call(svc.who_card, cid), parse_mode=ParseMode.HTML)
        return

    if not arg.isdigit():
        await query.answer()
        return
    result = await call(svc.handle_button, chat_id, action, int(arg))
    await query.answer(result.toast)
    if result.append_to_card:
        await query.edit_message_text(query.message.text_html + "\n\n" + result.append_to_card,
                                      parse_mode=ParseMode.HTML)
    elif result.remove_buttons:
        await query.edit_message_reply_markup(None)
    if result.reply:
        await query.message.reply_text(result.reply, parse_mode=ParseMode.HTML)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Handler error: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Oops, something went wrong on our side. Please try again.")


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    me = await app.bot.get_me()
    app.bot_data["ticker"] = asyncio.create_task(ticker(app))
    log.info("Bot @%s is running (scheduler every %d min). Press Ctrl+C to stop.", me.username, TICK_SECONDS // 60)


def build_app(token: str, svc: BotService) -> Application:
    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["service"] = svc
    for name, fn in [("start", start), ("persona", persona), ("myoffer", myoffer), ("stop", stop),
                     ("resume", resume), ("whoami", whoami), ("help", help_cmd), ("why", why),
                     ("history", history), ("timetravel", timetravel), ("admin", admin), ("audit", audit),
                     ("pause", pause),
                     ("unpause", unpause)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    truststore.inject_into_ssl()  # trust the Windows certificate store (antivirus / proxy SSL)
    load_dotenv()
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request logs would include the bot token
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
    conn = db.connect(check_same_thread=False)  # used from worker threads, guarded by _db_lock
    agent, agent_name = make_agent("auto")
    if agent_name == "stub":
        log.warning("No ANTHROPIC_API_KEY in .env: offers come from the rule-based agent.")
    else:
        log.info("Offer agent: %s", agent_name)
    log.info("Loading customers and rules…")
    admin_ids = {int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
    if not admin_ids:
        log.warning("ADMIN_CHAT_IDS is empty: /admin, /audit and /pause are disabled. Use /whoami to get your id.")
    svc = BotService(conn, Deps.load(conn, agent=agent, agent_name=agent_name, on_demand=True, channel="telegram"),
                     admin_ids=admin_ids)
    log.info("Ready: %d customers, %d already linked to Telegram.", len(svc.segments),
             conn.execute("SELECT COUNT(*) FROM customers WHERE telegram_chat_id IS NOT NULL").fetchone()[0])
    build_app(token, svc).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
