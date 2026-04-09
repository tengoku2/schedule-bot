import os
import json
import asyncio
import datetime
from functools import partial
from typing import Literal, Optional

import discord
import mysql.connector
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from flask import Flask
from waitress import serve

load_dotenv()

JST = datetime.timezone(datetime.timedelta(hours=9))
DEFAULT_REMINDERS = [
    ("1month", 30),
    ("2week", 14),
    ("1week", 7),
    ("3day", 3),
    ("24hour", 1),
]
TASK_CATEGORIES = (
    "general",
    "composer",
    "vocal",
    "operations",
    "illustration",
    "design",
)
TaskCategory = Literal["general", "composer", "vocal", "operations", "illustration", "design"]
TaskSortBy = Literal["due", "task", "category", "status"]
TaskSortOrder = Literal["asc", "desc"]
TaskGroupBy = Literal["none", "category", "due_date"]
STATUS_BULK_COOLDOWN_SECONDS = 10
status_bulk_cooldowns = {}


async def run_blocking(func, *args, **kwargs):
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


app = Flask(__name__)


@app.route("/")
def home():
    return "OK", 200


def run_web():
    port = int(os.environ.get("PORT", 8000))
    serve(app, host="0.0.0.0", port=port)


def get_db():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST"),
        port=int(os.environ.get("DB_PORT", 15040)),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASS"),
        database=os.environ.get("DB_NAME"),
        ssl_disabled=False,
        ssl_verify_cert=False,
    )


def get_cursor():
    db = get_db()
    return db, db.cursor(dictionary=True)


def json_loads_or_default(value, default):
    if value in (None, ""):
        return default
    return json.loads(value)


def normalize_task_category(category):
    if category in TASK_CATEGORIES:
        return category
    return "general"


def normalize_due(due):
    if due.tzinfo is None:
        return due.replace(tzinfo=JST)
    return due


def is_valid_task_guild(task):
    guild_id = task.get("guild_id")
    if not guild_id:
        return False
    return bot.get_guild(guild_id) is not None


def parse_compact_time_value(time_text):
    if not time_text.isdigit():
        raise ValueError("時間形式エラー")

    if len(time_text) <= 2:
        hour = int(time_text)
        minute = 0
    else:
        hour = int(time_text[:-2])
        minute = int(time_text[-2:])

    if hour >= 24 or minute >= 60:
        raise ValueError("時間形式エラー")

    return hour, minute


def parse_slash_datetime_input(dt_str, now):
    parts = dt_str.split()
    if not parts or "/" not in parts[0]:
        raise ValueError("日付形式エラー")

    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else ""

    month_day = date_part.split("/")
    if len(month_day) != 2:
        raise ValueError("日付形式エラー")

    month = int(month_day[0])
    day = int(month_day[1])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError("日付エラー")

    if not time_part:
        hour = 0
        minute = 0
    elif ":" in time_part:
        hhmm = time_part.split(":")
        if len(hhmm) != 2:
            raise ValueError("時間形式エラー")
        hour = int(hhmm[0])
        minute = int(hhmm[1])
        if hour >= 24 or minute >= 60:
            raise ValueError("時間形式エラー")
    else:
        hour, minute = parse_compact_time_value(time_part)

    try:
        due = datetime.datetime(now.year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        raise ValueError("存在しない日付です")

    if due <= now:
        due = due.replace(year=now.year + 1)
    return due


def parse_relative_day_input(dt_str, now):
    import re

    match = re.fullmatch(r"(\d+)日(?:後)?(?:\s+(.+))?", dt_str)
    if not match:
        raise ValueError("相対日付形式エラー")

    days = int(match.group(1))
    time_part = (match.group(2) or "").strip()
    target_date = now.date() + datetime.timedelta(days=days)

    if not time_part:
        return datetime.datetime.combine(target_date, datetime.time(0, 0), tzinfo=JST)

    lowered = time_part.lower()
    if lowered in ["今", "いま", "now"]:
        return datetime.datetime.combine(target_date, datetime.time(now.hour, now.minute), tzinfo=JST)

    hour, minute = parse_compact_time_value(time_part)
    return datetime.datetime.combine(target_date, datetime.time(hour, minute), tzinfo=JST)


def parse_datetime_input(dt_str):
    import re

    now = datetime.datetime.now(JST)

    if not dt_str:
        tomorrow = now.date() + datetime.timedelta(days=1)
        return datetime.datetime.combine(tomorrow, datetime.time(0, 0), tzinfo=JST)

    dt_str = dt_str.strip()

    if "明日" in dt_str:
        tomorrow = now.date() + datetime.timedelta(days=1)
        remain = dt_str.replace("明日", "", 1).strip()
        if not remain:
            return datetime.datetime.combine(tomorrow, datetime.time(0, 0), tzinfo=JST)
        hour, minute = parse_compact_time_value(remain)
        return datetime.datetime.combine(tomorrow, datetime.time(hour, minute), tzinfo=JST)

    if "/" in dt_str:
        return parse_slash_datetime_input(dt_str, now)

    if dt_str.lower() in ["今", "いま", "now"]:
        raise ValueError("日付指定が必要です")

    relative_day_match = re.fullmatch(r"(\d+)日(?:後)?(?:\s+(.+))?", dt_str)
    if relative_day_match:
        return parse_relative_day_input(dt_str, now)

    relative_match = re.fullmatch(r"(\d+)\s*(分|時間|日)後", dt_str)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)
        if unit == "分":
            return now + datetime.timedelta(minutes=amount)
        if unit == "時間":
            return now + datetime.timedelta(hours=amount)
        return now + datetime.timedelta(days=amount)

    if not dt_str.isdigit():
        raise ValueError("数字で入力してください")

    n = len(dt_str)
    if n <= 4:
        if n <= 2:
            hour = int(dt_str)
            minute = 0
        else:
            hour = int(dt_str[:-2])
            minute = int(dt_str[-2:])

        if hour > 24 or minute >= 60:
            raise ValueError("時間形式エラー")

        add_day = hour == 24
        if add_day:
            hour = 0

        due = datetime.datetime.combine(now.date(), datetime.time(hour, minute), tzinfo=JST)
        if due <= now or add_day:
            due += datetime.timedelta(days=1)
        return due

    time_part = dt_str[-4:]
    hour = int(time_part[:2])
    minute = int(time_part[2:])
    if hour >= 24 or minute >= 60:
        raise ValueError("時間形式エラー")

    date_part = dt_str[:-4]
    if len(date_part) == 2:
        month = int(date_part[0])
        day = int(date_part[1])
    elif len(date_part) == 3:
        month = int(date_part[0])
        day = int(date_part[1:3])
    elif len(date_part) == 4:
        month = int(date_part[:2])
        day = int(date_part[2:4])
    else:
        raise ValueError("日付形式エラー")

    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError("日付エラー")

    year = now.year
    try:
        due = datetime.datetime(year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        raise ValueError("存在しない日付です")

    if due <= now:
        due = due.replace(year=year + 1)
    return due


def parse_reminders(reminder_str):
    import re

    mapping = {
        "y": ("year", 365),
        "mo": ("month", 30),
        "w": ("week", 7),
        "d": ("day", 1),
        "h": ("hour", 1 / 24),
        "m": ("minute", 1 / 1440),
    }

    result = []
    for part in reminder_str.split(","):
        part = part.strip().lower()

        if part == "def":
            result.extend(DEFAULT_REMINDERS)
            continue

        match = re.match(r"(\d+)([a-z]+)", part)
        if not match:
            raise ValueError("単位エラー")

        num = int(match.group(1))
        unit = match.group(2)

        if unit not in mapping:
            raise ValueError("単位エラー")

        name, base = mapping[unit]
        result.append((f"{num}{name}", num * base))
    return result


def label_to_text(label):
    import re

    label = label.replace("months", "month")
    label = label.replace("weeks", "week")
    label = label.replace("days", "day")
    label = label.replace("hours", "hour")

    match = re.match(r"(\d+)", label)
    if not match:
        return label

    num = match.group(1)
    if "year" in label:
        return f"{num}年前"
    if "month" in label:
        return f"{num}ヶ月前"
    if "week" in label:
        return f"{num}週間前"
    if "day" in label:
        return f"{num}日前"
    if "hour" in label:
        return f"{num}時間前"
    if "minute" in label:
        return f"{num}分前"
    return label


tasks_list = []
guild_settings_cache = {}


def load_tasks():
    global tasks_list
    print("[load_tasks] start")
    try:
        db, cursor = get_cursor()
        cursor.execute(
            """
            SELECT id, task, due, channel_id, notify_channel_id, owner_id,
                   visible_to, reminders, notified, status, guild_id, category
            FROM tasks
            """
        )
        rows = cursor.fetchall()
        tasks_list = [
            {
                "id": row["id"],
                "task": row["task"],
                "due": row["due"],
                "channel_id": row["channel_id"],
                "notify_channel_id": row.get("notify_channel_id"),
                "owner_id": row["owner_id"],
                "visible_to": json_loads_or_default(row["visible_to"], []),
                "reminders": json_loads_or_default(row["reminders"], []),
                "notified": json_loads_or_default(row["notified"], []),
                "status": row["status"],
                "guild_id": row["guild_id"],
                "category": normalize_task_category(row.get("category")),
            }
            for row in rows
        ]
        db.close()
    except Exception as e:
        print("[load_tasks] error:", e)


def load_guild_settings_cache():
    global guild_settings_cache
    print("[load_guild_settings] start")
    try:
        db, cursor = get_cursor()
        cursor.execute(
            """
            SELECT guild_id, manager_role_id, notify_channel_id
            FROM guild_settings
            """
        )
        rows = cursor.fetchall()
        guild_settings_cache = {
            row["guild_id"]: {
                "manager_role_id": row.get("manager_role_id"),
                "notify_channel_id": row.get("notify_channel_id"),
            }
            for row in rows
        }
        db.close()
    except Exception as e:
        print("[load_guild_settings] error:", e)


def get_guild_settings(guild_id):
    if guild_id in guild_settings_cache:
        return guild_settings_cache[guild_id]

    db, cursor = get_cursor()
    cursor.execute(
        """
        SELECT manager_role_id, notify_channel_id
        FROM guild_settings
        WHERE guild_id=%s
        """,
        (guild_id,),
    )
    row = cursor.fetchone()
    db.close()
    settings = row or {}
    guild_settings_cache[guild_id] = settings
    return settings


def get_notify_channel(guild_id):
    return get_guild_settings(guild_id).get("notify_channel_id")


def insert_task(task_name, due, channel_id, notify_channel_id, user_id, guild_id, reminders, category):
    db, cursor = get_cursor()
    cursor.execute(
        """
        INSERT INTO tasks
        (task, due, channel_id, notify_channel_id, owner_id, visible_to, roles,
         reminders, notified, mention, everyone, status, guild_id, category)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            task_name,
            due,
            channel_id,
            notify_channel_id,
            user_id,
            json.dumps([]),
            json.dumps([]),
            json.dumps(reminders),
            json.dumps([]),
            False,
            False,
            "todo",
            guild_id,
            normalize_task_category(category),
        ),
    )
    db.commit()
    db.close()


def update_task_full(task_id, task_name, due, reminders, notify_channel_id, category):
    db, cursor = get_cursor()
    cursor.execute(
        """
        UPDATE tasks
        SET task=%s, due=%s, reminders=%s, notify_channel_id=%s, category=%s
        WHERE id=%s
        """,
        (task_name, due, json.dumps(reminders), notify_channel_id, normalize_task_category(category), task_id),
    )
    db.commit()
    db.close()


def update_status(task_id, status):
    db, cursor = get_cursor()
    cursor.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, task_id))
    db.commit()
    db.close()


def insert_task_once(interaction_id, task_name, due, channel_id, notify_channel_id, user_id, guild_id, reminders, category):
    lock_name = f"schedule-bot:add:{interaction_id}"
    db, cursor = get_cursor()
    try:
        cursor.execute("SELECT GET_LOCK(%s, 0) AS locked", (lock_name,))
        row = cursor.fetchone()
        if not row or row.get("locked") != 1:
            return False

        cursor.execute(
            """
            INSERT INTO tasks
            (task, due, channel_id, notify_channel_id, owner_id, visible_to, roles,
             reminders, notified, mention, everyone, status, guild_id, category)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                task_name,
                due,
                channel_id,
                notify_channel_id,
                user_id,
                json.dumps([]),
                json.dumps([]),
                json.dumps(reminders),
                json.dumps([]),
                False,
                False,
                "todo",
                guild_id,
                normalize_task_category(category),
            ),
        )
        db.commit()
        return True
    finally:
        try:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            cursor.fetchall()
        except Exception:
            pass
        db.close()


