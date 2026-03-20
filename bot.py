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
# JST
# -----------------------
JST = datetime.timezone(datetime.timedelta(hours=9))

# 日付パース
def parse_date(date_str):
    now = datetime.datetime.now(JST)

    # YYYY-MM-DD
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except:
        pass

    # 数字（3桁 or 4桁）
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

    # M/D
    if "/" in date_str:
        m, d = map(int, date_str.split("/"))
        year = now.year
        d = datetime.date(year, m, d)

        if d < now.date():
            d = datetime.date(year + 1, m, d)

        return d

    raise ValueError("日付形式エラー")

# 時間パース
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
        due = t["due"].replace(tzinfo=JST)

        new_list.append({
            "id": t["id"],
            "task": t["task"],
            "due": due,
            "channel_id": t["channel_id"],
            "owner_id": t["owner_id"],
            "visible_to": json.loads(t["visible_to"] or "[]"),
            "status": t["status"],
            "notified": json.loads(t["notified"] or "[]"),
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
def insert_task(task_name, due, channel_id, user_id):
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
        json.dumps([0]),
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
    time_str: str = None
):

    await interaction.response.send_message("⏳ 追加中...", ephemeral=True)

    now = datetime.datetime.now(JST)

    try:
        # -------------------
        # 両方なし
        # -------------------
        if not date_str and not time_str:
            due = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
            )
            due = due.replace(tzinfo=JST)

        # -------------------
        # dateなし
        # -------------------
        elif not date_str:
            t = parse_time(time_str)

            today_due = datetime.datetime.combine(now.date(), t).replace(tzinfo=JST)

            if today_due > now:
                due = today_due
            else:
                due = today_due + datetime.timedelta(days=1)

        # -------------------
        # timeなし
        # -------------------
        elif not time_str:
            d = parse_date(date_str)

            if d == now.date():
                due = datetime.datetime.combine(d, datetime.time(23, 59)).replace(tzinfo=JST)
            else:
                due = datetime.datetime.combine(d, datetime.time(0, 0)).replace(tzinfo=JST)

        # -------------------
        # 両方あり
        # -------------------
        else:
            d = parse_date(date_str)
            t = parse_time(time_str)
            due = datetime.datetime.combine(d, t).replace(tzinfo=JST)

    except Exception:
        await interaction.edit_original_response(
            content="❌ 日時形式エラー\n例: 320 21 / 3/20 930"
        )
        return

    try:
        await asyncio.to_thread(
            insert_task,
            task_name,
            due,
            interaction.channel.id,
            interaction.user.id
        )

    except Exception as e:
        print(e)
        await interaction.edit_original_response(content="❌ DBエラー")
        return

    jst_due = due.replace(tzinfo=datetime.timezone.utc).astimezone(JST)

    await interaction.edit_original_response(
        content=f"✅ 追加: {task_name}\n📅 {jst_due.strftime('%m/%d %H:%M')}"
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
# リマインド機能
# -----------------------
from discord.ext import tasks

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
    now = datetime.datetime.now(JST)

    for t in tasks_list:
        if t["status"] != "todo":
            continue

        due = t["due"]

        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)

        if due < now:
            continue

        notified = t.get("notified", [])

        for label, days in REMINDERS:
            if label in notified:
                continue

            remind_time = due - datetime.timedelta(days=days)

            if remind_time <= now <= remind_time + datetime.timedelta(seconds=30):
                channel = bot.get_channel(t["channel_id"])
                if channel:
                    await channel.send(
                        f"⏰ {label_to_text(label)}リマインド: {t['task']}"
                    )

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

    try:
        await asyncio.to_thread(load_tasks)
    except Exception as e:
        print("❌ load_tasks失敗:", e)

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