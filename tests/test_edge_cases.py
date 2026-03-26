#!/usr/bin/env python3
"""Comprehensive edge-case tests for tg-migrate."""

import csv
import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

# Ensure we use the local package
sys.path.insert(0, str(Path(__file__).parent.parent))

FAIL = 0
PASS = 0

def check(name, condition, detail=""):
    global FAIL, PASS
    if condition:
        print(f"  ✅ {name}")
        PASS += 1
    else:
        print(f"  ❌ {name}: {detail}")
        FAIL += 1


def test_db():
    """Test SQLite state store edge cases."""
    print("\n=== DB TESTS ===")
    from tg_migrate.db import MigrationDB, STATUS_IMPORTED, STATUS_PROBED_OK, STATUS_PROBED_DEAD, STATUS_MIGRATED

    with tempfile.TemporaryDirectory() as tmpdir:
        db = MigrationDB(os.path.join(tmpdir, "test.db"))

        # 1. Upsert + idempotency
        db.upsert_user("123", "main", name="Alice", username="alice", role="student")
        db.upsert_user("123", "main", name="Alice Updated", username="", role="")
        users = db.get_users()
        check("Upsert idempotent (1 user)", len(users) == 1)
        check("Upsert name updated", users[0]["name"] == "Alice Updated")
        check("Upsert username NOT blanked", users[0]["username"] == "alice",
              f"got: {users[0]['username']}")

        # 2. Same tg_id, different bots
        db.upsert_user("123", "admin", name="Alice", role="admin")
        all_users = db.get_users()
        check("Same tg_id different bots = 2 users", len(all_users) == 2)
        main_users = db.get_users(bot_name="main")
        check("Filter by bot_name works", len(main_users) == 1)

        # 3. Status transitions
        db.set_status("123", "main", STATUS_PROBED_OK)
        u = db.get_users(bot_name="main")[0]
        check("Status PROBED_OK set", u["status"] == STATUS_PROBED_OK)
        check("probed_at timestamp set", u["probed_at"] is not None)

        db.set_status("123", "main", STATUS_PROBED_DEAD, error="Chat not found")
        u = db.get_users(bot_name="main")[0]
        check("Status PROBED_DEAD set", u["status"] == STATUS_PROBED_DEAD)
        check("Error recorded", u["error"] == "Chat not found")

        db.set_status("123", "main", STATUS_MIGRATED)
        u = db.get_users(bot_name="main")[0]
        check("Status MIGRATED set", u["status"] == STATUS_MIGRATED)
        check("migrated_at timestamp set", u["migrated_at"] is not None)

        # 4. Stats
        db.upsert_user("456", "main", name="Bob", role="teacher")
        db.set_status("456", "main", STATUS_PROBED_DEAD, error="blocked")
        stats = db.get_stats(bot_name="main")
        check("Stats total correct", stats["total"] == 2, f"got {stats['total']}")
        check("Stats ready_pct correct", stats["ready_pct"] == 50.0,
              f"got {stats['ready_pct']}")

        # 5. Empty DB stats
        empty_stats = db.get_stats(bot_name="nonexistent")
        check("Empty stats - no crash", empty_stats["total"] == 0)
        check("Empty stats - 0% ready", empty_stats["ready_pct"] == 0.0)

        # 6. Events logged
        events = db.get_events(limit=100)
        check("Events logged for status changes", len(events) >= 3,
              f"got {len(events)}")

        # 7. Meta
        db.set_meta("test_key", "test_val")
        check("Meta set/get", db.get_meta("test_key") == "test_val")
        check("Meta missing key", db.get_meta("nope") is None)

        # 8. Edge: empty tg_id
        db.upsert_user("", "main", name="Ghost")
        ghost = db.get_users(bot_name="main")
        ghost_count = sum(1 for u in ghost if u["tg_id"] == "")
        check("Empty tg_id allowed (but should be filtered by importer)",
              ghost_count == 1)

        # 9. Special chars in name
        db.upsert_user("789", "main", name="O'Brien \"Mac\" <test>", role="student")
        u = db.get_users(bot_name="main")
        obrien = [x for x in u if x["tg_id"] == "789"]
        check("Special chars in name", len(obrien) == 1 and "O'Brien" in obrien[0]["name"])

        # 10. Redirect seen
        db.mark_redirect_seen("123", "main")
        all_main = db.get_users(bot_name="main")
        u123 = [x for x in all_main if x["tg_id"] == "123"][0]
        check("redirect_seen_at set", u123["redirect_seen_at"] is not None)

        db.close()


