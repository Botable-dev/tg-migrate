"""YAML config loader and validation."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


CONFIG_FILE = "migrate.yaml"


@dataclass
class BotConfig:
    name: str
    old_token_env: str
    new_token_env: str
    new_username: str = ""
    new_link: str = ""

    def __post_init__(self):
        if not self.new_link and self.new_username:
            clean = self.new_username.lstrip("@")
            self.new_link = f"https://t.me/{clean}"


@dataclass
class UserSource:
    source: str = "csv"               # csv | json | supabase | manual
    file: str = ""
    tg_id_col: str = "tg_id"
    name_col: str = "name"
    username_col: str = "username"
    role_col: str = "role"
    bot_col: str = "bot"              # column mapping user → bot name
    # Supabase-specific
    supabase_url_env: str = "SUPABASE_URL"
    supabase_key_env: str = "SUPABASE_KEY"
    tables: list = field(default_factory=list)


@dataclass
class AlertConfig:
    bot_token_env: str = ""
    chat_id_env: str = ""


@dataclass
class ProbeConfig:
    method: str = "getChat"           # getChat | send_message
    test_message: str = "🔄 Migration check — please ignore."
    concurrency: int = 5              # parallel probe requests
    delay_ms: int = 100               # delay between probes (rate limit)


@dataclass
class RedirectConfig:
    message: str = (
        "🔄 This bot has moved!\n\n"
        "Please open the new bot and press /start:\n"
        "👉 {new_username}\n\n"
        "All notifications are now sent there."
    )
    drop_pending: bool = True


@dataclass
class MigrateConfig:
    project: str = "my-bot"
    migration_date: str = ""
    db_path: str = "tg_migrate.db"
    env_file: str = ".env"
    bots: list[BotConfig] = field(default_factory=list)
    users: UserSource = field(default_factory=UserSource)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    redirect: RedirectConfig = field(default_factory=RedirectConfig)


def load_config(path: Optional[str] = None) -> MigrateConfig:
    """Load and validate migrate.yaml."""
    config_path = Path(path or CONFIG_FILE)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found: {config_path}\n"
            f"Run 'tg-migrate init' to create one."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    cfg = MigrateConfig(
        project=raw.get("project", "my-bot"),
        migration_date=raw.get("migration_date", ""),
        db_path=raw.get("db_path", "tg_migrate.db"),
        env_file=raw.get("env_file", ".env"),
    )

    # Bots
    for b in raw.get("bots", []):
        cfg.bots.append(BotConfig(
            name=b["name"],
            old_token_env=b.get("old_token_env", ""),
            new_token_env=b.get("new_token_env", ""),
            new_username=b.get("new_username", ""),
            new_link=b.get("new_link", ""),
        ))

    # Users
    u = raw.get("users", {})
    if u:
        cfg.users = UserSource(
            source=u.get("source", "csv"),
            file=u.get("file", ""),
            tg_id_col=u.get("tg_id_col", "tg_id"),
            name_col=u.get("name_col", "name"),
            username_col=u.get("username_col", "username"),
            role_col=u.get("role_col", "role"),
            bot_col=u.get("bot_col", "bot"),
            supabase_url_env=u.get("supabase_url_env", "SUPABASE_URL"),
            supabase_key_env=u.get("supabase_key_env", "SUPABASE_KEY"),
            tables=u.get("tables", []),
        )

    # Alerts
    a = raw.get("alerts", {})
    if a:
        cfg.alerts = AlertConfig(
            bot_token_env=a.get("bot_token_env", ""),
            chat_id_env=a.get("chat_id_env", ""),
        )

    # Probe
    p = raw.get("probe", {})
    if p:
        cfg.probe = ProbeConfig(
            method=p.get("method", "getChat"),
            test_message=p.get("test_message", cfg.probe.test_message),
            concurrency=p.get("concurrency", 5),
            delay_ms=p.get("delay_ms", 100),
        )

    # Redirect
    r = raw.get("redirect", {})
    if r:
        cfg.redirect = RedirectConfig(
            message=r.get("message", cfg.redirect.message),
            drop_pending=r.get("drop_pending", True),
        )

    return cfg


EXAMPLE_CONFIG = """\
# tg-migrate configuration
# See: https://github.com/Botable-dev/tg-migrate

project: my-bot
migration_date: "{date}"
db_path: tg_migrate.db
env_file: .env

# Bot definitions — old token → new token mapping
bots:
  - name: main
    old_token_env: OLD_TELEGRAM_BOT_TOKEN
    new_token_env: TELEGRAM_BOT_TOKEN
    new_username: "@MyNewBot"

  # Add more bots if needed:
  # - name: admin
  #   old_token_env: OLD_ADMIN_BOT_TOKEN
  #   new_token_env: ADMIN_BOT_TOKEN
  #   new_username: "@MyNewAdminBot"

# Where to load users from
users:
  source: csv                   # csv | json | supabase
  file: users.csv               # CSV with columns: tg_id, name, role, bot
  tg_id_col: tg_id
  name_col: name
  role_col: role
  bot_col: bot                  # maps user to bot name

  # Supabase import (alternative):
  # source: supabase
  # supabase_url_env: SUPABASE_URL
  # supabase_key_env: SUPABASE_KEY
  # tables:
  #   - table: students
  #     tg_id_col: tg_id
  #     name_col: name
  #     role: student
  #     bot: main
  #   - table: staff
  #     tg_id_col: tg_id
  #     name_col: name
  #     role: teacher
  #     bot: main

# Probe settings — how to check if users can receive messages
probe:
  method: getChat               # getChat (silent) | send_message (sends a test msg)
  test_message: "🔄 Migration check — please ignore."
  concurrency: 5
  delay_ms: 100

# Redirect bot message template
redirect:
  message: |
    🔄 This bot has moved!

    Please open the new bot and press /start:
    👉 {{new_username}}

    All notifications are now sent there.
  drop_pending: true

# Telegram alerts (migration progress to admin chat)
alerts:
  bot_token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: ADMIN_CHAT_ID
"""