def update_status_bulk(task_ids, status):
    if not task_ids:
        return
    db, cursor = get_cursor()
    try:
        placeholders = ",".join(["%s"] * len(task_ids))
        cursor.execute(
            f"UPDATE tasks SET status=%s WHERE id IN ({placeholders})",
            tuple([status] + list(task_ids)),
        )
        db.commit()
    finally:
        db.close()


def clear_notified(task_id):
    db, cursor = get_cursor()
    cursor.execute("UPDATE tasks SET notified=%s WHERE id=%s", (json.dumps([]), task_id))
    db.commit()
    db.close()


def append_notified(task_id, notified):
    db, cursor = get_cursor()
    cursor.execute("UPDATE tasks SET notified=%s WHERE id=%s", (json.dumps(notified), task_id))
    db.commit()
    db.close()


def get_task_status(task_id):
    db, cursor = get_cursor()
    try:
        cursor.execute("SELECT status FROM tasks WHERE id=%s", (task_id,))
        row = cursor.fetchone()
        return row["status"] if row else None
    finally:
        db.close()


def get_task_by_id(task_id):
    db, cursor = get_cursor()
    try:
        cursor.execute(
            """
            SELECT id, task, due, channel_id, notify_channel_id, owner_id,
                   visible_to, reminders, notified, status, guild_id, category
            FROM tasks
            WHERE id=%s
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "task": row["task"],
            "due": row["due"],
            "channel_id": row["channel_id"],
            "notify_channel_id": row.get("notify_channel_id"),
            "owner_id": row["owner_id"],
            "visible_to": json_loads_or_default(row["visible_to"], []),
            "reminders": json_loads_or_default(row["reminders"], []),
            "notified": json_loads_or_default(row["notified"], []),
            "status": row["status"],
            "guild_id": row["guild_id"],
            "category": normalize_task_category(row.get("category")),
        }
    finally:
        db.close()


def delete_task(task_id):
    db, cursor = get_cursor()
    cursor.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
    db.commit()
    db.close()


def delete_tasks_bulk(task_ids):
    if not task_ids:
        return
    db, cursor = get_cursor()
    try:
        placeholders = ",".join(["%s"] * len(task_ids))
        cursor.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", tuple(task_ids))
        db.commit()
    finally:
        db.close()


def parse_task_ids(task_ids_text):
    # 複数指定は 1,2,5 のようなカンマ区切りを想定する。
    # 空要素や数値以外はエラーにする。
    ids = []
    for part in task_ids_text.split(","):
        value = part.strip()
        if not value:
            raise ValueError("empty task id")
        if not value.isdigit():
            raise ValueError("invalid task id")
        ids.append(int(value))
    return ids


def filter_accessible_tasks(task_ids, user_id, manager):
    found_tasks = []
    missing_ids = []
    forbidden_ids = []

    for task_id in task_ids:
        task = next((t for t in tasks_list if t["id"] == task_id), None)
        if not task:
            missing_ids.append(task_id)
            continue
        if user_id != task["owner_id"] and not manager:
            forbidden_ids.append(task_id)
            continue
        found_tasks.append(task)

    return found_tasks, missing_ids, forbidden_ids


def get_filtered_tasks_for_user(guild_id, user_id, manager, channel_id=None, status=None, mine_only=False, owner_id=None, category=None):
    filtered = []
    for task in tasks_list:
        if task["guild_id"] != guild_id:
            continue
        if channel_id is not None and task["channel_id"] != channel_id:
            continue
        if status and task["status"] != status:
            continue
        if category and normalize_task_category(task.get("category")) != normalize_task_category(category):
            continue
        if owner_id is not None and task["owner_id"] != owner_id:
            continue
        if mine_only or not manager:
            if task["owner_id"] != user_id:
                continue
        filtered.append(task)

    return filtered


def sort_tasks(tasks, sort_by="due", order="asc"):
    reverse = order == "desc"

    def sort_key(task):
        due = normalize_due(task["due"])
        if sort_by == "task":
            return (str(task["task"]).lower(), due, task["id"])
        if sort_by == "category":
            return (normalize_task_category(task.get("category")), due, task["id"])
        if sort_by == "status":
            return (task["status"] != "todo", due, task["id"])
        return (due, task["id"])

    return sorted(tasks, key=sort_key, reverse=reverse)


def format_task_choice_name(task):
    task_name = str(task["task"]).replace("\n", " ").strip()
    category = normalize_task_category(task.get("category"))
    due = normalize_due(task["due"]).strftime("%m/%d %H:%M")
    suffix = f" ({task['status']} / {category} / {due})"
    label = f"[{task['id']}] {task_name}{suffix}"
    if len(label) <= 100:
        return label

    prefix = f"[{task['id']}] "
    max_task_len = max(0, 100 - len(prefix) - len(suffix) - 1)
    shortened = task_name[:max_task_len] + "…" if max_task_len < len(task_name) else task_name
    return f"{prefix}{shortened}{suffix}"


async def get_accessible_autocomplete_tasks(interaction: discord.Interaction):
    if not interaction.guild:
        return []

    manager = is_manager(interaction)
    return get_filtered_tasks_for_user(interaction.guild.id, interaction.user.id, manager)


def filter_task_choices(tasks, query):
    query = (query or "").strip().lower()
    if not query:
        return tasks[:25]

    matched = []
    for task in tasks:
        if query in str(task["id"]).lower() or query in str(task["task"]).lower():
            matched.append(task)
    return matched[:25]


def is_manager(interaction):
    try:
        settings = get_guild_settings(interaction.guild.id)
        role_id = settings.get("manager_role_id")
        if not role_id:
            return False
        return any(role.id == role_id for role in interaction.user.roles)
    except Exception as e:
        print("[manager] error:", e)
        return False


def can_manage_settings(interaction):
    if interaction.user.guild_permissions.manage_guild:
        return True
    return is_manager(interaction)


def is_manager_member(member, guild_settings):
    role_id = guild_settings.get("manager_role_id") if guild_settings else None
    if not member or not role_id:
        return False
    return any(role.id == role_id for role in member.roles)


def resolve_notification_channel_id(task):
    if task.get("notify_channel_id"):
        return task["notify_channel_id"]

    settings = get_guild_settings(task["guild_id"])
    if settings.get("notify_channel_id"):
        return settings["notify_channel_id"]

    return task["channel_id"]


def build_manager_mention(task):
    # 個別通知先が設定されている場合は、manager_role メンションを付けない。
    if task.get("notify_channel_id"):
        return ""

    settings = get_guild_settings(task["guild_id"])
    if settings.get("notify_channel_id") and settings.get("manager_role_id"):
        return f"<@&{settings['manager_role_id']}> "
    return ""


async def get_target_channel(channel_id):
    # 既存コードは bot.get_channel() のキャッシュに依存していたため、
    # slash command 直後や別チャンネル指定時に channel が取れず通知が落ちることがあった。
    channel = bot.get_channel(channel_id)
    if channel:
        return channel

    try:
        fetched = await bot.fetch_channel(channel_id)
    except Exception as e:
        print("[notify] fetch_channel error:", channel_id, e)
        return None

    if isinstance(fetched, discord.TextChannel):
        return fetched

    print("[notify] unsupported channel type:", channel_id, type(fetched).__name__)
    return None


async def send_task_notification(task, message, view=None):
    return await send_task_notification_with_mentions(task, message, view=view)


async def send_task_notification_with_mentions(task, message, allowed_mentions_override=None, view=None):
    # 通知優先順位:
    # 1. tasks.notify_channel_id
    # 2. guild_settings.notify_channel_id
    # 3. channel_id
    if not is_valid_task_guild(task):
        print("[notify] invalid guild:", task.get("id"), task.get("guild_id"), task.get("task"))
        return False

    channel_id = resolve_notification_channel_id(task)
    channel = await get_target_channel(channel_id)
    if not channel:
        print("[notify] channel not found:", channel_id, task.get("task"))
        return False

    settings = get_guild_settings(task["guild_id"])
    guild = bot.get_guild(task["guild_id"])
    owner_member = None
    if guild and task.get("owner_id"):
        owner_member = guild.get_member(task["owner_id"])
        if owner_member is None:
            try:
                owner_member = await guild.fetch_member(task["owner_id"])
            except Exception as e:
                print("[notify] fetch_member error:", task.get("owner_id"), e)

    if owner_member is None:
        allowed_mentions = discord.AllowedMentions(
            roles=False,
            users=False,
            everyone=False,
        )
    elif is_manager_member(owner_member, settings):
        allowed_mentions = discord.AllowedMentions(
            roles=True,
            users=True,
            everyone=True,
        )
    else:
        allowed_mentions = discord.AllowedMentions(
            roles=True,
            users=False,
            everyone=False,
        )

    content = f"{build_manager_mention(task)}{message}"
    if allowed_mentions_override is not None:
        allowed_mentions = allowed_mentions_override
    try:
        await channel.send(
            content,
            allowed_mentions=allowed_mentions,
            view=view,
        )
    except Exception as e:
        print("[notify] send error:", channel_id, task.get("task"), e)
        return False

    return True


def format_delete_log_message(executor_name, tasks_for_log, target_owner_name=None):
    count = len(tasks_for_log)
    lines = [
        "🗑【削除】",
        f"実行者: {executor_name}",
    ]
    if target_owner_name:
        lines.append(f"対象: {target_owner_name}")
    lines.append(f"{count}件削除")

    for task in tasks_for_log[:10]:
        due = task["due"]
        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)
        lines.append(f"[{task['id']}] {task['task']} ({task['status']})")

    if count > 10:
        lines.append("...")

    return "\n".join(lines)


async def send_delete_log(tasks_for_log, executor_name, target_owner_name=None):
    if not tasks_for_log:
        return

    grouped = {}
    for task in tasks_for_log:
        channel_id = resolve_notification_channel_id(task)
        grouped.setdefault(channel_id, []).append(task)

    for grouped_tasks in grouped.values():
        await send_task_notification_with_mentions(
            grouped_tasks[0],
            format_delete_log_message(executor_name, grouped_tasks, target_owner_name=target_owner_name),
            allowed_mentions_override=discord.AllowedMentions.none(),
        )


def format_done_log_message(executor_name, tasks_for_log):
    count = len(tasks_for_log)
    lines = [
        "✅【完了】",
        f"実行者: {executor_name}",
        f"{count}件完了",
    ]

    for task in tasks_for_log[:10]:
        due = task["due"]
        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)
        lines.append(f"[{task['id']}] {task['task']} ({due.strftime('%m/%d %H:%M')})")

    if count > 10:
        lines.append("...")

    return "\n".join(lines)


async def send_done_log(tasks_for_log, executor_name):
    if not tasks_for_log:
        return

    grouped = {}
    for task in tasks_for_log:
        channel_id = resolve_notification_channel_id(task)
        grouped.setdefault(channel_id, []).append(task)

    for channel_id, grouped_tasks in grouped.items():
        channel = await get_target_channel(channel_id)
        if not channel:
            print("[done_log] channel not found:", channel_id)
            continue
        try:
            await channel.send(
                format_done_log_message(executor_name, grouped_tasks),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            print("[done_log] sent:", channel_id, len(grouped_tasks))
        except Exception as e:
            print("[done_log] send error:", channel_id, e)


def format_status_bulk_log_message(executor_label, target_label, tasks_for_log, status):
    count = len(tasks_for_log)
    lines = [
        "🔄 一括ステータス更新",
        f"実行者: {executor_label}",
        f"対象: {target_label}",
        f"件数: {count}件",
        f"→ {status}",
    ]

    preview = tasks_for_log[:5]
    for task in preview:
        lines.append(f"[{task['id']}] {task['task']}")

    if count > 5:
        lines.append(f"その他{count - 5}件...")

    return "\n".join(lines)


async def send_status_bulk_log(tasks_for_log, executor_label, target_label, status):
    if not tasks_for_log:
        return

    grouped = {}
    for task in tasks_for_log:
        channel_id = resolve_notification_channel_id(task)
        grouped.setdefault(channel_id, []).append(task)

    for channel_id, grouped_tasks in grouped.items():
        channel = await get_target_channel(channel_id)
        if not channel:
            print("[status_bulk_log] channel not found:", channel_id)
            continue
        try:
            await channel.send(
                format_status_bulk_log_message(executor_label, target_label, grouped_tasks, status),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            print("[status_bulk_log] send error:", channel_id, e)


def format_due_message(task, due):
    return (
        "⏰【期限】\n"
        f"📌 {task['task']}\n"
        f"🕒 {due.strftime('%m/%d %H:%M')}"
    )


def format_reminder_message(task, due, label):
    return (
        f"🔔【リマインド（{label_to_text(label)}）】\n"
        f"📌 {task['task']}\n"
        f"🕒 {due.strftime('%m/%d %H:%M')}"
    )


def format_daily_message(task, due):
    return (
        "🌙【未完了タスク】\n"
        f"📌 {task['task']}\n"
        f"🕒 {due.strftime('%m/%d %H:%M')}（期限切れ）"
    )


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


class DeleteConfirmView(discord.ui.View):
    def __init__(self, task):
        super().__init__(timeout=30)
        self.task = task
        self.message = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="削除中...", view=None)
        try:
            await run_blocking(delete_task, self.task["id"])
            await run_blocking(load_tasks)
            await send_delete_log([self.task], interaction.user.display_name)
        except Exception as e:
            print("[delete] error:", e)
            await interaction.followup.send("削除失敗", ephemeral=True)
            return

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました", view=None)


class BulkDeleteConfirmView(discord.ui.View):
    def __init__(self, tasks_to_delete):
        super().__init__(timeout=30)
        self.tasks_to_delete = tasks_to_delete
        self.message = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Delete All", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Deleting...", view=None)
        try:
            await run_blocking(delete_tasks_bulk, [task["id"] for task in self.tasks_to_delete])
            await run_blocking(load_tasks)
            await send_delete_log(self.tasks_to_delete, interaction.user.display_name)
        except Exception as e:
            print("[bulk_delete] error:", e)
            await interaction.followup.send("Bulk delete failed", ephemeral=True)
            return

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled", view=None)


class BulkActionConfirmView(discord.ui.View):
    def __init__(self, action_name, tasks_to_apply, callback):
        super().__init__(timeout=30)
        self.action_name = action_name
        self.tasks_to_apply = tasks_to_apply
        self.callback = callback
        self.message = None

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="はい", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=f"{self.action_name}中...", view=None)
        try:
            result = await self.callback(self.tasks_to_apply, interaction)
        except Exception as e:
            print("[bulk_action] error:", e)
            await interaction.followup.send(f"{self.action_name}に失敗しました", ephemeral=True)
            return
        if result is not False:
            await interaction.followup.send(f"{len(self.tasks_to_apply)}件{self.action_name}しました", ephemeral=True)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました", view=None)


class NotificationActionView(discord.ui.View):
    def __init__(self, task_id):
        super().__init__(timeout=86400)
        self.task_id = task_id

    async def _disable(self, interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    async def _load_task_for_action(self, interaction):
        task = await run_blocking(get_task_by_id, self.task_id)
        if not task:
            await interaction.response.send_message("Task not found", ephemeral=True)
            return None

        manager = is_manager(interaction)
        if interaction.user.id != task["owner_id"] and not manager:
            await interaction.response.send_message("Not allowed", ephemeral=True)
            return None

        if task["status"] == "done":
            await interaction.response.send_message("Already done", ephemeral=True)
            return None

        return task

    @discord.ui.button(label="Done", style=discord.ButtonStyle.success)
    async def done_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        task = await self._load_task_for_action(interaction)
        if not task:
            return

        await run_blocking(update_status, self.task_id, "done")
        await run_blocking(load_tasks)
        await send_done_log([task], interaction.user.display_name)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Keep Todo", style=discord.ButtonStyle.secondary)
    async def keep_todo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        task = await self._load_task_for_action(interaction)
        if not task:
            return
        await self._disable(interaction)


class TaskPickerSelect(discord.ui.Select):
    def __init__(self, parent_view, tasks_page):
        self.parent_view = parent_view
        self.tasks_page = tasks_page
        options = [
            discord.SelectOption(label=format_task_choice_name(task), value=str(task["id"]))
            for task in tasks_page
        ]
        super().__init__(
            placeholder="タスクを選択",
            min_values=0,
            max_values=max(1, len(options)),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        page_task_ids = {task["id"] for task in self.tasks_page}
        self.parent_view.selected_ids.difference_update(page_task_ids)
        self.parent_view.selected_ids.update(int(value) for value in self.values)
        await interaction.response.edit_message(
            content=self.parent_view.render_content(),
            view=self.parent_view.rebuild(),
        )


class TaskActionsView(discord.ui.View):
    def __init__(self, tasks_for_ui, guild_id, owner_user_id, manager, channel_id=None, status_filter=None, mine_only=False, category_filter=None, sort_by="due", order="asc", group_by="none"):
        super().__init__(timeout=180)
        self.tasks_for_ui = tasks_for_ui
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.manager = manager
        self.channel_id = channel_id
        self.status_filter = status_filter
        self.mine_only = mine_only
        self.category_filter = category_filter
        self.sort_by = sort_by
        self.order = order
        self.group_by = group_by
        self.page = 0
        self.selected_ids = set()
        self.message = None
        self._apply_components()

    def current_page_tasks(self):
        start = self.page * 25
        end = start + 25
        return self.tasks_for_ui[start:end]

    def total_pages(self):
        if not self.tasks_for_ui:
            return 1
        return (len(self.tasks_for_ui) - 1) // 25 + 1

    def selected_tasks(self):
        selected = [task for task in self.tasks_for_ui if task["id"] in self.selected_ids]
        selected.sort(key=lambda task: task["id"])
        return selected

    def render_content(self):
        page_tasks = self.current_page_tasks()
        lines = [
            f"Task UI ({self.page + 1}/{self.total_pages()})",
            f"Selected: {len(self.selected_ids)}",
            f"sort: {self.sort_by} ({self.order}) / group: {self.group_by}",
            "",
        ]
        if not page_tasks:
            lines.append("No tasks")
            return "\n".join(lines)

        current_group = None
        for task in page_tasks:
            if self.group_by == "category":
                group_label = f"[{normalize_task_category(task.get('category'))}]"
            elif self.group_by == "due_date":
                group_label = f"[{normalize_due(task['due']).strftime('%m/%d')}]"
            else:
                group_label = None

            if group_label and group_label != current_group:
                if current_group is not None:
                    lines.append("")
                lines.append(group_label)
                current_group = group_label

            marker = "[x]" if task["id"] in self.selected_ids else "[ ]"
            lines.append(f"{marker} {format_task_choice_name(task)}")
        return "\n".join(lines)

    def _apply_components(self):
        self.clear_items()
        page_tasks = self.current_page_tasks()
        if page_tasks:
            self.add_item(TaskPickerSelect(self, page_tasks))
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self.add_item(self.todo_button)
        self.add_item(self.done_button)
        self.add_item(self.delete_button)
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages() - 1
        no_selection = not self.selected_ids
        self.todo_button.disabled = no_selection
        self.done_button.disabled = no_selection
        self.delete_button.disabled = no_selection

    def rebuild(self):
        self._apply_components()
        return self

    async def _run_status(self, interaction, status):
        selected_tasks = self.selected_tasks()
        if not selected_tasks:
            await interaction.response.send_message("タスクを選択してください", ephemeral=True)
            return

        await interaction.response.defer()
        updated_ids = ", ".join(f"[{task['id']}]" for task in selected_tasks)
        done_log_targets = [task for task in selected_tasks if task["status"] == "todo" and status == "done"]

        try:
            await run_blocking(update_status_bulk, [task["id"] for task in selected_tasks], status)
            await run_blocking(load_tasks)
            await send_done_log(done_log_targets, interaction.user.display_name)
        except Exception as e:
            print("[tasks_ui status] error:", e)
            await interaction.followup.send("更新に失敗しました", ephemeral=True)
            return

        self.tasks_for_ui = get_filtered_tasks_for_user(
            self.guild_id,
            self.owner_user_id,
            self.manager,
            channel_id=self.channel_id,
            status=self.status_filter,
            mine_only=self.mine_only,
            category=self.category_filter,
        )
        self.tasks_for_ui = sort_tasks(self.tasks_for_ui, self.sort_by, self.order)
        self.selected_ids.clear()
        self.page = min(self.page, self.total_pages() - 1)
        try:
            await interaction.edit_original_response(content=self.render_content(), view=self.rebuild())
            if not done_log_targets:
                await interaction.followup.send(f"Updated: {updated_ids} -> {status}", ephemeral=True)
        except Exception as e:
            print("[tasks_ui status] edit error:", e)
            await interaction.followup.send("更新は完了しました。UIの更新に失敗しました", ephemeral=True)

    async def _run_delete(self, interaction):
        selected_tasks = self.selected_tasks()
        if not selected_tasks:
            await interaction.response.send_message("タスクを選択してください", ephemeral=True)
            return

        confirm_view = BulkActionConfirmView(
            "削除",
            selected_tasks,
            self._confirm_delete,
        )
        lines = [f"{len(selected_tasks)}件削除します。実行しますか？"]
        for task in selected_tasks[:10]:
            lines.append(format_task_choice_name(task))
        if len(selected_tasks) > 10:
            lines.append("...")
        await interaction.response.send_message("\n".join(lines), ephemeral=True, view=confirm_view)
        confirm_view.message = await interaction.original_response()

    async def _confirm_delete(self, selected_tasks, interaction):
        await run_blocking(delete_tasks_bulk, [task["id"] for task in selected_tasks])
        await run_blocking(load_tasks)
        await send_delete_log(selected_tasks, interaction.user.display_name)
        return False

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(content=self.render_content(), view=self.rebuild())

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages() - 1:
            self.page += 1
        await interaction.response.edit_message(content=self.render_content(), view=self.rebuild())

    @discord.ui.button(label="未完了", style=discord.ButtonStyle.primary, row=1)
    async def todo_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_status(interaction, "todo")

    @discord.ui.button(label="完了", style=discord.ButtonStyle.success, row=1)
    async def done_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_status(interaction, "done")

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, row=1)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_delete(interaction)


@tree.command(name="status", description="Update task status")
async def status_cmd(
    interaction: discord.Interaction,
    task_ids: str,
    status: Literal["todo", "done"],
):
    await interaction.response.send_message("Updating...", ephemeral=True)

    try:
        parsed_ids = parse_task_ids(task_ids)
    except ValueError:
        await interaction.edit_original_response(content="Use task IDs like 1,2,3")
        return

    await run_blocking(load_tasks)
    manager = is_manager(interaction)
    target_tasks, missing_ids, forbidden_ids = filter_accessible_tasks(parsed_ids, interaction.user.id, manager)

    if not target_tasks:
        messages = []
        if missing_ids:
            messages.append(f"Missing IDs: {', ' .join(map(str, missing_ids))}")
        if forbidden_ids:
            messages.append(f"Forbidden IDs: {', ' .join(map(str, forbidden_ids))}")
        await interaction.edit_original_response(content="\n".join(messages) if messages else "No matching tasks")
        return

    done_log_targets = [task for task in target_tasks if task["status"] == "todo" and status == "done"]
    try:
        for task in target_tasks:
            await run_blocking(update_status, task["id"], status)
        await run_blocking(load_tasks)
        await send_done_log(done_log_targets, interaction.user.display_name)
    except Exception as e:
        print("[status] error:", e)
        await interaction.edit_original_response(content="Update failed")
        return

    messages = [f"[{task['id']}] {task['task']} → {status}" for task in target_tasks]
    messages.append(f"ステータス更新: {status}")
    if missing_ids:
        messages.append(f"Missing IDs: {', ' .join(map(str, missing_ids))}")
    if forbidden_ids:
        messages.append(f"Forbidden IDs: {', ' .join(map(str, forbidden_ids))}")
    if status == "done" and not missing_ids and not forbidden_ids:
        await interaction.delete_original_response()
        return
    await interaction.edit_original_response(content="\n".join(messages))


@status_cmd.autocomplete("task_ids")
async def status_task_ids_autocomplete(interaction: discord.Interaction, current: str):
    try:
        tasks = await get_accessible_autocomplete_tasks(interaction)

        raw = current or ""
        parts = [part.strip() for part in raw.split(",")]
        prefix_parts = parts[:-1]
        current_part = parts[-1] if parts else ""
        selected_ids = {part for part in prefix_parts if part.isdigit()}

        choices = []
        for task in filter_task_choices(tasks, current_part):
            task_id_text = str(task["id"])
            if task_id_text in selected_ids:
                continue

            choice_value_parts = prefix_parts + [task_id_text]
            choice_value = ",".join(part for part in choice_value_parts if part)
            if raw.endswith(","):
                choice_value = raw + task_id_text

            choices.append(
                app_commands.Choice(
                    name=format_task_choice_name(task),
                    value=choice_value,
                )
            )
            if len(choices) >= 25:
                break

        print("[autocomplete status]", interaction.guild.id if interaction.guild else None, current, len(choices))
        return choices
    except Exception as e:
        print("[autocomplete status] error:", e)
        return []


@tree.command(name="add", description="タスク追加")
async def add(
    interaction: discord.Interaction,
    task_name: str,
    dt_str: str = None,
    reminders: str = None,
    channel: Optional[discord.TextChannel] = None,
    category: TaskCategory = "general",
):
    print("[add] start")
    print("[add] guild:", interaction.guild.id if interaction.guild else None)
    print("[add] source_channel:", interaction.channel.id if interaction.channel else None)
    print("[add] notify_channel_arg:", channel.id if channel else None)
    try:
        await interaction.response.send_message(
            "追加中...",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        pass

    now = datetime.datetime.now(JST)
    try:
        due = parse_datetime_input(dt_str)
    except Exception as e:
        print("[add] datetime parse error:", e)
        await interaction.edit_original_response(content="日時形式エラー")
        return

    try:
        reminder_data = parse_reminders(reminders) if reminders else DEFAULT_REMINDERS
    except Exception:
        await interaction.edit_original_response(content="リマインド形式エラー")
        return

    filtered = []
    for label, days in reminder_data:
        remind_time = due - datetime.timedelta(days=days)
        if remind_time > now:
            filtered.append({"label": label, "days": days})

    try:
        inserted = await run_blocking(
            insert_task_once,
            interaction.id,
            task_name,
            due,
            interaction.channel.id,
            channel.id if channel else None,
            interaction.user.id,
            interaction.guild.id,
            filtered,
            category,
        )
    except Exception as e:
        print("[add] db error:", e)
        await interaction.edit_original_response(content="DBエラー")
        return

    if inserted is not True:
        await interaction.edit_original_response(
            content="この追加操作はすでに処理されています",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    reminder_text = ", ".join(label_to_text(r["label"]) for r in filtered) if filtered else "なし"
    channel_text = f"<#{channel.id}>" if channel else "デフォルト"

    await interaction.edit_original_response(
        content=(
            "✅ タスク追加\n"
            f"📌 {task_name}\n"
            f"🕒 {due.strftime('%m/%d %H:%M')}\n"
            f"🔔 {reminder_text}\n"
            f"📢 {channel_text}\n"
            f"📂 {category}"
        ),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await send_task_notification_with_mentions(
        {
            "task": task_name,
            "due": due,
            "channel_id": interaction.channel.id,
            "notify_channel_id": channel.id if channel else None,
            "owner_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "category": category,
        },
        f"Task added: {task_name}\nDue: {due.strftime('%m/%d %H:%M')}",
        allowed_mentions_override=discord.AllowedMentions.none(),
    )
    asyncio.create_task(run_blocking(load_tasks))


@add.autocomplete("dt_str")
async def add_dt_autocomplete(interaction: discord.Interaction, current: str):
    now = datetime.datetime.now(JST)
    if not current:
        return [
            app_commands.Choice(name="明日 -> 00:00", value="明日"),
            app_commands.Choice(name="3時間後", value="3時間後"),
            app_commands.Choice(name="今日18:00", value="1800"),
        ]

    suggestions = []
    stripped = current.strip()

    if "明日" in stripped:
        try:
            due = parse_datetime_input(stripped)
            suggestions.append(app_commands.Choice(name=f"{stripped} -> {due.strftime('%m/%d %H:%M')}", value=stripped))
        except Exception:
            pass

    if "/" in stripped:
        try:
            due = parse_datetime_input(stripped)
            suggestions.append(app_commands.Choice(name=f"{stripped} -> {due.strftime('%m/%d %H:%M')}", value=stripped))
        except Exception:
            pass

    if stripped.endswith("後") or any(unit in stripped for unit in ["分後", "時間後", "日後"]):
        try:
            due = parse_datetime_input(stripped)
            suggestions.append(app_commands.Choice(name=f"{stripped} -> {due.strftime('%m/%d %H:%M')}", value=stripped))
        except Exception:
            pass

    if "今" in stripped or "now" in stripped.lower():
        try:
            due = parse_datetime_input(stripped)
            suggestions.append(app_commands.Choice(name=f"{stripped} ({due.strftime('%m/%d %H:%M')})", value=stripped))
        except Exception:
            pass

    try:
        due = parse_datetime_input(stripped)
        diff = due - now
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        parts = []
        if days > 0:
            parts.append(f"{days}日")
        if hours > 0:
            parts.append(f"{hours}時間")
        if minutes > 0:
            parts.append(f"{minutes}分")
        remain_text = "".join(parts) if parts else "すぐ"
        label = f"{stripped} -> {due.strftime('%m/%d %H:%M')} ({remain_text}後)"
        suggestions.append(app_commands.Choice(name=label, value=stripped))
    except Exception:
        if not suggestions:
            return [app_commands.Choice(name="形式エラー", value=current)]

    deduped = []
    seen = set()
    for choice in suggestions:
        key = (choice.name, choice.value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(choice)
    return deduped[:25]


@tree.command(name="delete", description="タスク削除")
async def delete_task_cmd(interaction: discord.Interaction, task_ids: str):
    await interaction.response.defer(ephemeral=True)

    try:
        parsed_ids = parse_task_ids(task_ids)
    except ValueError:
        await interaction.edit_original_response(content="task_id は 1,2,3 の形式で入力してください")
        return

    await run_blocking(load_tasks)
    manager = is_manager(interaction)
    target_tasks, missing_ids, forbidden_ids = filter_accessible_tasks(parsed_ids, interaction.user.id, manager)

    if not target_tasks:
        messages = []
        if missing_ids:
            messages.append(f"見つからないID: {', '.join(map(str, missing_ids))}")
        if forbidden_ids:
            messages.append(f"権限なしID: {', '.join(map(str, forbidden_ids))}")
        await interaction.edit_original_response(content="\n".join(messages) if messages else "削除対象なし")
        return

    if len(target_tasks) == 1:
        task = target_tasks[0]
        view = DeleteConfirmView(task)
        await interaction.edit_original_response(content=f"削除しますか\n[{task['id']}] {task['task']}", view=view)
        view.message = await interaction.original_response()
        return

    lines = ["以下をまとめて削除しますか"]
    for task in target_tasks:
        lines.append(f"[{task['id']}] {task['task']}")
    if missing_ids:
        lines.append(f"見つからないID: {', '.join(map(str, missing_ids))}")
    if forbidden_ids:
        lines.append(f"権限なしID: {', '.join(map(str, forbidden_ids))}")

    view = BulkDeleteConfirmView(target_tasks)
    await interaction.edit_original_response(content="\n".join(lines), view=view)
    view.message = await interaction.original_response()


@delete_task_cmd.autocomplete("task_ids")
async def delete_task_ids_autocomplete(interaction: discord.Interaction, current: str):
    try:
        tasks = await get_accessible_autocomplete_tasks(interaction)

        raw = current or ""
        parts = [part.strip() for part in raw.split(",")]
        prefix_parts = parts[:-1]
        current_part = parts[-1] if parts else ""
        selected_ids = {part for part in prefix_parts if part.isdigit()}

        choices = []
        for task in filter_task_choices(tasks, current_part):
            task_id_text = str(task["id"])
            if task_id_text in selected_ids:
                continue

            choice_value_parts = prefix_parts + [task_id_text]
            choice_value = ",".join(part for part in choice_value_parts if part)
            if raw.endswith(","):
                choice_value = raw + task_id_text

            choices.append(
                app_commands.Choice(
                    name=format_task_choice_name(task),
                    value=choice_value,
                )
            )
            if len(choices) >= 25:
                break

        print("[autocomplete delete]", interaction.guild.id if interaction.guild else None, current, len(choices))
        return choices
    except Exception as e:
        print("[autocomplete delete] error:", e)
        return []


@tree.command(name="edit", description="タスク編集")
async def edit_task_cmd(
    interaction: discord.Interaction,
    task_id: str,
    task_name: str = None,
    dt_str: str = None,
    reminders: str = None,
    channel: Optional[discord.TextChannel] = None,
    category: Optional[TaskCategory] = None,
):
    try:
        parsed_task_id = int(task_id)
    except ValueError:
        await interaction.response.send_message("task_id は数値で入力してください", ephemeral=True)
        return

    print("[edit] start", parsed_task_id)
    print("[edit] guild:", interaction.guild.id if interaction.guild else None)
    print("[edit] source_channel:", interaction.channel.id if interaction.channel else None)
    print("[edit] notify_channel_arg:", channel.id if channel else None)
    try:
        await interaction.response.send_message("処理中...", ephemeral=True)
    except Exception:
        pass

    await run_blocking(load_tasks)
    task = next((t for t in tasks_list if t["id"] == parsed_task_id), None)
    if not task:
        await interaction.edit_original_response(content="タスクが見つからない")
        return
    if not (interaction.user.id == task["owner_id"] or is_manager(interaction)):
        await interaction.edit_original_response(content="権限がありません")
        return

    await run_blocking(clear_notified, task["id"])

    old_due = task["due"]
    if old_due.tzinfo is None:
        old_due = old_due.replace(tzinfo=JST)

    new_name = task_name if task_name else task["task"]
    new_notify_channel_id = channel.id if channel else task.get("notify_channel_id")
    new_category = category if category else normalize_task_category(task.get("category"))

    try:
        if reminders:
            new_reminders = [{"label": label, "days": days} for label, days in parse_reminders(reminders)]
        else:
            new_reminders = task.get("reminders", [])
    except Exception:
        await interaction.edit_original_response(content="リマインド形式エラー")
        return

    try:
        new_due = parse_datetime_input(dt_str) if dt_str else old_due
    except Exception as e:
        print("[edit] datetime parse error:", e)
        await interaction.edit_original_response(content="日時形式エラー")
        return

    try:
        await run_blocking(update_task_full, task["id"], new_name, new_due, new_reminders, new_notify_channel_id, new_category)
        await run_blocking(load_tasks)
    except Exception as e:
        print("[edit] error:", e)
        await interaction.edit_original_response(content="編集失敗")
        return

    reminder_text = ", ".join(r["label"] for r in new_reminders) if new_reminders else "none"
    channel_text = f"<#{new_notify_channel_id}>" if new_notify_channel_id else "inherit from guild/default"
    await interaction.edit_original_response(
        content=(
            f"Updated\n"
            f"[{parsed_task_id}] {new_name}\n"
            f"{new_due.strftime('%m/%d %H:%M')}\n"
            f"reminders: {reminder_text}\n"
            f"channel: {channel_text}\n"
            f"category: {new_category}"
        )
    )


@edit_task_cmd.autocomplete("task_id")
async def edit_task_id_autocomplete(interaction: discord.Interaction, current: str):
    try:
        tasks = await get_accessible_autocomplete_tasks(interaction)
        choices = [
            app_commands.Choice(name=format_task_choice_name(task), value=str(task["id"]))
            for task in filter_task_choices(tasks, current)
        ]
        print("[autocomplete edit]", interaction.guild.id if interaction.guild else None, current, len(choices))
        return choices
    except Exception as e:
        print("[autocomplete edit] error:", e)
        return []


@edit_task_cmd.autocomplete("dt_str")
async def edit_dt_autocomplete(interaction: discord.Interaction, current: str):
    return await add_dt_autocomplete(interaction, current)


@tree.command(name="tasks", description="タスクを UI で操作")
async def tasks_ui(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    status: Optional[Literal["todo", "done"]] = None,
    mine_only: bool = False,
    category: Optional[TaskCategory] = None,
    sort_by: TaskSortBy = "due",
    order: TaskSortOrder = "asc",
    group_by: TaskGroupBy = "none",
):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await run_blocking(load_tasks)
    manager = is_manager(interaction)
    source_channel_id = channel.id if channel else None
    filtered_tasks = get_filtered_tasks_for_user(
        interaction.guild.id,
        interaction.user.id,
        manager,
        channel_id=source_channel_id,
        status=status,
        mine_only=mine_only,
        category=category,
    )
    filtered_tasks = sort_tasks(filtered_tasks, sort_by, order)

    view = TaskActionsView(
        filtered_tasks,
        interaction.guild.id,
        interaction.user.id,
        manager,
        channel_id=source_channel_id,
        status_filter=status,
        mine_only=mine_only,
        category_filter=category,
        sort_by=sort_by,
        order=order,
        group_by=group_by,
    )
    await interaction.edit_original_response(content=view.render_content(), view=view)
    view.message = await interaction.original_response()


@tree.command(name="delete_bulk", description="条件に一致するタスクを一括削除")
async def delete_bulk(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    status: Optional[Literal["todo", "done"]] = None,
    mine_only: bool = False,
    owner: Optional[discord.Member] = None,
):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return

    await interaction.response.send_message("対象を確認中...", ephemeral=True)
    await run_blocking(load_tasks)
    manager = is_manager(interaction)
    if owner is not None and not manager:
        await interaction.edit_original_response(content="owner 指定は manager_role のみ使用できます")
        return
    if owner is not None and mine_only:
        await interaction.edit_original_response(content="owner と mine_only は併用できません")
        return
    if owner is not None and owner.id == interaction.user.id and not mine_only:
        await interaction.edit_original_response(content="owner に自分を指定する場合は mine_only を使ってください")
        return
    target_tasks = get_filtered_tasks_for_user(
        interaction.guild.id,
        interaction.user.id,
        manager,
        channel_id=channel.id if channel else None,
        status=status,
        mine_only=mine_only,
        owner_id=owner.id if owner else None,
    )

    if not target_tasks:
        await interaction.edit_original_response(content="対象タスクがありません")
        return

    async def do_delete(tasks_to_delete, interaction_for_log):
        await run_blocking(delete_tasks_bulk, [task["id"] for task in tasks_to_delete])
        await run_blocking(load_tasks)
        await send_delete_log(
            tasks_to_delete,
            interaction_for_log.user.display_name,
            target_owner_name=owner.display_name if owner else None,
        )
        return False

    view = BulkActionConfirmView("削除", target_tasks, do_delete)
    if owner is not None:
        lines = [f"{owner.display_name} のタスクを{len(target_tasks)}件削除します。実行しますか？"]
    else:
        lines = [f"{len(target_tasks)}件削除します。実行しますか？"]
    for task in target_tasks[:10]:
        lines.append(format_task_choice_name(task))
    if len(target_tasks) > 10:
        lines.append("...")
    await interaction.edit_original_response(content="\n".join(lines), view=view)
    view.message = await interaction.original_response()


@tree.command(name="status_bulk", description="条件に一致するタスクを一括更新")
async def status_bulk(
    interaction: discord.Interaction,
    status: Literal["todo", "done"],
    channel: Optional[discord.TextChannel] = None,
    mine_only: bool = False,
    owner: Optional[discord.Member] = None,
):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return

    await interaction.response.send_message("対象を確認中...", ephemeral=True)
    await run_blocking(load_tasks)
    manager = is_manager(interaction)
    if owner is not None and owner.id != interaction.user.id and not manager:
        await interaction.edit_original_response(content="他人のタスクを変更できるのは manager_role のみです")
        return
    if not manager:
        last_used = status_bulk_cooldowns.get(interaction.user.id)
        now = datetime.datetime.now(JST)
        if last_used and (now - last_used).total_seconds() < STATUS_BULK_COOLDOWN_SECONDS:
            await interaction.edit_original_response(content="少し待ってから再実行してください")
            return
        status_bulk_cooldowns[interaction.user.id] = now

    target_tasks = get_filtered_tasks_for_user(
        interaction.guild.id,
        interaction.user.id,
        manager,
        channel_id=channel.id if channel else None,
        mine_only=mine_only,
        owner_id=owner.id if owner else None,
    )

    if not target_tasks:
        await interaction.edit_original_response(content="対象タスクがありません")
        return

    if owner is not None:
        target_label = "自分" if owner.id == interaction.user.id else owner.mention
    elif mine_only or not manager:
        target_label = "自分"
    else:
        target_label = "条件一致"

    async def do_status(tasks_to_apply, interaction_for_log):
        await run_blocking(update_status_bulk, [task["id"] for task in tasks_to_apply], status)
        await run_blocking(load_tasks)
        await send_status_bulk_log(
            tasks_to_apply,
            interaction_for_log.user.mention,
            target_label,
            status,
        )
        return False

    view = BulkActionConfirmView(f"status を {status} に変更", target_tasks, do_status)
    lines = [f"{len(target_tasks)}件を {status} に変更します。実行しますか？"]
    for task in target_tasks[:10]:
        lines.append(format_task_choice_name(task))
    if len(target_tasks) > 10:
        lines.append("...")
    await interaction.edit_original_response(content="\n".join(lines), view=view)
    view.message = await interaction.original_response()


@tree.command(name="help", description="コマンド一覧")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        content=(
            "利用できるコマンド\n"
            "/add タスク追加\n"
            "/tasks UI で操作\n"
            "/delete ID指定で削除\n"
            "/delete_bulk 条件で一括削除\n"
            "/status 状態変更\n"
            "/search キーワード検索"
        ),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="search", description="キーワードでタスク検索")
async def search_tasks(
    interaction: discord.Interaction,
    keyword: str,
    channel: Optional[discord.TextChannel] = None,
    mine_only: bool = False,
    category: Optional[TaskCategory] = None,
    sort_by: TaskSortBy = "due",
    order: TaskSortOrder = "asc",
):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return

    await interaction.response.send_message("検索中...", ephemeral=True)
    await run_blocking(load_tasks)
    manager = is_manager(interaction)
    candidates = get_filtered_tasks_for_user(
        interaction.guild.id,
        interaction.user.id,
        manager,
        channel_id=channel.id if channel else None,
        mine_only=mine_only,
        category=category,
    )

    keyword_lower = keyword.strip().lower()
    results = [task for task in candidates if keyword_lower in str(task["task"]).lower()]
    results = sort_tasks(results, sort_by, order)

    if not results:
        await interaction.edit_original_response(content="一致するタスクはありません")
        return

    lines = [f"検索結果: {len(results)}件"]
    for task in results[:20]:
        lines.append(format_task_choice_name(task))
    if len(results) > 20:
        lines.append("...")

    await interaction.edit_original_response(
        content="\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="list", description="Task list")
async def list_tasks(
    interaction: discord.Interaction,
    mode: Literal["todo", "done", "all"] = "todo",
    category: Optional[TaskCategory] = None,
    sort_by: TaskSortBy = "due",
    order: TaskSortOrder = "asc",
):
    await interaction.response.send_message("Loading...", ephemeral=True)
    now = datetime.datetime.now(JST)
    await run_blocking(load_tasks)

    if not tasks_list:
        await interaction.edit_original_response(content="No tasks")
        return
    if not interaction.guild:
        await interaction.edit_original_response(content="Use this command in a server")
        return

    filtered_tasks = []
    for task in tasks_list:
        if task["guild_id"] != interaction.guild.id:
            continue
        if mode != "all" and task["channel_id"] != interaction.channel.id:
            continue
        if mode == "todo" and task["status"] != "todo":
            continue
        if mode == "done" and task["status"] != "done":
            continue
        if category and normalize_task_category(task.get("category")) != normalize_task_category(category):
            continue
        filtered_tasks.append(task)

    filtered_tasks = sort_tasks(filtered_tasks, sort_by, order)
    msg = f"Task list ({mode}) / sort: {sort_by} ({order})\n"
    i = 1
    for task in filtered_tasks:
        due = normalize_due(task["due"])
        msg += f"{i}. [{task['id']}] {task['task']} ({normalize_task_category(task.get('category'))})\n"
        msg += f"Due: {due.strftime('%m/%d %H:%M')}\n"
        if task.get("notify_channel_id"):
            msg += f"Notify: <#{task['notify_channel_id']}>\n"

        remaining = []
        for reminder in task.get("reminders", []):
            remind_time = due - datetime.timedelta(days=reminder["days"])
            if remind_time <= now:
                continue
            if reminder["label"] not in task.get("notified", []):
                remaining.append(label_to_text(reminder["label"]))
        if remaining:
            msg += "Reminders: " + ", ".join(remaining) + "\n"
        msg += "\n"
        i += 1

    await interaction.edit_original_response(content=msg)


@tasks.loop(seconds=30)
async def reminder_loop():
    now = datetime.datetime.now(JST)
    print("[reminder] loop")
    await run_blocking(load_tasks)

    for task in tasks_list:
        if task["status"] == "done":
            continue
        if not is_valid_task_guild(task):
            print("[reminder] skip invalid guild:", task["id"], task.get("guild_id"), task["task"])
            continue

        due = task["due"]
        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)

        notified = list(task.get("notified", []))
        today_str = now.strftime("%Y-%m-%d")

        if (
            task["status"] != "done"
            and now > due
            and now.date() > due.date()
            and now.hour == 9
            and now.minute == 0
        ):
            if today_str not in notified:
                current_status = await run_blocking(get_task_status, task["id"])
                if current_status is None or current_status == "done":
                    print("[reminder] skip daily:", task["id"], current_status)
                    continue
                print("[reminder] send daily:", task["id"], task["task"])
                await send_task_notification(task, format_daily_message(task, due))
                notified.append(today_str)
                await run_blocking(append_notified, task["id"], notified)
                task["notified"] = notified

        notified = list(task.get("notified", []))
        if "due" not in notified and now >= due:
            current_status = await run_blocking(get_task_status, task["id"])
            if current_status is None or current_status == "done":
                print("[reminder] skip due:", task["id"], current_status)
                continue
            print("[reminder] send due:", task["id"], task["task"])
            await send_task_notification(task, format_due_message(task, due), view=NotificationActionView(task["id"]))
            notified.append("due")
            await run_blocking(append_notified, task["id"], notified)
            task["notified"] = notified

        notified = list(task.get("notified", []))
        for reminder in task.get("reminders", []):
            label = reminder["label"]
            remind_time = due - datetime.timedelta(days=reminder["days"])
            if label in notified:
                continue
            if remind_time <= now <= remind_time + datetime.timedelta(seconds=30):
                current_status = await run_blocking(get_task_status, task["id"])
                if current_status is None or current_status == "done":
                    print("[reminder] skip reminder:", task["id"], label, current_status)
                    continue
                print("[reminder] send reminder:", task["id"], label, task["task"])
                await send_task_notification(task, format_reminder_message(task, due, label), view=NotificationActionView(task["id"]))
                notified.append(label)
                await run_blocking(append_notified, task["id"], notified)
                task["notified"] = notified


@tasks.loop(minutes=5)
async def keep_db_alive():
    try:
        db, cursor = get_cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        db.close()
        print("[db] keep alive")
        if not guild_settings_cache:
            load_guild_settings_cache()
    except Exception as e:
        print("[db] keep alive error:", e)


@tree.command(name="set_notify_channel", description="通知チャンネル設定")
async def set_notify_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return
    if not can_manage_settings(interaction):
        await interaction.response.send_message("manage_guild または manager_role が必要です", ephemeral=True)
        return

    db, cursor = get_cursor()
    cursor.execute(
        """
        INSERT INTO guild_settings (guild_id, notify_channel_id)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE notify_channel_id=%s
        """,
        (interaction.guild.id, channel.id, channel.id),
    )
    db.commit()
    db.close()
    guild_settings_cache[interaction.guild.id] = {
        **get_guild_settings(interaction.guild.id),
        "notify_channel_id": channel.id,
    }

    await interaction.response.send_message(f"通知チャンネル設定: {channel.name}", ephemeral=True)


@tree.command(name="set_manager_role", description="管理ロール設定")
async def set_manager_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return
    if not can_manage_settings(interaction):
        await interaction.response.send_message("manage_guild または manager_role が必要です", ephemeral=True)
        return

    try:
        await interaction.response.send_message("設定中...", ephemeral=True)
    except Exception:
        pass

    try:
        db, cursor = get_cursor()
        cursor.execute(
            """
            INSERT INTO guild_settings (guild_id, manager_role_id)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE manager_role_id=%s
            """,
            (interaction.guild.id, role.id, role.id),
        )
        db.commit()
        db.close()
        guild_settings_cache[interaction.guild.id] = {
            **get_guild_settings(interaction.guild.id),
            "manager_role_id": role.id,
        }
    except Exception as e:
        print("[manager_role] error:", e)
        await interaction.edit_original_response(content="設定失敗")
        return

    await interaction.edit_original_response(content=f"管理ロール設定: {role.name}")


GUILD_ID = int(os.environ.get("GUILD_ID", "1479381180146257950"))


async def sync_guild_commands(guild):
    guild_object = discord.Object(id=guild.id)
    tree.copy_global_to(guild=guild_object)
    await tree.sync(guild=guild_object)
    print("[startup] guild sync done:", guild.id, guild.name)


@bot.event
async def on_ready():
    print("[startup] ready")
    # 実機検証では複数 guild を跨いで使っているため、
    # 1 guild 固定ではなく、参加中の全 guild に guild command を同期する。
    # global sync だけだと反映が遅く、旧定義の slash command を触ってしまう。
    # guild sync を使う場合は、global command を各 guild に明示的にコピーしてから同期する。
    for guild in bot.guilds:
        try:
            await sync_guild_commands(guild)
        except Exception as e:
            print("[startup] guild sync error:", guild.id, e)

    try:
        global_commands = await bot.http.get_global_commands(bot.application_id)
        for command in global_commands:
            await bot.http.delete_global_command(bot.application_id, command["id"])
        print("[startup] cleared global commands:", len(global_commands))
    except Exception as e:
        print("[startup] clear global commands error:", e)

    try:
        await run_blocking(load_guild_settings_cache)
        await run_blocking(load_tasks)
    except Exception as e:
        print("[startup] initial load error:", e)

    print("[startup] command sync done")

    if not reminder_loop.is_running():
        reminder_loop.start()
    if not keep_db_alive.is_running():
        keep_db_alive.start()

    print("[startup] reminder loop started")
    print("[startup] commands:", [c.name for c in tree.get_commands()])


@bot.event
async def on_guild_join(guild):
    try:
        await sync_guild_commands(guild)
    except Exception as e:
        print("[startup] guild join sync error:", guild.id, e)


def start_bot():
    bot.run(os.environ.get("TOKEN"))


if __name__ == "__main__":
    import threading

    threading.Thread(target=run_web, daemon=True).start()
    bot.run(os.environ.get("TOKEN"))