def test_config():
    """Test config parsing edge cases."""
    print("\n=== CONFIG TESTS ===")
    from tg_migrate.config import load_config, EXAMPLE_CONFIG, MigrateConfig
    from datetime import datetime, timezone

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Missing config
        try:
            load_config(os.path.join(tmpdir, "nope.yaml"))
            check("Missing config raises error", False)
        except FileNotFoundError:
            check("Missing config raises FileNotFoundError", True)

        # 2. Empty YAML
        empty_path = os.path.join(tmpdir, "empty.yaml")
        Path(empty_path).write_text("")
        cfg = load_config(empty_path)
        check("Empty YAML → defaults", cfg.project == "my-bot")
        check("Empty YAML → no bots", len(cfg.bots) == 0)

        # 3. Minimal config
        minimal_path = os.path.join(tmpdir, "minimal.yaml")
        Path(minimal_path).write_text("project: test\nbots:\n  - name: b1\n    old_token_env: OLD\n    new_token_env: NEW\n    new_username: '@TestBot'\n")
        cfg = load_config(minimal_path)
        check("Minimal config bot parsed", len(cfg.bots) == 1)
        check("Bot link auto-generated", cfg.bots[0].new_link == "https://t.me/TestBot")
        check("Default probe method", cfg.probe.method == "getChat")

        # 4. Example config template renders
        now = datetime.now(timezone.utc).isoformat()
        rendered = EXAMPLE_CONFIG.format(date=now)
        template_path = os.path.join(tmpdir, "template.yaml")
        Path(template_path).write_text(rendered)
        cfg = load_config(template_path)
        check("Example template parses", cfg.project == "my-bot")
        check("Template has 1 bot", len(cfg.bots) == 1)

        # 5. Config with unknown keys (shouldn't crash)
        extra_path = os.path.join(tmpdir, "extra.yaml")
        Path(extra_path).write_text("project: test\nunknown_key: value\nbots: []\n")
        cfg = load_config(extra_path)
        check("Unknown keys ignored", cfg.project == "test")

        # 6. Bot without new_username (no link generated)
        no_user_path = os.path.join(tmpdir, "nouser.yaml")
        Path(no_user_path).write_text("bots:\n  - name: x\n    old_token_env: A\n    new_token_env: B\n")
        cfg = load_config(no_user_path)
        check("Bot without username → empty link", cfg.bots[0].new_link == "")


