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
            "due": t["due"].astimezone(JST),
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
# /add（完全安定版）
# -----------------------
@tree.command(name="add", description="タスク追加")
async def add(interaction: discord.Interaction, task_name: str):

    print("🔥 /add 呼ばれた")

    # 👇 即レス（これが命）
    await interaction.response.send_message("⏳ 追加中...", ephemeral=True)

    now = datetime.datetime.now(JST)
    due = now + datetime.timedelta(days=1)

    try:
        await asyncio.to_thread(
            insert_task,
            task_name,
            due,
            interaction.channel.id,
            interaction.user.id
        )
        print("✅ DB OK")

    except Exception as e:
        print("❌ DBエラー:", e)
        await interaction.edit_original_response(content="❌ DBエラー")
        return

    await interaction.edit_original_response(content=f"✅ 追加: {task_name}")

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

        if t["due"] < now:
            continue

        notified = t.get("notified", [])

        for label, days in REMINDERS:
            if label in notified:
                continue

            remind_time = t["due"] - datetime.timedelta(days=days)

            if now >= remind_time:
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

    reminder_loop.start()
    print("🔔 リマインド開始")

# -----------------------
# 実行
# -----------------------
if __name__ == "__main__":
    bot.run(os.environ.get("TOKEN"))