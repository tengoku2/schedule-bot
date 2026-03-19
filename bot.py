import os
import discord
from discord.ext import tasks
from discord import app_commands
import datetime
import json
import asyncio
import mysql.connector

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

    print("📦 rows取得:", len(rows))

    tasks_list = []
    for t in rows:
        tasks_list.append({
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

def can_edit(task, user):
    return user.guild_permissions.administrator or task["owner_id"] == user.id

# -----------------------
# リマインド
# -----------------------
def parse_reminders(reminder_str):
    mapping = {
        "1週間": 7, "3日前": 3, "24時間": 1,
        "3時間": 0.125, "10秒前": 10/86400
    }
    return [mapping[r.strip()] for r in reminder_str.split(",") if r.strip() in mapping]

def reminder_label(days):
    if days >= 7: return "1週間前"
    elif days >= 3: return "3日前"
    elif days >= 1: return "24時間前"
    elif days >= 1/8: return "3時間前"
    else: return f"{int(days*86400)}秒前"

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

# -----------------------
# /add
# -----------------------
@tree.command(name="add", description="タスク追加")
async def add(interaction: discord.Interaction, task_name: str):
    await interaction.response.defer()

    now = datetime.datetime.now(JST)
    due = now + datetime.timedelta(days=1)

    try:
        db, cursor = get_cursor()

        cursor.execute("""
        INSERT INTO tasks 
        (task, due, channel_id, owner_id, visible_to, roles, reminders, notified, mention, everyone, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            task_name,
            due,
            interaction.channel.id,
            interaction.user.id,
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

    except Exception as e:
        print(e)
        await interaction.followup.send("❌ DBエラー")
        return

    await asyncio.to_thread(load_tasks)

    await interaction.followup.send(f"✅ 追加: {task_name}")

# -----------------------
# 起動
# -----------------------
@bot.event
async def on_ready():
    print("🚀 起動完了")

    await asyncio.to_thread(load_tasks)
    await tree.sync()

    check_tasks.start()

# -----------------------
# リマインド
# -----------------------
@tasks.loop(seconds=30)
async def check_tasks():
    now = datetime.datetime.now(JST)

    for task in tasks_list:
        if task["status"] == "done":
            continue

        for r in task["reminders"]:
            if r in task["notified"]:
                continue

            if task["due"] - datetime.timedelta(days=r) <= now:
                channel = bot.get_channel(task["channel_id"])
                if channel:
                    await channel.send(f"⏰ {task['task']}")

                task["notified"].append(r)

# -----------------------
# 実行
# -----------------------
if __name__ == "__main__":
    bot.run(os.environ.get("TOKEN"))