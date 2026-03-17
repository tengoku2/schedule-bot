import os
import discord
from discord.ext import tasks
from discord import app_commands
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
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# -----------------------
# Discord Bot設定
# -----------------------
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

tasks_list = []

# GUILD_IDが設定されていればサーバー専用
GUILD_ID = os.environ.get("GUILD_ID")
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

# JST タイムゾーン設定
JST = datetime.timezone(datetime.timedelta(hours=9))

# リマインド文字列→日数変換関数
def parse_reminders(reminder_str: str):
    mapping = {
        "1か月": 30,
        "2週間": 14,
        "1週間": 7,
        "3日前": 3,
        "24時間": 1,
        "3時間": 0.125
    }
    reminders_list = []
    for r in reminder_str.split(","):
        r = r.strip()
        if r in mapping:
            reminders_list.append(mapping[r])
    return reminders_list

# -----------------------
# タスク追加
# -----------------------
@tree.command(name="add", description="タスクを追加します")
@app_commands.describe(
    date="MMDDまたはYYYYMMDD形式",
    time="HHMM形式",
    task_name="タスク内容",
    reminders="リマインド日数のカンマ区切り (例:30,14,7)"
)
async def add(
    interaction: discord.Interaction,
    date: str,
    time: str,
    task_name: str,
    reminders: str = ""
):
    try:
        now = datetime.datetime.now(JST)
        # 西暦省略対応
        if len(date) == 4:  # MMDD
            year = now.year
            due_naive = datetime.datetime.strptime(f"{year}{date} {time}", "%Y%m%d %H%M")
            due = due_naive.replace(tzinfo=JST)
            if due < now:
                due = due.replace(year=year+1)
        elif len(date) == 8:  # YYYYMMDD
            due_naive = datetime.datetime.strptime(f"{date} {time}", "%Y%m%d %H%M")
            due = due_naive.replace(tzinfo=JST)
        else:
            raise ValueError("日付形式が不正")
    except ValueError:
        await interaction.response.send_message(
            "❌ 日付・時間は MMDD HHMM または YYYYMMDD HHMM 形式で入力してください",
            ephemeral=True
        )
        return

    # リマインド設定
    if reminders:
        reminders_list = parse_reminders(reminders)
        if not reminders_list:
            await interaction.response.send_message(
                "❌ remindersが不正です。例: 1か月,2週間,1週間,24時間,3時間",
                ephemeral=True
            )
            return
    else:
        reminders_list = [30, 14, 7, 3, 1, 0.125]  # デフォルト

    task = {
        "task": task_name,
        "due": due,
        "channel_id": interaction.channel.id,
        "reminders": reminders_list,
        "notified": []
    }
    tasks_list.append(task)
    await interaction.response.send_message(
        f"✅ タスク登録: {task_name}（期限: {due.strftime('%Y-%m-%d %H:%M')}）\nリマインド: {reminders_list} 日前"
    )

# -----------------------
# タスク一覧
# -----------------------
@tree.command(name="tasks", description="タスク一覧を表示します")
async def show_list(interaction: discord.Interaction):
    if not tasks_list:
        await interaction.response.send_message("タスクなし", ephemeral=True)
        return

    msg = ""
    for i, t in enumerate(tasks_list):
        notified_str = ", ".join([str(r) for r in t["notified"]])
        status = f"✅通知済({notified_str})" if t["notified"] else "⌛未通知"
        msg += f"{i+1}. {t['task']}（期限: {t['due'].strftime('%Y-%m-%d %H:%M')}） {status}\n"

    await interaction.response.send_message(msg)

# -----------------------
# タスク削除
# -----------------------
@tree.command(name="delete", description="タスクを削除します")
@app_commands.describe(index="削除するタスク番号")
async def delete(interaction: discord.Interaction, index: int):
    if 0 < index <= len(tasks_list):
        removed = tasks_list.pop(index-1)
        await interaction.response.send_message(f"❌ タスク削除: {removed['task']}")
    else:
        await interaction.response.send_message("❌ 無効な番号です", ephemeral=True)

# -----------------------
# リマインダー処理
# -----------------------
@tasks.loop(seconds=30)
async def check_tasks():
    now = datetime.datetime.now(JST)
    for task in tasks_list:
        for r in task["reminders"]:
            if r in task["notified"]:
                continue
            reminder_time = task["due"] - datetime.timedelta(days=r)
            if now >= reminder_time:
                channel = bot.get_channel(task["channel_id"])
                if channel:
                    await channel.send(f"⏰ {int(r*24)}時間前のリマインド\n📌 {task['task']}")
                task["notified"].append(r)

# -----------------------
# 起動時
# -----------------------
@bot.event
async def on_ready():
    if GUILD_OBJ:
        await tree.sync(guild=GUILD_OBJ)
        print(f"サーバー専用スラッシュコマンドを {GUILD_ID} に同期しました")
    else:
        await tree.sync()
        print("グローバルスラッシュコマンドを同期しました")
    print(f"{bot.user} が起動しました！")
    check_tasks.start()

# -----------------------
# Bot起動
# -----------------------
bot.run(os.environ.get("TOKEN"))