def test_import_csv():
    """Test CSV import edge cases."""
    print("\n=== IMPORT CSV TESTS ===")
    from tg_migrate.db import MigrationDB
    from tg_migrate.config import MigrateConfig, BotConfig, UserSource

    with tempfile.TemporaryDirectory() as tmpdir:
        db = MigrationDB(os.path.join(tmpdir, "test.db"))
        cfg = MigrateConfig(bots=[BotConfig(name="main", old_token_env="O", new_token_env="N")])

        # 1. Normal CSV
        csv_path = os.path.join(tmpdir, "users.csv")
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["tg_id", "name", "username", "role", "bot"])
            w.writerow(["111", "Alice", "alice", "student", "main"])
            w.writerow(["222", "Bob", "", "teacher", "main"])
            w.writerow(["", "Ghost", "", "", "main"])  # empty tg_id
            w.writerow(["333", "", "", "", ""])  # no name

        from tg_migrate.cli import _import_csv
        _import_csv(db, cfg, csv_path, None)
        users = db.get_users()
        check("CSV: 3 users imported (empty tg_id skipped)", len(users) == 3)

        # 2. CSV with different column names
        custom_path = os.path.join(tmpdir, "custom.csv")
        with open(custom_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["user_id", "full_name", "nick", "user_role"])
            w.writerow(["444", "Charlie", "charlie", "admin"])

        cfg2 = MigrateConfig(
            bots=[BotConfig(name="main", old_token_env="O", new_token_env="N")],
            users=UserSource(tg_id_col="user_id", name_col="full_name", username_col="nick", role_col="user_role"),
        )
        db2 = MigrationDB(os.path.join(tmpdir, "test2.db"))
        _import_csv(db2, cfg2, custom_path, "main")
        users2 = db2.get_users()
        check("Custom columns: 1 user", len(users2) == 1)
        check("Custom columns: name correct", users2[0]["name"] == "Charlie")

        # 3. CSV with BOM (Windows export)
        bom_path = os.path.join(tmpdir, "bom.csv")
        with open(bom_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["tg_id", "name"])
            w.writerow(["555", "Diana"])

        db3 = MigrationDB(os.path.join(tmpdir, "test3.db"))
        try:
            _import_csv(db3, cfg, bom_path, "main")
            users3 = db3.get_users()
            check("BOM CSV: 1 user imported", len(users3) == 1, f"got {len(users3)}")
        except Exception as e:
            check("BOM CSV: no crash", False, str(e))

        db.close()
        db2.close()
        db3.close()


def test_import_json():
    """Test JSON import edge cases."""
    print("\n=== IMPORT JSON TESTS ===")
    from tg_migrate.db import MigrationDB
    from tg_migrate.config import MigrateConfig, BotConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        db = MigrationDB(os.path.join(tmpdir, "test.db"))
        cfg = MigrateConfig(bots=[BotConfig(name="main", old_token_env="O", new_token_env="N")])

        # 1. Array format
        arr_path = os.path.join(tmpdir, "array.json")
        Path(arr_path).write_text(json.dumps([
            {"tg_id": "100", "name": "Alice", "role": "student"},
            {"tg_id": "200", "name": "Bob"},
        ]))
        from tg_migrate.cli import _import_json
        _import_json(db, cfg, arr_path, "main")
        check("JSON array: 2 users", len(db.get_users()) == 2)

        # 2. Object with "users" key
        db2 = MigrationDB(os.path.join(tmpdir, "test2.db"))
        obj_path = os.path.join(tmpdir, "obj.json")
        Path(obj_path).write_text(json.dumps({
            "users": [{"tg_id": "300", "name": "Charlie"}]
        }))
        _import_json(db2, cfg, obj_path, "main")
        check("JSON {users: []} format: 1 user", len(db2.get_users()) == 1)

        # 3. Object with "data" key
        db3 = MigrationDB(os.path.join(tmpdir, "test3.db"))
        data_path = os.path.join(tmpdir, "data.json")
        Path(data_path).write_text(json.dumps({
            "data": [{"tg_id": "400", "name": "Diana"}]
        }))
        _import_json(db3, cfg, data_path, "main")
        check("JSON {data: []} format: 1 user", len(db3.get_users()) == 1)

        # 4. Empty JSON
        db4 = MigrationDB(os.path.join(tmpdir, "test4.db"))
        empty_path = os.path.join(tmpdir, "empty.json")
        Path(empty_path).write_text("[]")
        _import_json(db4, cfg, empty_path, "main")
        check("Empty JSON: 0 users, no crash", len(db4.get_users()) == 0)

        db.close(); db2.close(); db3.close(); db4.close()


