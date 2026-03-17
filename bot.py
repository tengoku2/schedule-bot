import os
import discord
from discord.ext import tasks
from discord import app_commands
from flask import Flask
import threading
import datetime

# -----------------------
# Flask: Ping回避用ヘルスチェック
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

# -----------------------
# リマインド文字列→日数変換関数
# -----------------------
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

def reminder_label(days: float) -> str:
    if days >= 30:
        return "1か月前"
    elif days >= 14:
        return "2週間前"
    elif days >= 7:
        return "1週間前"
    elif days >= 3:
        return "3日前"
    elif days >= 1:
        return "24時間前"
    else:
        return "3時間前"

# -----------------------
# タスク追加
# -----------------------
@tree.command(name="add", description="タスクを追加します")
@app_commands.describe(
    date="MMDDまたはYYYYMMDD形式",
    time="HHMM形式",
    task_name="タスク内容",
    reminders="リマインド日数のカンマ区切り (例:1か月,2週間,1週間)"
)
async def add(
    interaction: discord.Interaction,
    date: str,
    time: str,
    task_name: str,
    reminders: str = ""
):
    now = datetime.datetime.now(JST)

    # 西暦省略対応
    try:
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
            raise ValueError
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
                "❌ remindersが不正です。例: 1か月,2週間,1週間,3日前,24時間,3時間",
                ephemeral=True
            )
            return
    else:
        reminders_list = [30, 14, 7, 3, 1, 0.125]

    task = {
        "task": task_name,
        "due": due,
        "channel_id": interaction.channel.id,
        "reminders": reminders_list,
        "notified": []
    }
    tasks_list.append(task)
    await interaction.response.send_message(
        f"✅ タスク登録: {task_name}（期限: {due.strftime('%Y-%m-%d %H:%M')}）\nリマインド: {', '.join([reminder_label(r) for r in reminders_list])}"
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
        notified_labels = [reminder_label(r) for r in t["notified"]]
        status = f"✅通知済({', '.join(notified_labels)})" if t["notified"] else "⌛未通知"
        all_reminders_labels = [reminder_label(r) for r in t["reminders"]]
        msg += (
            f"{i+1}. {t['task']}（期限: {t['due'].strftime('%Y-%m-%d %H:%M')}）\n"
            f"　リマインド: {', '.join(all_reminders_labels)}\n"
            f"　状態: {status}\n"
        )
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
# リマインダー処理（当日だけ通知＋期限後1か月で削除）
# -----------------------
@tasks.loop(seconds=30)
async def check_tasks():
    now = datetime.datetime.now(JST)
    to_remove = []

    for task in tasks_list:
        # リマインド通知
        for r in task["reminders"]:
            if r in task["notified"]:
                continue

            reminder_time = task["due"] - datetime.timedelta(days=r)
            # 当日になったら通知（過去分はスキップ）
            if now >= reminder_time and now < reminder_time + datetime.timedelta(seconds=30):
                channel = bot.get_channel(task["channel_id"])
                if channel:
                    await channel.send(f"⏰ {reminder_label(r)}のリマインド\n📌 {task['task']}")
                task["notified"].append(r)

        # 期限＋1か月経過でタスク削除
        if set(task["reminders"]) == set(task["notified"]):
            if now >= task["due"] + datetime.timedelta(days=30):
                to_remove.append(task)

    for task in to_remove:
        tasks_list.remove(task)
        print(f"🗑️ タスク削除（期限+1か月経過）: {task['task']}")

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