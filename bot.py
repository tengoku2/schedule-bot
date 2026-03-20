import os
import discord
from discord import app_commands
import datetime
import json
import asyncio
import mysql.connector
import threading
from flask import Flask

# -----------------------
# Flask（Koyeb用）
# -----------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# -----------------------
# DB接続
# -----------------------
def get_db():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST"),
        port=int(os.environ.get("DB_PORT", 15040)),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASS"),
        database=os.environ.get("DB_NAME"),
        ssl_disabled=False,
        ssl_verify_cert=False
    )

def get_cursor():
    db = get_db()
    return db, db.cursor(dictionary=True)

# -----------------------
# 日付パース
# -----------------------
def parse_date(date_str):
    now = datetime.datetime.now()

    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except:
        pass

    if date_str.isdigit():
        if len(date_str) in [3, 4]:
            if len(date_str) == 3:
                month = int(date_str[0])
                day = int(date_str[1:])
            else:
                month = int(date_str[:2])
                day = int(date_str[2:])
            year = now.year
            d = datetime.date(year, month, day)

            if d < now.date():
                d = datetime.date(year + 1, month, day)

            return d

    if "/" in date_str:
        m, d = map(int, date_str.split("/"))
        year = now.year
        d = datetime.date(year, m, d)

        if d < now.date():
            d = datetime.date(year + 1, m, d)

        return d

    raise ValueError("日付形式エラー")

# -----------------------
# 時間パース
# -----------------------
def parse_time(time_str):
    if ":" in time_str:
        return datetime.datetime.strptime(time_str, "%H:%M").time()

    if time_str.isdigit():
        if len(time_str) <= 2:
            return datetime.time(int(time_str), 0)
        elif len(time_str) in [3, 4]:
            hour = int(time_str[:-2])
            minute = int(time_str[-2:])
            return datetime.time(hour, minute)

    raise ValueError("時間形式エラー")

# -----------------------
# REMINDERパース
# -----------------------
def parse_reminders(reminder_str):
    mapping = {
        "m": ("month", 30),
        "w": ("week", 7),
        "d": ("day", 1),
        "h": ("hour", 1/24),
    }

    result = []

    for part in reminder_str.split(","):
        part = part.strip().lower()

        num = int(part[:-1])
        unit = part[-1]

        if unit not in mapping:
            raise ValueError("単位エラー")

        name, base = mapping[unit]

        label = f"{num}{name}"
        days = num * base

        result.append((label, days))

    return result

# -----------------------
# データ
# -----------------------
tasks_list = []

def load_tasks():
    global tasks_list
    print("🔄 load_tasks開始")

    db, cursor = get_cursor()
    cursor.execute("SELECT * FROM tasks")
    rows = cursor.fetchall()

    new_list = []
    for t in rows:
        new_list.append({
            "id": t["id"],
            "task": t["task"],
            "due": t["due"],  # ← そのまま使う
            "channel_id": t["channel_id"],
            "owner_id": t["owner_id"],
            "visible_to": json.loads(t["visible_to"] or "[]"),
            "status": t["status"],
            "notified": json.loads(t["notified"] or "[]"),
            "reminders": json.loads(t["reminders"] or "[]"),
        })

    tasks_list = new_list
    db.close()

