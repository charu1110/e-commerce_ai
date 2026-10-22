"""Telegram bot: one bot for the whole class (python-telegram-bot v21, polling).

    python -m src.bot

Thin layer over src/bot_logic.py. Secrets come from .env only and are never logged.
"""
from __future__ import annotations

import logging
import os

import truststore
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src import db
from src.bot_logic import SEGMENT_LABELS, BotService
from src.engine.state import Deps

log = logging.getLogger("offer_bot")

COMMANDS = [
    ("start", "Become a customer and see who you are"),
    ("myoffer", "Check if there's an offer for you now"),
    ("persona", "Switch to a different customer"),
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
    cid = svc.linked_customer(chat_id)
    if cid:
        await reply(update, svc.who_card(cid))
        return
    await reply(update, HELP + "\n\n<b>First, who do you want to be?</b>", persona_buttons())


async def persona(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "<b>Who do you want to be?</b> Pick a shopper type:", persona_buttons())


async def myoffer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, decision_id = service(context).my_offer(update.effective_chat.id)
    await reply(update, text, offer_buttons(decision_id) if decision_id else None)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = service(context).set_opt_in(update.effective_chat.id, False)
    await reply(update, "🔕 Done. You won't get offers any more. /resume to turn them back on."
                if ok else "Tap /start first.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ok = service(context).set_opt_in(update.effective_chat.id, True)
    await reply(update, "🔔 Offers are back on. Try /myoffer." if ok else "Tap /start first.")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, f"Your chat id is <code>{update.effective_chat.id}</code>. "
                        "Put it in ADMIN_CHAT_IDS in .env to get admin commands.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, HELP)


# ---------------------------------------------------------------- buttons
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    svc, chat_id = service(context), update.effective_chat.id
    action, _, arg = (query.data or "").partition(":")

    if action == "persona":
        await query.answer("Finding you a customer…")
        cid = svc.link(chat_id, None if arg == "any" else arg)
        if cid is None:
            await query.message.reply_text("Sorry, no free customer of that type. Pick another.",
                                           reply_markup=persona_buttons())
            return
        await query.edit_message_reply_markup(None)
        await query.message.reply_text(svc.who_card(cid), parse_mode=ParseMode.HTML)
        return

    if not arg.isdigit():
        await query.answer()
        return
    result = svc.handle_button(chat_id, action, int(arg))
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
    log.info("Bot @%s is running. Press Ctrl+C to stop.", me.username)


def build_app(token: str, svc: BotService) -> Application:
    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["service"] = svc
    for name, fn in [("start", start), ("persona", persona), ("myoffer", myoffer), ("stop", stop),
                     ("resume", resume), ("whoami", whoami), ("help", help_cmd)]:
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
    conn = db.connect()
    log.info("Loading customers and rules…")
    svc = BotService(conn, Deps.load(conn, on_demand=True, channel="telegram"))
    log.info("Ready: %d customers, %d already linked to Telegram.", len(svc.segments),
             conn.execute("SELECT COUNT(*) FROM customers WHERE telegram_chat_id IS NOT NULL").fetchone()[0])
    build_app(token, svc).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
