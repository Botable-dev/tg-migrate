"""User migration tracking — probe and status management."""

import asyncio
import logging
import os
from typing import Optional

from telegram import Bot
from telegram.error import BadRequest, Forbidden, NetworkError

from .config import MigrateConfig, BotConfig, ProbeConfig
from .db import MigrationDB, STATUS_PROBED_OK, STATUS_PROBED_DEAD

log = logging.getLogger(__name__)

# Permanent errors — retrying is futile
PERMANENT_ERRORS = [
    "chat not found",
    "bot was blocked",
    "user is deactivated",
    "forbidden",
]


def _is_permanent(error: Exception) -> bool:
    err_lower = str(error).lower()
    return any(phrase in err_lower for phrase in PERMANENT_ERRORS)


async def _probe_one_getchat(bot: Bot, tg_id: str) -> tuple[bool, str]:
    """Probe via getChat — silent, no message sent."""
    try:
        chat = await bot.get_chat(chat_id=int(tg_id))
        return True, f"ok: {chat.first_name or ''}"
    except (BadRequest, Forbidden) as e:
        if _is_permanent(e):
            return False, str(e)
        raise
    except Exception as e:
        return False, str(e)


async def _probe_one_send(bot: Bot, tg_id: str, message: str) -> tuple[bool, str]:
    """Probe via send_message — sends a test message."""
    try:
        msg = await bot.send_message(chat_id=int(tg_id), text=message)
        # Delete the test message immediately
        try:
            await bot.delete_message(chat_id=int(tg_id), message_id=msg.message_id)
        except Exception:
            pass
        return True, "ok: message sent"
    except (BadRequest, Forbidden) as e:
        if _is_permanent(e):
            return False, str(e)
        raise
    except Exception as e:
        return False, str(e)


async def _probe_batch(
    bot: Bot,
    users: list[dict],
    db: MigrationDB,
    bot_name: str,
    probe_cfg: ProbeConfig,
    on_progress=None,
) -> dict:
    """Probe a batch of users with concurrency control."""
    semaphore = asyncio.Semaphore(probe_cfg.concurrency)
    delay = probe_cfg.delay_ms / 1000.0
    stats = {"ok": 0, "dead": 0, "error": 0, "total": len(users)}

    async def probe_one(user: dict):
        tg_id = user["tg_id"]
        async with semaphore:
            try:
                if probe_cfg.method == "send_message":
                    ok, detail = await _probe_one_send(bot, tg_id, probe_cfg.test_message)
                else:
                    ok, detail = await _probe_one_getchat(bot, tg_id)

                if ok:
                    db.set_status(tg_id, bot_name, STATUS_PROBED_OK)
                    stats["ok"] += 1
                else:
                    db.set_status(tg_id, bot_name, STATUS_PROBED_DEAD, error=detail)
                    stats["dead"] += 1

            except NetworkError as e:
                log.warning(f"Network error probing {tg_id}: {e}")
                stats["error"] += 1
            except Exception as e:
                log.error(f"Unexpected error probing {tg_id}: {e}")
                db.set_status(tg_id, bot_name, STATUS_PROBED_DEAD, error=str(e))
                stats["dead"] += 1

            if on_progress:
                done = stats["ok"] + stats["dead"] + stats["error"]
                on_progress(done, stats["total"])

            if delay > 0:
                await asyncio.sleep(delay)

    tasks = [probe_one(u) for u in users]
    await asyncio.gather(*tasks)
    return stats


def probe_users(
    cfg: MigrateConfig,
    db: MigrationDB,
    bot_name: Optional[str] = None,
    on_progress=None,
) -> dict:
    """Probe all imported users for each bot. Returns aggregated stats."""
    results = {}

    for bot_cfg in cfg.bots:
        if bot_name and bot_cfg.name != bot_name:
            continue

        token = os.getenv(bot_cfg.new_token_env, "")
        if not token:
            log.warning(f"Skipping {bot_cfg.name}: {bot_cfg.new_token_env} not set")
            continue

        users = db.get_users(bot_name=bot_cfg.name)
        if not users:
            log.info(f"No users to probe for bot '{bot_cfg.name}'")
            continue

        log.info(f"Probing {len(users)} users for bot '{bot_cfg.name}'...")
        bot = Bot(token=token)
        stats = asyncio.run(_probe_batch(
            bot, users, db, bot_cfg.name, cfg.probe, on_progress
        ))
        results[bot_cfg.name] = stats

    return results
