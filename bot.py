import discord
from discord.ext import commands, tasks
import datetime
import json
import os

TOKEN = "MTQ4MzQyNzA3MzI5MTg0OTc1OA.GytC9k.rLd8w06ClGgIELofLWXOyRLQN0M9woqAsaYo-I"

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

TASK_FILE = "tasks.json"

# -----------------------
# データ読み書き
# -----------------------
def load_tasks():
    if not os.path.exists(TASK_FILE):
        return []
    with open(TASK_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_tasks(tasks):
    with open(TASK_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)

# -----------------------
# タスク追加
# -----------------------
@bot.command()
async def add(ctx, date: str, *, task_name):
    tasks = load_tasks()

    task = {
        "task": task_name,
        "date": date,
        "notified": False,
        "channel_id": ctx.channel.id
    }

    tasks.append(task)
    save_tasks(tasks)

    await ctx.send(f"✅ タスク登録: {task_name}（期限: {date}）")

# -----------------------
# タスク一覧
# -----------------------
@bot.command()
async def list(ctx):
    tasks = load_tasks()

    if not tasks:
        await ctx.send("タスクなし")
        return

    msg = ""
    for i, t in enumerate(tasks):
        msg += f"{i+1}. {t['task']}（{t['date']}）\n"

    await ctx.send(msg)

# -----------------------
# リマインド処理
# -----------------------
@tasks.loop(minutes=1)
async def check_tasks():
    tasks = load_tasks()
    now = datetime.datetime.now()

    for task in tasks:
        if task["notified"]:
            continue

        due = datetime.datetime.strptime(task["date"], "%Y-%m-%d")

        # 当日になったら通知
        if now.date() >= due.date():
            channel = bot.get_channel(task["channel_id"])
            if channel:
                await channel.send(f"⏰ 期限です！\n📌 {task['task']}")

            task["notified"] = True

    save_tasks(tasks)

# -----------------------
# 起動時
# -----------------------
@bot.event
async def on_ready():
    print("Bot起動")
    check_tasks.start()

bot.run(TOKEN)