def test_cutover():
    """Test .env cutover edge cases."""
    print("\n=== CUTOVER TESTS ===")
    from tg_migrate.config import MigrateConfig, BotConfig
    from tg_migrate.cutover import cutover_env

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Basic swap
        env_path = os.path.join(tmpdir, ".env")
        Path(env_path).write_text(
            "TELEGRAM_BOT_TOKEN=old_token_123\n"
            "OTHER_VAR=keep_me\n"
            "NEW_TELEGRAM_BOT_TOKEN=new_token_456\n"
        )
        cfg = MigrateConfig(
            env_file=env_path,
            bots=[BotConfig(name="main", old_token_env="TELEGRAM_BOT_TOKEN", new_token_env="TELEGRAM_BOT_TOKEN")],
        )
        diff = cutover_env(cfg, apply=False)
        check("Dry-run returns swaps", len(diff["swaps"]) >= 1)
        check("Dry-run doesn't modify file", "old_token_123" in Path(env_path).read_text())

        # Apply
        diff = cutover_env(cfg, apply=True)
        content = Path(env_path).read_text()
        check("Apply: token swapped", "new_token_456" in content)
        check("Apply: NEW_ line removed", "NEW_TELEGRAM_BOT_TOKEN" not in content)
        check("Apply: OTHER_VAR preserved", "keep_me" in content)
        check("Apply: backup created", diff.get("backup", "") != "")

        # 2. No NEW_ vars present
        env2 = os.path.join(tmpdir, ".env2")
        Path(env2).write_text("TELEGRAM_BOT_TOKEN=my_token\n")
        cfg2 = MigrateConfig(
            env_file=env2,
            bots=[BotConfig(name="main", old_token_env="OLD", new_token_env="TELEGRAM_BOT_TOKEN")],
        )
        diff2 = cutover_env(cfg2, apply=False)
        check("No NEW_ vars: empty swaps", len(diff2["swaps"]) == 0)

        # 3. Values with = signs (base64 tokens etc.)
        env3 = os.path.join(tmpdir, ".env3")
        Path(env3).write_text(
            "TOKEN=abc=def==\n"
            "NEW_TOKEN=xyz=123==\n"
        )
        cfg3 = MigrateConfig(
            env_file=env3,
            bots=[BotConfig(name="main", old_token_env="TOKEN", new_token_env="TOKEN")],
        )
        diff3 = cutover_env(cfg3, apply=True)
        content3 = Path(env3).read_text()
        check("Values with = signs preserved", "xyz=123==" in content3)

        # 4. Missing .env
        cfg4 = MigrateConfig(env_file=os.path.join(tmpdir, "nope.env"))
        try:
            cutover_env(cfg4)
            check("Missing .env raises error", False)
        except FileNotFoundError:
            check("Missing .env raises FileNotFoundError", True)

        # 5. Comments preserved
        env5 = os.path.join(tmpdir, ".env5")
        Path(env5).write_text(
            "# This is a comment\n"
            "TOKEN=old\n"
            "NEW_TOKEN=new\n"
            "# Another comment\n"
        )
        cfg5 = MigrateConfig(
            env_file=env5,
            bots=[BotConfig(name="main", old_token_env="TOKEN", new_token_env="TOKEN")],
        )
        cutover_env(cfg5, apply=True)
        lines5 = Path(env5).read_text().splitlines()
        check("Comments preserved", any("comment" in l for l in lines5))


