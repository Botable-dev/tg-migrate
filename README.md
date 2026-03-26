# tg-migrate

**General-purpose Telegram bot token migration CLI tool.**

Born from 9 real edge cases encountered during production bot migrations. Provides a structured, repeatable pipeline to migrate any Telegram bot to a new token with zero message loss and full user tracking.

## The Problem

When you change a Telegram Bot Token, **all users lose connection**. Their `tg_id` stays the same, but the new bot cannot send messages until they press `/start` again. This causes:

- All notifications stop (reminders, alerts, homework, etc.)
- Inline keyboard buttons in old messages break
- Support teams get flooded with "bot is broken" reports
- Developers scramble to fix cascading failures for 72+ hours

## The Solution

`tg-migrate` turns the chaotic manual process into **6 CLI commands**:

```
init → import → redirect → cutover → probe → status
```

## Installation

```bash
pip install tg-migrate
```

## Quick Start

```bash
# 1. Create config
tg-migrate init

# 2. Edit migrate.yaml with your bot tokens
#    Add users via CSV, JSON, or Supabase

# 3. Import users
tg-migrate import

# 4. Start redirect bots (old tokens show "bot moved" message)
tg-migrate redirect

# 5. Swap tokens in .env
tg-migrate cutover          # dry-run
tg-migrate cutover --apply  # real swap

# 6. Check who can receive messages
tg-migrate probe

# 7. Monitor progress
tg-migrate status -d          # detailed
tg-migrate status -w 60       # live refresh
tg-migrate status --notify    # send to Telegram
```

## Configuration

All settings live in `migrate.yaml`:

```yaml
project: my-bot
migration_date: "2026-03-22T20:25:00+00:00"

bots:
  - name: main
    old_token_env: OLD_TELEGRAM_BOT_TOKEN
    new_token_env: TELEGRAM_BOT_TOKEN
    new_username: "@MyNewBot"
  - name: admin
    old_token_env: OLD_ADMIN_BOT_TOKEN
    new_token_env: ADMIN_BOT_TOKEN
    new_username: "@MyNewAdminBot"

users:
  source: csv
  file: users.csv

probe:
  method: getChat        # silent default; or send_message
  concurrency: 5
  delay_ms: 100
```

### User Import Sources

**CSV:**
```csv
tg_id,name,username,role,bot
123456789,Alice,alice_wonder,student,main
987654321,Bob,,teacher,main
```

**JSON:**
```json
[
  {"tg_id": "123456789", "name": "Alice", "role": "student", "bot": "main"}
]
```

**Supabase:**
```yaml
users:
  source: supabase
  supabase_url_env: SUPABASE_URL
  supabase_key_env: SUPABASE_KEY
  tables:
    - table: students
      tg_id_col: tg_id
      name_col: name
      role: student
      bot: main
```

## Commands

| Command | Description |
|---------|-------------|
| `tg-migrate init` | Create `migrate.yaml` + SQLite DB |
| `tg-migrate import` | Load users from CSV/JSON/Supabase |
| `tg-migrate redirect` | Start redirect bots (old → new) |
| `tg-migrate cutover` | Swap tokens in `.env` with backup |
| `tg-migrate probe` | Check user reachability via new bot |
| `tg-migrate status` | Migration progress report |
| `tg-migrate doctor` | Scan code for migration anti-patterns |
| `tg-migrate cleanup` | Remove `OLD_*` vars and artifacts |

## Probe Methods

**`getChat`** (default) — Silent. Calls Telegram's `getChat` API. No message is sent to the user. Checks if the chat exists and the bot can access it.

**`send_message`** — Sends a test message and immediately deletes it. More reliable (confirms message delivery) but intrusive.

Override via config or CLI:
```bash
tg-migrate probe --method send_message
```

## Doctor: Code Health Check

Scans your bot code for 6 known migration anti-patterns:

```bash
tg-migrate doctor ./my_bot/
```

| Check | Severity | What it finds |
|-------|----------|---------------|
| `infinite_retry` | 🔴 Critical | Chat not found without marking sent |
| `recipient_loop` | 🔴 Critical | Broadcast without per-recipient try/except |
| `null_dedup` | 🟡 Warning | SQL IN() on nullable columns |
| `stale_callback` | 🟡 Warning | Missing "Message is not modified" handler |
| `session_kill` | 🟡 Warning | Session destroyed on validation failure |
| `double_answer` | ℹ️ Info | Multiple query.answer() in same callback |

## User Status State Machine

```
IMPORTED → PROBED_OK → MIGRATED
              ↓
         PROBED_DEAD (Chat not found — user hasn't /started)
```

- **IMPORTED** — Loaded from source, not yet checked
- **PROBED_OK** — Bot can send messages to this user
- **PROBED_DEAD** — Chat not found, bot blocked, or user deactivated
- **MIGRATED** — User confirmed active (via application logic)

## Edge Cases Handled

This tool was built after discovering these issues in production:

1. **Infinite retry loop** — Chat not found errors not marked as permanent
2. **Admin alert spam** — 18,500 duplicate alerts in 48 hours
3. **Recipient loop crash** — One dead chat kills delivery to all recipients
4. **NULL SQL dedup** — `IN(NULL)` always returns FALSE
5. **Supabase 1000-row limit** — Default limit truncating dedup queries
6. **Watchdog false positive** — Error count baseline reset on restart
7. **Invisible migration** — Users /starting but not tracked
8. **Orphaned homework** — Files sent to unreachable users
9. **Session destruction** — "Finish" button killing in-progress sessions

## License

MIT
