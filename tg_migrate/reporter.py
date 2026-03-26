"""CLI and Telegram progress reports."""

import os
import asyncio
import logging
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from telegram import Bot

from .db import MigrationDB, STATUS_IMPORTED, STATUS_MIGRATED, STATUS_PROBED_OK, STATUS_PROBED_DEAD
from .config import MigrateConfig

log = logging.getLogger(__name__)
console = Console()

STATUS_EMOJI = {
    STATUS_IMPORTED: "⬜",
    STATUS_PROBED_OK: "✅",
    STATUS_PROBED_DEAD: "❌",
    STATUS_MIGRATED: "🟢",
}

STATUS_LABEL = {
    STATUS_IMPORTED: "Not probed",
    STATUS_PROBED_OK: "Reachable",
    STATUS_PROBED_DEAD: "Unreachable",
    STATUS_MIGRATED: "Migrated",
}


def _progress_bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def print_status(db: MigrationDB, cfg: MigrateConfig, detailed: bool = False):
    """Print migration status to terminal with rich formatting."""
    console.print()
    console.print(Panel(
        f"[bold]{cfg.project}[/bold] — Migration Status",
        box=box.DOUBLE,
        style="cyan",
    ))

    for bot_cfg in cfg.bots:
        stats = db.get_stats(bot_name=bot_cfg.name)
        total = stats["total"]
        if total == 0:
            console.print(f"\n  [dim]{bot_cfg.name}: no users imported[/dim]")
            continue

        by_status = stats["by_status"]
        ready = by_status.get(STATUS_PROBED_OK, 0) + by_status.get(STATUS_MIGRATED, 0)
        pct = stats["ready_pct"]

        console.print(f"\n  [bold]{bot_cfg.name}[/bold] → {bot_cfg.new_username}")
        console.print(f"  [{_progress_bar(pct)}] {pct:.0f}%  ({ready}/{total} reachable)")

        # Status breakdown
        for status, count in sorted(by_status.items(), key=lambda x: x[1], reverse=True):
            if count > 0:
                emoji = STATUS_EMOJI.get(status, "?")
                label = STATUS_LABEL.get(status, status)
                console.print(f"    {emoji} {label}: {count}")

        # Role breakdown
        if stats["by_role"]:
            console.print()
            for role, role_stats in stats["by_role"].items():
                r_total = role_stats["total"]
                r_ready = role_stats["migrated"] + role_stats["probed_ok"]
                r_pct = (r_ready / max(r_total, 1)) * 100
                console.print(f"    {role}: {r_ready}/{r_total} ({r_pct:.0f}%)")

        # Detailed user list
        if detailed:
            console.print()
            table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
            table.add_column("Status", width=4)
            table.add_column("Name", min_width=15)
            table.add_column("Username", min_width=12)
            table.add_column("Role", width=10)
            table.add_column("Error", max_width=30)

            users = db.get_users(bot_name=bot_cfg.name)
            for u in users:
                emoji = STATUS_EMOJI.get(u["status"], "?")
                name = u.get("name") or "—"
                username = f"@{u['username']}" if u.get("username") else ""
                role = u.get("role") or ""
                error = (u.get("error") or "")[:30]
                style = "dim" if u["status"] == STATUS_PROBED_DEAD else ""

                table.add_row(emoji, name, username, role, error, style=style)

            console.print(table)

    console.print()


def format_telegram_report(db: MigrationDB, cfg: MigrateConfig) -> str:
    """Format migration status as a Telegram message."""
    parts = [f"📊 *{cfg.project} — Migration Status*\n"]

    for bot_cfg in cfg.bots:
        stats = db.get_stats(bot_name=bot_cfg.name)
        total = stats["total"]
        if total == 0:
            continue

        by_status = stats["by_status"]
        ready = by_status.get(STATUS_PROBED_OK, 0) + by_status.get(STATUS_MIGRATED, 0)
        dead = by_status.get(STATUS_PROBED_DEAD, 0)
        pct = stats["ready_pct"]

        parts.append(f"*{bot_cfg.name}* → {bot_cfg.new_username}")
        parts.append(f"✅ Reachable: {ready}/{total} ({pct:.0f}%)")
        parts.append(f"❌ Unreachable: {dead}")

        # Role breakdown
        for role, role_stats in stats["by_role"].items():
            r_total = role_stats["total"]
            r_ready = role_stats["migrated"] + role_stats["probed_ok"]
            parts.append(f"  {role}: {r_ready}/{r_total}")

        parts.append("")

    # Who's still missing?
    for bot_cfg in cfg.bots:
        dead_users = db.get_users(bot_name=bot_cfg.name, status=STATUS_PROBED_DEAD)
        if dead_users:
            parts.append(f"⏳ *Waiting ({bot_cfg.name}):*")
            for u in dead_users[:15]:
                name = u.get("name") or "—"
                parts.append(f"  • {name}")
            if len(dead_users) > 15:
                parts.append(f"  ...and {len(dead_users) - 15} more")
            parts.append("")

    return "\n".join(parts)


async def _send_telegram(token: str, chat_id: str, text: str):
    bot = Bot(token=token)
    await bot.send_message(
        chat_id=int(chat_id),
        text=text,
        parse_mode="Markdown",
    )


def notify_telegram(db: MigrationDB, cfg: MigrateConfig):
    """Send migration status report to Telegram alert channel."""
    token = os.getenv(cfg.alerts.bot_token_env, "")
    chat_id = os.getenv(cfg.alerts.chat_id_env, "")

    if not token or not chat_id:
        log.error("Alerts not configured: set alerts.bot_token_env and alerts.chat_id_env")
        return

    text = format_telegram_report(db, cfg)
    asyncio.run(_send_telegram(token, chat_id, text))
    log.info(f"Report sent to chat {chat_id}")