def test_healthcheck():
    """Test doctor/healthcheck edge cases."""
    print("\n=== HEALTHCHECK TESTS ===")
    from tg_migrate.healthcheck import scan_file, scan_directory

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. File with infinite_retry pattern
        bad_py = os.path.join(tmpdir, "bad_bot.py")
        Path(bad_py).write_text("""
async def send_notification(tg_id, text):
    try:
        await bot.send_message(tg_id, text)
    except Exception as e:
        if 'Chat not found' in str(e):
            logger.error(f"Failed: {e}")
            # BUG: doesn't mark as sent or permanent
""")
        findings = scan_file(Path(bad_py))
        has_infinite = any(f.check == "infinite_retry" for f in findings)
        check("Detects infinite_retry", has_infinite)

        # 2. File WITH the fix (should NOT trigger)
        good_py = os.path.join(tmpdir, "good_bot.py")
        Path(good_py).write_text("""
async def send_notification(tg_id, text):
    try:
        await bot.send_message(tg_id, text)
    except Exception as e:
        if is_permanent_send_error(e):
            mark_user_unreachable(tg_id)
        if 'Chat not found' in str(e):
            logger.warning("Dead chat")
""")
        findings2 = scan_file(Path(good_py))
        has_infinite2 = any(f.check == "infinite_retry" for f in findings2)
        check("Good code: no infinite_retry finding", not has_infinite2)

        # 3. Recipient loop without try/except
        loop_py = os.path.join(tmpdir, "loop_bot.py")
        Path(loop_py).write_text("""
async def broadcast():
    for user in recipients:
        await bot.send_message(user.tg_id, text)
""")
        findings3 = scan_file(Path(loop_py))
        has_loop = any(f.check == "recipient_loop" for f in findings3)
        check("Detects recipient_loop", has_loop)

        # 4. Recipient loop WITH try/except
        safe_loop_py = os.path.join(tmpdir, "safe_loop.py")
        Path(safe_loop_py).write_text("""
async def broadcast():
    for user in recipients:
        try:
            await bot.send_message(user.tg_id, text)
        except Exception:
            pass
""")
        findings4 = scan_file(Path(safe_loop_py))
        has_safe_loop = any(f.check == "recipient_loop" for f in findings4)
        check("Safe loop: no recipient_loop finding", not has_safe_loop)

        # 5. Empty file
        empty_py = os.path.join(tmpdir, "empty.py")
        Path(empty_py).write_text("")
        findings5 = scan_file(Path(empty_py))
        check("Empty file: no findings", len(findings5) == 0)

        # 6. Non-python file in directory
        txt_file = os.path.join(tmpdir, "notes.txt")
        Path(txt_file).write_text("Chat not found error handler")
        all_findings = scan_directory(tmpdir)
        txt_findings = [f for f in all_findings if f.file.endswith(".txt")]
        check("Non-python files skipped", len(txt_findings) == 0)

        # 7. Binary file doesn't crash
        bin_file = os.path.join(tmpdir, "data.py")
        Path(bin_file).write_bytes(b"\x00\x01\x02\xff\xfe")
        try:
            scan_file(Path(bin_file))
            check("Binary .py file: no crash", True)
        except Exception as e:
            check("Binary .py file: no crash", False, str(e))

        # 8. Double query.answer
        dbl_py = os.path.join(tmpdir, "dbl.py")
        Path(dbl_py).write_text("""
async def my_callback(update, context):
    query = update.callback_query
    await query.answer()
    # ... logic ...
    await query.answer("Done!", show_alert=True)
""")
        findings8 = scan_file(Path(dbl_py))
        has_dbl = any(f.check == "double_answer" for f in findings8)
        check("Detects double query.answer()", has_dbl)


def test_reporter():
    """Test reporter edge cases."""
    print("\n=== REPORTER TESTS ===")
    from tg_migrate.db import MigrationDB, STATUS_PROBED_OK, STATUS_PROBED_DEAD
    from tg_migrate.config import MigrateConfig, BotConfig
    from tg_migrate.reporter import print_status, format_telegram_report

    with tempfile.TemporaryDirectory() as tmpdir:
        db = MigrationDB(os.path.join(tmpdir, "test.db"))
        cfg = MigrateConfig(
            project="test-project",
            bots=[BotConfig(name="main", old_token_env="O", new_token_env="N", new_username="@TestBot")],
        )

        # 1. Empty DB
        try:
            print_status(db, cfg)
            check("Empty DB status: no crash", True)
        except Exception as e:
            check("Empty DB status: no crash", False, str(e))

        # 2. With users
        db.upsert_user("1", "main", name="Alice", role="student")
        db.upsert_user("2", "main", name="Bob", role="teacher")
        db.set_status("1", "main", STATUS_PROBED_OK)
        db.set_status("2", "main", STATUS_PROBED_DEAD, error="Chat not found")

        try:
            print_status(db, cfg, detailed=True)
            check("Detailed status: no crash", True)
        except Exception as e:
            check("Detailed status: no crash", False, str(e))

        # 3. Telegram report format
        report = format_telegram_report(db, cfg)
        check("Telegram report not empty", len(report) > 0)
        check("Telegram report has project name", "test-project" in report)
        check("Telegram report has percentage", "50%" in report)

        # 4. Unicode names
        db.upsert_user("3", "main", name="Ёкатерина 🇷🇺", role="student")
        db.set_status("3", "main", STATUS_PROBED_OK)
        try:
            print_status(db, cfg, detailed=True)
            report2 = format_telegram_report(db, cfg)
            check("Unicode names: no crash", True)
        except Exception as e:
            check("Unicode names: no crash", False, str(e))

        db.close()


