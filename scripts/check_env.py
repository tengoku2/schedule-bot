import os
import sys
import json
import traceback
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_VARS = [
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASS",
    "DB_NAME",
]


def main() -> int:
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        print("Missing environment variables:", ", ".join(missing))
        return 1

    try:
        import bot
    except Exception:
        print("Failed to import bot.py")
        traceback.print_exc()
        return 1

    print("Imported bot.py successfully")

    examples = ["", "310", "1800", "411000", "12040711"]
    parsed = {}
    for value in examples:
        try:
            due = bot.parse_datetime_input(value)
            parsed[value or "<empty>"] = {"ok": due.isoformat()}
        except Exception as exc:
            parsed[value or "<empty>"] = {"error": str(exc)}

    print("Datetime parser examples:")
    print(json.dumps(parsed, ensure_ascii=False, indent=2))

    reminder_examples = bot.parse_reminders("1d,3h")
    print("Reminder parser example:", reminder_examples)

    try:
        db = mysql.connector.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ["DB_PORT"]),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASS"],
            database=os.environ["DB_NAME"],
            ssl_disabled=False,
            ssl_verify_cert=False,
        )
    except Exception:
        print("Failed to connect to MySQL")
        traceback.print_exc()
        return 1

    try:
        cursor = db.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        print("MySQL connection: OK")

        cursor.execute("SHOW TABLES")
        tables = sorted(row[0] for row in cursor.fetchall())
        print("Tables:", ", ".join(tables))

        required_columns = {
            "tasks": {"id", "task", "due", "channel_id", "notify_channel_id", "owner_id", "reminders", "notified", "status", "guild_id", "category", "loop_remind_start", "loop_remind_interval_minutes", "loop_remind_last_sent_at"},
            "guild_settings": {"guild_id", "manager_role_id", "notify_channel_id"},
        }

        dict_cursor = db.cursor(dictionary=True)
        for table_name, required in required_columns.items():
            dict_cursor.execute(f"SHOW COLUMNS FROM {table_name}")
            existing = {row["Field"] for row in dict_cursor.fetchall()}
            missing = sorted(required - existing)
            if missing:
                print(f"Missing columns in {table_name}: {', '.join(missing)}")
                return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
