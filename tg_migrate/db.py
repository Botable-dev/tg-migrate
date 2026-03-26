"""SQLite state store for migration tracking."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DEFAULT_DB = "tg_migrate.db"

# User statuses (state machine)
STATUS_IMPORTED = "imported"       # Loaded from source, not yet probed
STATUS_PROBED_OK = "probed_ok"     # getChat/send succeeded — user can receive messages
STATUS_PROBED_DEAD = "probed_dead" # Chat not found — user hasn't /started new bot
STATUS_MIGRATED = "migrated"       # User confirmed active in new bot

ALL_STATUSES = {STATUS_IMPORTED, STATUS_PROBED_OK, STATUS_PROBED_DEAD, STATUS_MIGRATED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id TEXT NOT NULL,
    bot_name TEXT NOT NULL,
    name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    role TEXT DEFAULT '',
    status TEXT DEFAULT 'imported',
    probed_at TEXT,
    migrated_at TEXT,
    redirect_seen_at TEXT,
    error TEXT,
    PRIMARY KEY (tg_id, bot_name)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id TEXT NOT NULL,
    bot_name TEXT DEFAULT '',
    event TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    details TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationDB:
    """SQLite-backed migration state store."""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    @contextmanager
    def _cursor(self):
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Meta ──

    def set_meta(self, key: str, value: str):
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = ?",
                (key, value, value),
            )

    def get_meta(self, key: str) -> Optional[str]:
        with self._cursor() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    # ── Users ──

    def upsert_user(
        self,
        tg_id: str,
        bot_name: str,
        name: str = "",
        username: str = "",
        role: str = "",
    ):
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO users (tg_id, bot_name, name, username, role, status)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tg_id, bot_name) DO UPDATE SET
                       name = COALESCE(NULLIF(?, ''), name),
                       username = COALESCE(NULLIF(?, ''), username),
                       role = COALESCE(NULLIF(?, ''), role)
                """,
                (tg_id, bot_name, name, username, role, STATUS_IMPORTED,
                 name, username, role),
            )

    def set_status(self, tg_id: str, bot_name: str, status: str, error: str = ""):
        ts = _now()
        extra = {}
        if status == STATUS_PROBED_OK or status == STATUS_PROBED_DEAD:
            extra["probed_at"] = ts
        elif status == STATUS_MIGRATED:
            extra["migrated_at"] = ts

        set_parts = ["status = ?", "error = ?"]
        params = [status, error]
        for col, val in extra.items():
            set_parts.append(f"{col} = ?")
            params.append(val)

        params.extend([tg_id, bot_name])

        with self._cursor() as cur:
            cur.execute(
                f"UPDATE users SET {', '.join(set_parts)} WHERE tg_id = ? AND bot_name = ?",
                params,
            )
            self._log_event(cur, tg_id, bot_name, status, error)

    def mark_redirect_seen(self, tg_id: str, bot_name: str):
        with self._cursor() as cur:
            cur.execute(
                "UPDATE users SET redirect_seen_at = ? WHERE tg_id = ? AND bot_name = ?",
                (_now(), tg_id, bot_name),
            )
            self._log_event(cur, tg_id, bot_name, "redirect_seen", "")

    def get_users(self, bot_name: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM users WHERE 1=1"
        params = []
        if bot_name:
            query += " AND bot_name = ?"
            params.append(bot_name)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY role, name"

        with self._cursor() as cur:
            rows = cur.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self, bot_name: Optional[str] = None) -> dict:
        """Get migration statistics."""
        users = self.get_users(bot_name=bot_name)
        total = len(users)
        by_status = {}
        for s in ALL_STATUSES:
            by_status[s] = sum(1 for u in users if u["status"] == s)
        by_role = {}
        for u in users:
            role = u.get("role") or "unknown"
            by_role.setdefault(role, {"total": 0, "migrated": 0, "probed_ok": 0})
            by_role[role]["total"] += 1
            if u["status"] == STATUS_MIGRATED:
                by_role[role]["migrated"] += 1
            elif u["status"] == STATUS_PROBED_OK:
                by_role[role]["probed_ok"] += 1

        ready = by_status.get(STATUS_PROBED_OK, 0) + by_status.get(STATUS_MIGRATED, 0)
        return {
            "total": total,
            "by_status": by_status,
            "by_role": by_role,
            "ready_pct": (ready / max(total, 1)) * 100,
        }

    # ── Events ──

    def _log_event(self, cur, tg_id: str, bot_name: str, event: str, details: str):
        cur.execute(
            "INSERT INTO events (tg_id, bot_name, event, timestamp, details) VALUES (?, ?, ?, ?, ?)",
            (tg_id, bot_name, event, _now(), details),
        )

    def get_events(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
