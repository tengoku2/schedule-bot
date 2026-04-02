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
    ("2weeks", 14),
    ("1week", 7),
    ("3days", 3),
    ("1day", 1),
    ("3hours", 3 / 24),
]


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


def parse_datetime_input(dt_str):
    now = datetime.datetime.now(JST)

    if not dt_str:
        tomorrow = now.date() + datetime.timedelta(days=1)
        return datetime.datetime.combine(tomorrow, datetime.time(0, 0), tzinfo=JST)

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


def load_tasks():
    global tasks_list
    print("[load_tasks] start")
    try:
        db, cursor = get_cursor()
        cursor.execute(
            """
            SELECT id, task, due, channel_id, notify_channel_id, owner_id,
                   visible_to, reminders, notified, status, guild_id
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
            }
            for row in rows
        ]
        db.close()
    except Exception as e:
        print("[load_tasks] error:", e)


def get_guild_settings(guild_id):
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
    return row or {}


def get_notify_channel(guild_id):
    return get_guild_settings(guild_id).get("notify_channel_id")


def insert_task(task_name, due, channel_id, notify_channel_id, user_id, guild_id, reminders):
    db, cursor = get_cursor()
    cursor.execute(
        """
        INSERT INTO tasks
        (task, due, channel_id, notify_channel_id, owner_id, visible_to, roles,
         reminders, notified, mention, everyone, status, guild_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
    )
    db.commit()
    db.close()


def update_task_full(task_id, task_name, due, reminders, notify_channel_id):
    db, cursor = get_cursor()
    cursor.execute(
        """
        UPDATE tasks
        SET task=%s, due=%s, reminders=%s, notify_channel_id=%s
        WHERE id=%s
        """,
        (task_name, due, json.dumps(reminders), notify_channel_id, task_id),
    )
    db.commit()
    db.close()


def update_status(task_id, status):
    db, cursor = get_cursor()
    cursor.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, task_id))
    db.commit()
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


def delete_task(task_id):
    db, cursor = get_cursor()
    cursor.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
    db.commit()
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


async def send_task_notification(task, message):
    # 通知優先順位:
    # 1. tasks.notify_channel_id
    # 2. guild_settings.notify_channel_id
    # 3. channel_id
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
    try:
        await channel.send(
            content,
            allowed_mentions=allowed_mentions,
        )
    except Exception as e:
        print("[notify] send error:", channel_id, task.get("task"), e)
        return False

    return True


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
        except Exception as e:
            print("[delete] error:", e)
            await interaction.followup.send("削除失敗", ephemeral=True)
            return
        await interaction.followup.send(f"削除: {self.task['task']}", ephemeral=True)

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
            for task in self.tasks_to_delete:
                await run_blocking(delete_task, task["id"])
            await run_blocking(load_tasks)
        except Exception as e:
            print("[bulk_delete] error:", e)
            await interaction.followup.send("Bulk delete failed", ephemeral=True)
            return
        deleted_ids = ", ".join(f"[{task['id']}]" for task in self.tasks_to_delete)
        await interaction.followup.send(f"Deleted: {deleted_ids}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled", view=None)


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

    try:
        for task in target_tasks:
            await run_blocking(update_status, task["id"], status)
        await run_blocking(load_tasks)
    except Exception as e:
        print("[status] error:", e)
        await interaction.edit_original_response(content="Update failed")
        return

    updated_ids = ", ".join(f"[{task['id']}]" for task in target_tasks)
    messages = [f"Updated: {updated_ids} -> {status}"]
    if missing_ids:
        messages.append(f"Missing IDs: {', ' .join(map(str, missing_ids))}")
    if forbidden_ids:
        messages.append(f"Forbidden IDs: {', ' .join(map(str, forbidden_ids))}")
    await interaction.edit_original_response(content="\\n".join(messages))


@tree.command(name="add", description="タスク追加")
async def add(
    interaction: discord.Interaction,
    task_name: str,
    dt_str: str = None,
    reminders: str = None,
    channel: Optional[discord.TextChannel] = None,
):
    print("[add] start")
    print("[add] guild:", interaction.guild.id if interaction.guild else None)
    print("[add] source_channel:", interaction.channel.id if interaction.channel else None)
    print("[add] notify_channel_arg:", channel.id if channel else None)
    try:
        await interaction.response.send_message("追加中...", ephemeral=True)
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
        await run_blocking(
            insert_task,
            task_name,
            due,
            interaction.channel.id,
            channel.id if channel else None,
            interaction.user.id,
            interaction.guild.id,
            filtered,
        )
    except Exception as e:
        print("[add] db error:", e)
        await interaction.edit_original_response(content="DBエラー")
        return

    reminder_text = ", ".join(label_to_text(r["label"]) for r in filtered) if filtered else "なし"
    channel_text = f"<#{channel.id}>" if channel else "デフォルト"

    await interaction.edit_original_response(
        content=(
            "✅ タスク追加\n"
            f"📌 {task_name}\n"
            f"🕒 {due.strftime('%m/%d %H:%M')}\n"
            f"🔔 {reminder_text}\n"
            f"📢 {channel_text}"
        )
    )
    await send_task_notification(
        {
            "task": task_name,
            "due": due,
            "channel_id": interaction.channel.id,
            "notify_channel_id": channel.id if channel else None,
            "owner_id": interaction.user.id,
            "guild_id": interaction.guild.id,
        },
        f"Task added: {task_name}\nDue: {due.strftime('%m/%d %H:%M')}",
    )
    asyncio.create_task(run_blocking(load_tasks))


