"""tg-migrate CLI — General-purpose Telegram bot migration tool.

Commands:
    init      — Create migrate.yaml + SQLite DB
    import    — Load users from CSV/JSON/Supabase
    redirect  — Start redirect bots (old tokens → new bots)
    cutover   — Swap OLD→NEW tokens in .env
    probe     — Check which users can receive messages from new bots
    status    — Show migration progress
    doctor    — Scan code for known migration anti-patterns
    cleanup   — Remove migration artifacts
"""

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box

from . import __version__
from .config import load_config, MigrateConfig, EXAMPLE_CONFIG, CONFIG_FILE
from .db import MigrationDB

console = Console()


def _load(config_path: str | None, env_file: str | None = None) -> tuple[MigrateConfig, MigrationDB]:
    """Load config + open DB. Loads .env from config's env_file path."""
    cfg = load_config(config_path)
    env = env_file or cfg.env_file
    if Path(env).exists():
        load_dotenv(env, override=True)
    db = MigrationDB(cfg.db_path)
    return cfg, db


@click.group()
@click.version_option(version=__version__, prog_name="tg-migrate")
def main():
    """🔄 tg-migrate — Telegram bot token migration tool."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# ── init ──

@main.command()
@click.option("--force", is_flag=True, help="Overwrite existing config")
def init(force):
    """Create migrate.yaml and initialize the SQLite database."""
    config_path = Path(CONFIG_FILE)
    if config_path.exists() and not force:
        console.print(f"[yellow]⚠ {CONFIG_FILE} already exists. Use --force to overwrite.[/yellow]")
        return

    now = datetime.now(timezone.utc).isoformat()
    content = EXAMPLE_CONFIG.format(date=now)
    config_path.write_text(content)
    console.print(f"[green]✅ Created {CONFIG_FILE}[/green]")

    # Init DB
    db = MigrationDB()
    db.set_meta("created_at", now)
    db.close()
    console.print(f"[green]✅ Created tg_migrate.db[/green]")
    console.print()
    console.print("[dim]Next: edit migrate.yaml, then run 'tg-migrate import'[/dim]")


# ── import ──

@main.command("import")
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("--env", "env_file", default=None, help=".env file path")
@click.option("--file", "import_file", default=None, help="Override import file path")
@click.option("--bot", "bot_name", default=None, help="Assign all users to this bot")
def import_users(config_path, env_file, import_file, bot_name):
    """Load users from CSV, JSON, or Supabase into the migration database."""
    cfg, db = _load(config_path, env_file)
    source = cfg.users.source
    filepath = import_file or cfg.users.file

    if source == "csv":
        _import_csv(db, cfg, filepath, bot_name)
    elif source == "json":
        _import_json(db, cfg, filepath, bot_name)
    elif source == "supabase":
        _import_supabase(db, cfg, bot_name)
    else:
        console.print(f"[red]Unknown source: {source}[/red]")
        return

    # Show summary
    for bot_cfg in cfg.bots:
        users = db.get_users(bot_name=bot_cfg.name)
        console.print(f"  [green]{bot_cfg.name}[/green]: {len(users)} users")

    db.close()


def _import_csv(db: MigrationDB, cfg: MigrateConfig, filepath: str, bot_override: str | None):
    console.print(f"[cyan]Importing from CSV: {filepath}[/cyan]")
    # Try utf-8-sig first (handles Windows BOM), fall back to utf-8
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with open(filepath, newline="", encoding=encoding) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            # Verify tg_id column exists
            if rows and cfg.users.tg_id_col in rows[0]:
                break
        except (UnicodeDecodeError, KeyError):
            continue
    else:
        console.print(f"[red]Could not parse CSV: column '{cfg.users.tg_id_col}' not found[/red]")
        return

    count = 0
    for row in rows:
        tg_id = str(row.get(cfg.users.tg_id_col, "")).strip()
        if not tg_id:
            continue
        name = row.get(cfg.users.name_col, "")
        username = row.get(cfg.users.username_col, "")
        role = row.get(cfg.users.role_col, "")
        bot = bot_override or row.get(cfg.users.bot_col, cfg.bots[0].name if cfg.bots else "main")
        db.upsert_user(tg_id, bot, name=name, username=username, role=role)
        count += 1
    console.print(f"[green]✅ Imported {count} users from CSV[/green]")


def _import_json(db: MigrationDB, cfg: MigrateConfig, filepath: str, bot_override: str | None):
    console.print(f"[cyan]Importing from JSON: {filepath}[/cyan]")
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        data = data.get("users", data.get("data", []))

    count = 0
    for item in data:
        tg_id = str(item.get(cfg.users.tg_id_col, "")).strip()
        if not tg_id:
            continue
        name = item.get(cfg.users.name_col, "")
        username = item.get(cfg.users.username_col, "")
        role = item.get(cfg.users.role_col, "")
        bot = bot_override or item.get(cfg.users.bot_col, cfg.bots[0].name if cfg.bots else "main")
        db.upsert_user(tg_id, bot, name=name, username=username, role=role)
        count += 1
    console.print(f"[green]✅ Imported {count} users from JSON[/green]")


def _import_supabase(db: MigrationDB, cfg: MigrateConfig, bot_override: str | None):
    try:
        from supabase import create_client
    except ImportError:
        console.print("[red]Install supabase: pip install supabase[/red]")
        return

    url = os.getenv(cfg.users.supabase_url_env, "")
    key = os.getenv(cfg.users.supabase_key_env, "")
    if not url or not key:
        console.print(f"[red]Set {cfg.users.supabase_url_env} and {cfg.users.supabase_key_env}[/red]")
        return

    sb = create_client(url, key)
    total = 0
    PAGE_SIZE = 1000  # Supabase default limit — must paginate!

    for tbl in cfg.users.tables:
        table_name = tbl["table"]
        tg_col = tbl.get("tg_id_col", "tg_id")
        name_col = tbl.get("name_col", "name")
        role = tbl.get("role", "")
        bot = bot_override or tbl.get("bot", cfg.bots[0].name if cfg.bots else "main")

        console.print(f"[cyan]Importing from Supabase table: {table_name}[/cyan]")

        # Paginate to avoid 1000-row default limit
        all_rows = []
        offset = 0
        while True:
            batch = sb.table(table_name).select("*").range(offset, offset + PAGE_SIZE - 1).execute().data
            all_rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        count = 0
        for row in all_rows:
            tg_id = str(row.get(tg_col, "")).strip()
            if not tg_id or tg_id == "None":
                continue
            name = row.get(name_col, "")
            username = row.get("tg_username", "")
            db.upsert_user(tg_id, bot, name=name, username=username, role=role)
            count += 1

        console.print(f"  [green]{table_name}: {count} users with tg_id[/green]")
        total += count

    console.print(f"[green]✅ Imported {total} users from Supabase[/green]")


# ── redirect ──

@main.command()
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("--env", "env_file", default=None, help=".env file path")
def redirect(config_path, env_file):
    """Start redirect bots for old tokens (shows 'bot moved' message)."""
    cfg, db = _load(config_path, env_file)
    from .redirect import start_redirect
    start_redirect(cfg, db)


# ── cutover ──

@main.command()
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("--env", "env_file", default=None, help=".env file path")
@click.option("--apply", is_flag=True, help="Actually apply changes (default is dry-run)")
def cutover(config_path, env_file, apply):
    """Swap OLD→NEW tokens in .env with backup."""
    cfg, _ = _load(config_path, env_file)
    from .cutover import cutover_env

    diff = cutover_env(cfg, apply=apply)

    if diff.get("swaps"):
        table = Table(title="Token Swaps", box=box.SIMPLE)
        table.add_column("Key")
        table.add_column("Old Value")
        table.add_column("New Value")
        for swap in diff["swaps"]:
            table.add_row(swap["key"], swap["old"], swap["new"])
        console.print(table)

    if diff.get("removes"):
        console.print(f"\n[dim]Lines to remove: {', '.join(diff['removes'])}[/dim]")

    if apply:
        console.print(f"\n[green]✅ Cutover applied. Backup: {diff.get('backup')}[/green]")
        console.print("[dim]Next: docker compose down && docker compose up -d --build[/dim]")
    else:
        console.print("\n[yellow]DRY RUN — add --apply to execute[/yellow]")


# ── probe ──

@main.command()
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("--env", "env_file", default=None, help=".env file path")
@click.option("--bot", "bot_name", default=None, help="Only probe this bot's users")
@click.option("--method", type=click.Choice(["getChat", "send_message"]), default=None,
              help="Override probe method")
def probe(config_path, env_file, bot_name, method):
    """Check which users can receive messages from new bots."""
    cfg, db = _load(config_path, env_file)

    if method:
        cfg.probe.method = method

    console.print(f"[cyan]Probing with method: {cfg.probe.method}[/cyan]")

    from .tracker import probe_users

    def on_progress(done, total):
        pct = (done / max(total, 1)) * 100
        sys.stdout.write(f"\r  Progress: {done}/{total} ({pct:.0f}%)")
        sys.stdout.flush()

    results = probe_users(cfg, db, bot_name=bot_name, on_progress=on_progress)
    console.print()

    for name, stats in results.items():
        console.print(f"\n  [bold]{name}[/bold]: {stats['ok']} reachable, "
                      f"{stats['dead']} unreachable, {stats['error']} errors "
                      f"(of {stats['total']})")

    db.close()
    console.print("\n[dim]Run 'tg-migrate status' for full report[/dim]")


# ── status ──

@main.command()
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("--env", "env_file", default=None, help=".env file path")
@click.option("--detailed", "-d", is_flag=True, help="Show per-user details")
@click.option("--watch", "-w", type=int, default=0, help="Auto-refresh interval (seconds)")
@click.option("--notify", is_flag=True, help="Send report to Telegram")
def status(config_path, env_file, detailed, watch, notify):
    """Show migration progress."""
    cfg, db = _load(config_path, env_file)
    from .reporter import print_status, notify_telegram

    if notify:
        notify_telegram(db, cfg)
        console.print("[green]✅ Report sent to Telegram[/green]")
        db.close()
        return

    if watch > 0:
        try:
            sys.stdout.write("\033[?25l")  # hide cursor
            while True:
                sys.stdout.write("\033[H\033[J")  # clear
                sys.stdout.flush()
                print_status(db, cfg, detailed=detailed)
                console.print(f"[dim]  Refreshing in {watch}s (Ctrl+C to stop)[/dim]")
                time.sleep(watch)
        except KeyboardInterrupt:
            pass
        finally:
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()
    else:
        print_status(db, cfg, detailed=detailed)

    db.close()


# ── doctor ──

@main.command()
@click.argument("path", default=".")
@click.option("--json-output", is_flag=True, help="Output findings as JSON")
def doctor(path, json_output):
    """Scan code for known migration anti-patterns.

    Based on 9 real edge cases from production migrations.
    Checks for: infinite retry, recipient loop crashes, NULL dedup,
    stale callback errors, session destruction, double query.answer().
    """
    from .healthcheck import scan_directory

    findings = scan_directory(path)

    if json_output:
        import json as json_mod
        click.echo(json_mod.dumps(
            [{"check": f.check, "severity": f.severity, "file": f.file,
              "line": f.line, "message": f.message, "fix": f.fix}
             for f in findings],
            indent=2,
        ))
        return

    if not findings:
        console.print("[green]✅ No migration anti-patterns found![/green]")
        return

    severity_style = {
        "critical": "red bold",
        "warning": "yellow",
        "info": "dim",
    }

    console.print(f"\n[bold]Found {len(findings)} potential issue(s):[/bold]\n")

    for f in sorted(findings, key=lambda x: ("critical", "warning", "info").index(x.severity)):
        style = severity_style.get(f.severity, "")
        console.print(f"  [{style}]{f.severity.upper()}[/{style}] {f.check}")
        console.print(f"    {f.file}:{f.line}")
        console.print(f"    {f.message}")
        console.print(f"    [dim]Fix: {f.fix[:100]}...[/dim]" if len(f.fix) > 100 else f"    [dim]Fix: {f.fix}[/dim]")
        console.print()


# ── cleanup ──

@main.command()
@click.option("-c", "--config", "config_path", default=None, help="Config file path")
@click.option("--env", "env_file", default=None, help=".env file path")
@click.option("--apply", is_flag=True, help="Actually remove artifacts")
def cleanup(config_path, env_file, apply):
    """Remove migration artifacts (OLD_* tokens, redirect config, DB)."""
    cfg, db = _load(config_path, env_file)

    artifacts = []

    # Check for OLD_* env vars
    env_path = Path(cfg.env_file)
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("OLD_"):
                    artifacts.append(f".env: {line.strip()[:50]}")

    # Check for DB
    db_path = Path(cfg.db_path)
    if db_path.exists():
        artifacts.append(f"DB: {db_path} ({db_path.stat().st_size / 1024:.1f} KB)")

    if not artifacts:
        console.print("[green]✅ Nothing to clean up![/green]")
        return

    console.print("[bold]Migration artifacts found:[/bold]")
    for a in artifacts:
        console.print(f"  • {a}")

    if not apply:
        console.print("\n[yellow]DRY RUN — add --apply to remove[/yellow]")
        return

    # Remove OLD_* from .env
    if env_path.exists():
        with open(env_path) as f:
            lines = f.readlines()
        cleaned = [l for l in lines if not l.strip().startswith("OLD_")]
        with open(env_path, "w") as f:
            f.writelines(cleaned)
        console.print("[green]✅ Removed OLD_* vars from .env[/green]")

    console.print("\n[dim]Keeping DB for audit. Delete manually: rm tg_migrate.db[/dim]")
    db.close()


if __name__ == "__main__":
    main()
