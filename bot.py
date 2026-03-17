import os
import discord
from discord.ext import commands, tasks
from flask import Flask
import threading
import datetime

# -----------------------
# Flask: Koyeb用ヘルスチェック
# -----------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 8000))  # KoyebのPORT環境変数に対応
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# -----------------------
# Discord Bot設定
# -----------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

tasks_list = []  # 永続化なし

# -----------------------
# タスク追加
# -----------------------
@bot.command()
async def add(ctx, date: str, time: str, *, task_name):
    """
    例: !add 2026-03-18 21:30 タスク名
    """
    try:
        due = datetime.datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        await ctx.send("❌ 日付・時間は YYYY-MM-DD HH:MM 形式で入力してください")
        return

    task = {
        "task": task_name,
        "due": due,
        "channel_id": ctx.channel.id,
        "notified": False
    }
    tasks_list.append(task)
    await ctx.send(f"✅ タスク登録: {task_name}（期限: {due}）")

# -----------------------
# タスク一覧
# -----------------------
@bot.command(name="tasks")
async def show_list(ctx):
    if not tasks_list:
        await ctx.send("タスクなし")
        return

    msg = ""
    for i, t in enumerate(tasks_list):
        status = "✅通知済" if t["notified"] else "⌛未通知"
        msg += f"{i+1}. {t['task']}（期限: {t['due'].strftime('%Y-%m-%d %H:%M')}） {status}\n"

    await ctx.send(msg)

# -----------------------
# タスク削除
# -----------------------
@bot.command()
async def delete(ctx, index: int):
    if 0 < index <= len(tasks_list):
        removed = tasks_list.pop(index-1)
        await ctx.send(f"❌ タスク削除: {removed['task']}")
    else:
        await ctx.send("❌ 無効な番号です")

# -----------------------
# リマインダー処理
# -----------------------
@tasks.loop(seconds=30)
async def check_tasks():
    now = datetime.datetime.now()
    for task in tasks_list:
        if task["notified"]:
            continue
        if now >= task["due"]:
            channel = bot.get_channel(task["channel_id"])
            if channel:
                await channel.send(f"⏰ 期限です！\n📌 {task['task']}")
            task["notified"] = True

# -----------------------
# 起動時
# -----------------------
@bot.event
async def on_ready():
    print(f"{bot.user} が起動しました！")
    check_tasks.start()

# -----------------------
# Bot起動
# -----------------------
bot.run(os.environ.get("TOKEN"))  # Koyebの環境変数TOKENを使用