# -----------------------
# Discord設定
# -----------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------
# DB INSERT
# -----------------------
def insert_task(task_name, due, channel_id, user_id, reminders):
    db, cursor = get_cursor()

    cursor.execute("""
    INSERT INTO tasks 
    (task, due, channel_id, owner_id, visible_to, roles, reminders, notified, mention, everyone, status)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        task_name,
        due,
        channel_id,
        user_id,
        json.dumps([]),
        json.dumps([]),
        json.dumps([reminders]),
        json.dumps([]),
        False,
        False,
        "todo"
    ))

    db.commit()
    db.close()

# -----------------------
# /add
# -----------------------
@tree.command(name="add", description="タスク追加")
async def add(
    interaction: discord.Interaction,
    task_name: str,
    date_str: str = None,
    time_str: str = None,
    reminders: str = None  # ←追加
):

    await interaction.response.send_message("⏳ 追加中...", ephemeral=True)

    now = datetime.datetime.now()

    try:
        if not date_str and not time_str:
            due = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        elif not date_str:
            t = parse_time(time_str)
            today_due = datetime.datetime.combine(now.date(), t)

            if today_due > now:
                due = today_due
            else:
                due = today_due + datetime.timedelta(days=1)

        elif not time_str:
            d = parse_date(date_str)

            if d == now.date():
                due = datetime.datetime.combine(d, datetime.time(23, 59))
            else:
                due = datetime.datetime.combine(d, datetime.time(0, 0))

        else:
            d = parse_date(date_str)
            t = parse_time(time_str)
            due = datetime.datetime.combine(d, t)

    except Exception:
        await interaction.edit_original_response(content="❌ 日時形式エラー\n例: 320 21 / 3/20 930")
        return

        # リマインド設定
    try:
        if reminders:
            reminder_data = parse_reminders(reminders)
            reminder_labels = [r[0] for r in reminder_data]
        else:
            reminder_labels = [r[0] for r in DEFAULT_REMINDERS]
    except:
        await interaction.edit_original_response(content="❌ リマインド形式エラー（例: 1d,2h）")
        return

    try:
        await asyncio.to_thread(
            insert_task,
            task_name,
            due,
            interaction.channel.id,
            interaction.user.id,
            reminder_labels  # ←これ追加
        )
    except Exception as e:
        print(e)
        await interaction.edit_original_response(content="❌ DBエラー")
        return

    await interaction.edit_original_response(
        content=f"✅ 追加: {task_name}\n📅 {due.strftime('%m/%d %H:%M')}"
    )

    asyncio.create_task(asyncio.to_thread(load_tasks))

# -----------------------
# /list
# -----------------------
@tree.command(name="list", description="タスク一覧")
async def list_tasks(interaction: discord.Interaction):

    await interaction.response.send_message("⏳ 読み込み中...", ephemeral=True)

    if not tasks_list:
        await interaction.edit_original_response(content="📭 タスクなし")
        return

    msg = "📋 タスク一覧\n"
    for i, t in enumerate(tasks_list, 1):
        msg += f"{i}. {t['task']}\n"
        msg += f"📅 {t['due'].strftime('%m/%d %H:%M')}\n\n"

    await interaction.edit_original_response(content=msg)

# -----------------------
# リマインド
# -----------------------
from discord.ext import tasks

DEFAULT_REMINDERS = [
    ("1month", 30),
    ("2weeks", 14),
    ("1week", 7),
    ("3days", 3),
    ("1day", 1),
    ("3hours", 3/24),
]

REMINDERS = [
    ("1month", 30),
    ("2weeks", 14),
    ("1week", 7),
    ("3days", 3),
    ("1day", 1),
    ("3hours", 3/24),
]

def label_to_text(label):
    return {
        "1month": "1ヶ月前",
        "2weeks": "2週間前",
        "1week": "1週間前",
        "3days": "3日前",
        "1day": "24時間前",
        "3hours": "3時間前",
    }.get(label, label)

@tasks.loop(seconds=30)
async def reminder_loop():
    now = datetime.datetime.now()

    for t in tasks_list:
        if t["status"] != "todo":
            continue

        if t["due"] < now:
            continue

        notified = t.get("notified", [])

        reminder_settings = t.get("reminders", [])

        for label in reminder_settings:

            if label in notified:
                continue

            if "month" in label:
                days = int(label.replace("month", "")) * 30
            elif "week" in label:
                days = int(label.replace("week", "")) * 7
            elif "day" in label:
                days = int(label.replace("day", ""))
            elif "hour" in label:
                days = int(label.replace("hour", "")) / 24
            else:
                continue

            remind_time = t["due"] - datetime.timedelta(days=days)

            if remind_time <= now <= remind_time + datetime.timedelta(seconds=30):
                channel = bot.get_channel(t["channel_id"])
                if channel:
                    await channel.send(f"⏰ {label_to_text(label)}リマインド: {t['task']}")

                notified.append(label)

                db, cursor = get_cursor()
                cursor.execute(
                    "UPDATE tasks SET notified=%s WHERE id=%s",
                    (json.dumps(notified), t["id"])
                )
                db.commit()
                db.close()

# -----------------------
# 起動
# -----------------------
@bot.event
async def on_ready():
    print("🚀 起動完了")

    await asyncio.to_thread(load_tasks)

    await tree.sync()
    print("✅ コマンド同期完了")

    if not reminder_loop.is_running():
        reminder_loop.start()
    print("🔔 リマインド開始")

# -----------------------
# 実行
# -----------------------
if __name__ == "__main__":
    bot.run(os.environ.get("TOKEN"))