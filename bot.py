import os
import discord
from discord import app_commands
from discord.ext import tasks
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
        port=int(os.environ.get("DB_PORT", 15042)),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASS"),
        database=os.environ.get("DB_NAME"),
        ssl_disabled=False
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
            "reminders": json.loads(t["reminders"] or "[]"),
            "notified": json.loads(t["notified"] or "[]"),
            "mention": t["mention"],
            "roles": json.loads(t["roles"] or "[]"),
            "status": t["status"],
            "completed_by": t["completed_by"],
            "completed_at": t["completed_at"],
            "everyone": t["everyone"],
        })

    tasks_list = new_list
    db.close()

# -----------------------
# Discord設定
# -----------------------
intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------
# 権限
# -----------------------
def can_view(task, user):
    if user.guild_permissions.administrator:
        return True
    if not task["visible_to"]:
        return True
    if user.id in task["visible_to"]:
        return True
    user_roles = [r.id for r in user.roles]
    return any(r in user_roles for r in task.get("roles", []))

# -----------------------
# /list
# -----------------------
@tree.command(name="list", description="タスク一覧")
async def list_tasks(interaction: discord.Interaction):
    await interaction.response.defer()

    visible = [t for t in tasks_list if can_view(t, interaction.user) and t["status"] != "done"]

    if not visible:
        await interaction.followup.send("📭 タスクなし")
        return

    msg = "📋 タスク一覧\n"
    for i, t in enumerate(visible, 1):
        msg += f"{i}. {t['task']}（<@{t['owner_id']}>）\n"
        msg += f"📅 {t['due'].strftime('%m/%d %H:%M')}\n\n"

    await interaction.followup.send(msg)


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

    await interaction.response.send_message("⏳ 追加中...", ephemeral=True)

    now = datetime.datetime.now(JST)
    due = now + datetime.timedelta(days=1)

    try:
        # 👇 ここが最重要！！！！！
        await asyncio.to_thread(
            insert_task,
            task_name,
            due,
            interaction.channel_id,
            interaction.user.id
        )
        print("ARGS確認", task_name, due, interaction.channel.id, interaction.user.id)

    except Exception as e:
        print("❌ DBエラー:", e)
        await interaction.edit_original_response(content="❌ DBエラー")
        return

    await interaction.edit_original_response(content=f"✅ 追加: {task_name}")

    asyncio.create_task(asyncio.to_thread(load_tasks))

# -----------------------
# 起動
# -----------------------
@bot.event
async def on_ready():
    print("🚀 起動完了")

    await asyncio.to_thread(load_tasks)
    await tree.sync()

# -----------------------
# 実行
# -----------------------
if __name__ == "__main__":
    bot.run(os.environ.get("TOKEN"))