@add.autocomplete("dt_str")
async def add_dt_autocomplete(interaction: discord.Interaction, current: str):
    now = datetime.datetime.now(JST)
    if not current:
        return [
            app_commands.Choice(name="明日の0時", value=""),
            app_commands.Choice(name="3時間後", value="300"),
            app_commands.Choice(name="今日18:00", value="1800"),
        ]
    if not current.isdigit():
        return []
    try:
        due = parse_datetime_input(current)
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
        label = f"{current} -> {due.strftime('%m/%d %H:%M')} ({remain_text}後)"
        return [app_commands.Choice(name=label, value=current)]
    except Exception:
        return [app_commands.Choice(name="形式エラー", value=current)]


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
        await interaction.edit_original_response(content="\\n".join(messages) if messages else "削除対象なし")
        return

    if len(target_tasks) == 1:
        task = target_tasks[0]
        view = DeleteConfirmView(task)
        await interaction.edit_original_response(content=f"削除しますか\\n[{task['id']}] {task['task']}", view=view)
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
    await interaction.edit_original_response(content="\\n".join(lines), view=view)
    view.message = await interaction.original_response()


@tree.command(name="edit", description="タスク編集")
async def edit_task_cmd(
    interaction: discord.Interaction,
    task_id: int,
    task_name: str = None,
    dt_str: str = None,
    reminders: str = None,
    channel: Optional[discord.TextChannel] = None,
):
    print("[edit] start", task_id)
    print("[edit] guild:", interaction.guild.id if interaction.guild else None)
    print("[edit] source_channel:", interaction.channel.id if interaction.channel else None)
    print("[edit] notify_channel_arg:", channel.id if channel else None)
    try:
        await interaction.response.send_message("処理中...", ephemeral=True)
    except Exception:
        pass

    await run_blocking(load_tasks)
    task = next((t for t in tasks_list if t["id"] == task_id), None)
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
        await run_blocking(update_task_full, task["id"], new_name, new_due, new_reminders, new_notify_channel_id)
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
            f"[{task_id}] {new_name}\n"
            f"{new_due.strftime('%m/%d %H:%M')}\n"
            f"reminders: {reminder_text}\n"
            f"channel: {channel_text}"
        )
    )


@edit_task_cmd.autocomplete("dt_str")
async def edit_dt_autocomplete(interaction: discord.Interaction, current: str):
    return await add_dt_autocomplete(interaction, current)


@tree.command(name="list", description="タスク一覧")
async def list_tasks(
    interaction: discord.Interaction,
    mode: Literal["todo", "done", "all"] = "todo",
):
    await interaction.response.send_message("読み込み中...", ephemeral=True)
    now = datetime.datetime.now(JST)
    await run_blocking(load_tasks)

    if not tasks_list:
        await interaction.edit_original_response(content="タスクなし")
        return
    if not interaction.guild:
        await interaction.edit_original_response(content="サーバー内で使ってください")
        return

    msg = f"タスク一覧 ({mode})\n"
    i = 1
    for task in tasks_list:
        if task["guild_id"] != interaction.guild.id:
            continue
        if mode != "all" and task["channel_id"] != interaction.channel.id:
            continue
        if mode == "todo" and task["status"] != "todo":
            continue
        if mode == "done" and task["status"] != "done":
            continue

        due = task["due"]
        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)

        msg += f"{i}. [{task['id']}] {task['task']}\n"
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
                await send_task_notification(task, format_daily_message(task, due))
                notified.append(today_str)
                await run_blocking(append_notified, task["id"], notified)
                task["notified"] = notified

        notified = list(task.get("notified", []))
        if "due" not in notified and now >= due:
            await send_task_notification(task, format_due_message(task, due))
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
                await send_task_notification(task, format_reminder_message(task, due, label))
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
    except Exception as e:
        print("[db] keep alive error:", e)


@tree.command(name="set_notify_channel", description="通知チャンネル設定")
async def set_notify_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("manage_guild 権限が必要です", ephemeral=True)
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

    await interaction.response.send_message(f"通知チャンネル設定: {channel.name}", ephemeral=True)


@tree.command(name="set_manager_role", description="管理ロール設定")
async def set_manager_role(interaction: discord.Interaction, role: discord.Role):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("manage_guild 権限が必要です", ephemeral=True)
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
    except Exception as e:
        print("[manager_role] error:", e)
        await interaction.edit_original_response(content="設定失敗")
        return

    await interaction.edit_original_response(content=f"管理ロール設定: {role.name}")


GUILD_ID = int(os.environ.get("GUILD_ID", "1479381180146257950"))


@bot.event
async def on_ready():
    print("[startup] ready")
    # 実機検証では複数 guild を跨いで使っているため、
    # 1 guild 固定ではなく、参加中の全 guild に guild command を同期する。
    # global sync だけだと反映が遅く、旧定義の slash command を触ってしまう。
    for guild in bot.guilds:
        try:
            await tree.sync(guild=discord.Object(id=guild.id))
            print("[startup] guild sync done:", guild.id, guild.name)
        except Exception as e:
            print("[startup] guild sync error:", guild.id, e)

    try:
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


def start_bot():
    bot.run(os.environ.get("TOKEN"))


if __name__ == "__main__":
    import threading

    threading.Thread(target=run_web, daemon=True).start()
    bot.run(os.environ.get("TOKEN"))
