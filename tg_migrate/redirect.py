"""Generic redirect bot — runs old bot tokens showing "bot moved" message."""

import asyncio
import logging
import os
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from .config import MigrateConfig, BotConfig, RedirectConfig
from .db import MigrationDB

log = logging.getLogger(__name__)


async def _redirect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Respond to any message with the redirect notice."""
    new_username = context.bot_data.get("new_username", "")
    new_link = context.bot_data.get("new_link", "")
    message_template = context.bot_data.get("message_template", "")
    db: Optional[MigrationDB] = context.bot_data.get("db")
    bot_name: str = context.bot_data.get("bot_name", "")

    text = message_template.format(
        new_username=new_username,
        new_link=new_link,
        bot_name=bot_name,
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Open → {new_username}", url=new_link)]
    ])

    await update.effective_message.reply_text(text, reply_markup=keyboard)

    # Track that user saw the redirect
    if db and update.effective_user:
        tg_id = str(update.effective_user.id)
        db.mark_redirect_seen(tg_id, bot_name)
        log.info(f"Redirect shown to {tg_id} → {new_username}")


async def run_redirect_bots(cfg: MigrateConfig, db: MigrationDB):
    """Start all redirect bots in one asyncio process."""
    apps = []

    for bot_cfg in cfg.bots:
        token = os.getenv(bot_cfg.old_token_env, "")
        if not token:
            log.warning(f"Skipping redirect for '{bot_cfg.name}': "
                        f"{bot_cfg.old_token_env} not set")
            continue

        app = ApplicationBuilder().token(token).build()
        app.bot_data["new_username"] = bot_cfg.new_username
        app.bot_data["new_link"] = bot_cfg.new_link
        app.bot_data["message_template"] = cfg.redirect.message
        app.bot_data["bot_name"] = bot_cfg.name
        app.bot_data["db"] = db

        app.add_handler(CommandHandler("start", _redirect_handler))
        app.add_handler(MessageHandler(filters.ALL, _redirect_handler))
        app.add_handler(CallbackQueryHandler(_redirect_handler))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=cfg.redirect.drop_pending
        )
        apps.append(app)
        log.info(f"✅ Redirect bot '{bot_cfg.name}' started → {bot_cfg.new_username}")

    if not apps:
        log.error("No redirect bots started. Check OLD_* tokens in .env")
        return

    log.info(f"Running {len(apps)} redirect bot(s). Ctrl+C to stop.")

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for app in apps:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        db.close()


def start_redirect(cfg: MigrateConfig, db: MigrationDB):
    """Entry point for redirect bots (blocking)."""
    asyncio.run(run_redirect_bots(cfg, db))
