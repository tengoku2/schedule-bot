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

# -----------------------
# リマインド文字列→日数変換
# -----------------------
def parse_reminders(reminder_str: str):
    mapping = {
        "1か月": 30,
        "2週間": 14,
        "1週間": 7,
        "3日前": 3,
        "24時間": 1,
        "3時間": 0.125,
        # テスト用秒単位
        "10秒前": 10/86400,
        "5秒前": 5/86400,
        "1秒前": 1/86400
    }
    reminders_list = []
    for r in reminder_str.split(","):
        r = r.strip()
        if r in mapping:
            reminders_list.append(mapping[r])
    return reminders_list

# 日数→ラベル変換
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
    elif days >= 1/8:
        return "3時間前"
    elif days == 0:
        return "当日"
    else:
        return f"{int(days*86400)}秒前"

# -----------------------
# タスク追加
# -----------------------
@tree.command(name="add", description="タスクを追加します")
@app_commands.describe(
    date="MMDDまたはYYYYMMDD形式",
    time="HHMM形式",
    task_name="タスク内容",
    reminders="リマインドのカンマ区切り (例:1か月,2週間,1週間,10秒前,5秒前)"
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
    # リマインド設定
    if reminders:
        reminders_list = parse_reminders(reminders)
        if not reminders_list:
            await interaction.response.send_message(
                "❌ remindersが不正です",
                ephemeral=True
            )
            return
    else:
        reminders_list = [30, 14, 7, 3, 1, 0.125]  # デフォルト

    # 👇 ここ追加（超重要）
    now = datetime.datetime.now(JST)
    filtered_reminders = []

    for r in reminders_list:
        reminder_time = due - datetime.timedelta(days=r)
        if reminder_time > now:
            filtered_reminders.append(r)

    # もし全部過去なら「当日だけ残す」
    if not filtered_reminders:
        filtered_reminders = [0]

    # ソート（遠い順）
    filtered_reminders = sorted(filtered_reminders, reverse=True)



    task = {
        "task": task_name,
        "due": due,
        "channel_id": interaction.channel.id,
        "reminders": filtered_reminders, 
        "notified": []
    }
    tasks_list.append(task)
    await interaction.response.send_message(
        f"✅ タスク登録: {task_name}（期限: {due.strftime('%Y-%m-%d %H:%M')}）\nリマインド: {', '.join([reminder_label(r) for r in reminders_list])}"
    )

# -----------------------
# リマインダー処理（順番守る）
# -----------------------
@tasks.loop(seconds=5)  # 秒単位テスト用に短く
async def check_tasks():
    now = datetime.datetime.now(JST)
    to_remove = []

    for task in tasks_list:
        # 直近のリマインド以外は飛ばす
        remaining = [r for r in task["reminders"] if r not in task["notified"]]
        if not remaining:
            # 期限＋1か月経過でタスク削除
            if now >= task["due"] + datetime.timedelta(days=30):
                to_remove.append(task)
            continue

        next_reminder = remaining[0]
        reminder_time = task["due"] - datetime.timedelta(days=next_reminder)

        if now >= reminder_time:
            channel = bot.get_channel(task["channel_id"])
            if channel:
                await channel.send(
                    f"⏰ {task['task']}\n"
                    f"🕒 {reminder_label(next_reminder)} / 期限: {task['due'].strftime('%m/%d %H:%M')}"
                )
            task["notified"].append(next_reminder)

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