def test_init_command():
    """Test init command edge cases."""
    print("\n=== INIT COMMAND TESTS ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        orig_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            from click.testing import CliRunner
            from tg_migrate.cli import main

            runner = CliRunner()

            # 1. First init
            result = runner.invoke(main, ["init"])
            check("Init succeeds", result.exit_code == 0, result.output)
            check("migrate.yaml created", Path("migrate.yaml").exists())
            check("tg_migrate.db created", Path("tg_migrate.db").exists())

            # 2. Second init (should warn)
            result2 = runner.invoke(main, ["init"])
            check("Second init warns", "already exists" in result2.output)

            # 3. Init --force
            result3 = runner.invoke(main, ["init", "--force"])
            check("Init --force succeeds", result3.exit_code == 0)

            # 4. Generated config is parseable
            from tg_migrate.config import load_config
            cfg = load_config("migrate.yaml")
            check("Generated config parses", cfg.project == "my-bot")

        finally:
            os.chdir(orig_cwd)


def test_full_flow():
    """Test init → import → status flow end-to-end."""
    print("\n=== FULL FLOW TEST ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        orig_cwd = os.getcwd()
        os.chdir(tmpdir)
        try:
            from click.testing import CliRunner
            from tg_migrate.cli import main

            runner = CliRunner()

            # 1. Init
            runner.invoke(main, ["init", "--force"])

            # 2. Create CSV
            csv_path = os.path.join(tmpdir, "users.csv")
            with open(csv_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["tg_id", "name", "username", "role", "bot"])
                for i in range(10):
                    w.writerow([str(1000+i), f"User{i}", f"user{i}", "student", "main"])

            # 3. Update config to point to CSV
            import yaml
            cfg_data = yaml.safe_load(Path("migrate.yaml").read_text())
            cfg_data["users"]["file"] = csv_path
            Path("migrate.yaml").write_text(yaml.dump(cfg_data))

            # 4. Import
            result = runner.invoke(main, ["import"])
            check("Import succeeds", result.exit_code == 0, result.output)
            check("Import shows count", "10" in result.output, result.output)

            # 5. Status
            result = runner.invoke(main, ["status"])
            check("Status succeeds", result.exit_code == 0, result.output)

            # 6. Status detailed
            result = runner.invoke(main, ["status", "-d"])
            check("Status -d succeeds", result.exit_code == 0, result.output)

            # 7. Doctor on empty dir
            result = runner.invoke(main, ["doctor", tmpdir])
            check("Doctor on empty dir: no crash", result.exit_code == 0)

        finally:
            os.chdir(orig_cwd)


if __name__ == "__main__":
    test_db()
    test_config()
    test_import_csv()
    test_import_json()
    test_cutover()
    test_healthcheck()
    test_reporter()
    test_init_command()
    test_full_flow()

    print(f"\n{'='*50}")
    print(f"  RESULTS: {PASS} passed, {FAIL} failed")
    print(f"{'='*50}")
    sys.exit(1 if FAIL > 0 else 0)
