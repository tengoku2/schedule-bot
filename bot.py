import os
import discord
from discord.ext import tasks
from discord import app_commands
from flask import Flask
import threading
import datetime
import json

DATA_FILE = "tasks.json"

# -----------------------
# JSON保存関数
# -----------------------
def save_tasks():
    data = []
    for t in tasks_list:
        data.append({
            "task": t["task"],
            "due": t["due"].isoformat(),
            "channel_id": t["channel_id"],
            "owner_id": t["owner_id"],
            "visible_to": t["visible_to"],
            "reminders": t["reminders"],
            "notified": t["notified"]
        })
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# -----------------------
# JSON読み込み
# -----------------------
def load_tasks():
    global tasks_list
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        tasks_list = []
        for t in data:
            tasks_list.append({
                "task": t["task"],
                "due": datetime.datetime.fromisoformat(t["due"]),
                "channel_id": t["channel_id"],
                "owner_id": t["owner_id"],
                "visible_to": t["visible_to"],
                "reminders": t["reminders"],
                "notified": t["notified"]
            })
    except FileNotFoundError:
        tasks_list = []

# -----------------------
# Flaskヘルスチェック
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

# JSTタイムゾーン
JST = datetime.timezone(datetime.timedelta(hours=9))

# -----------------------
# ユーザー権限チェック
# -----------------------
def can_view(task, user):
    if user.guild_permissions.administrator:
        return True
    return user.id in task["visible_to"]

def can_edit(task, user):
    if user.guild_permissions.administrator:
        return True
    return task["owner_id"] == user.id

# -----------------------
# リマインド変換
# -----------------------
def parse_reminders(reminder_str: str):
    mapping = {
        "1か月": 30, "2週間": 14, "1週間": 7, "3日前": 3, "24時間": 1,
        "3時間": 0.125, "10秒前": 10/86400, "5秒前": 5/86400, "1秒前": 1/86400
    }
    reminders_list = []
    for r in reminder_str.split(","):
        r = r.strip()
        if r in mapping:
            reminders_list.append(mapping[r])
    return reminders_list

def reminder_label(days: float) -> str:
    if days >= 30: return "1か月前"
    elif days >= 14: return "2週間前"
    elif days >= 7: return "1週間前"
    elif days >= 3: return "3日前"
    elif days >= 1: return "24時間前"
    elif days >= 1/8: return "3時間前"
    elif days == 0: return "当日"
    else: return f"{int(days*86400)}秒前"

# -----------------------
# タスク一覧
# -----------------------
@tree.command(name="list", description="タスク一覧を表示", guild=GUILD_OBJ)
async def list_tasks(interaction: discord.Interaction):
    user = interaction.user
    visible_tasks = [t for t in tasks_list if can_view(t, user)]

    if not visible_tasks:
        await interaction.response.send_message("📭 タスクはありません")
        return

    msg = "📋 タスク一覧\n"
    for i, task in enumerate(visible_tasks, start=1):
        msg += (
            f"{i}. {task['task']}（作成者: <@{task['owner_id']}>）\n"
            f"📅 {task['due'].strftime('%m/%d %H:%M')}\n"
            f"🔔 {', '.join([reminder_label(r) for r in task['reminders']])}\n"
            f"👀 見れる人: {', '.join([f'<@{uid}>' for uid in task['visible_to']])}\n\n"
        )
    await interaction.response.send_message(msg)

# -----------------------
# タスク追加（チーム対応）
# -----------------------
@tree.command(name="add", description="タスクを追加します", guild=GUILD_OBJ)
@app_commands.describe(
    date="MMDDまたはYYYYMMDD",
    time="HHMM",
    task_name="タスク内容",
    reminders="リマインド例:1か月,2週間",
    visible="閲覧可能ユーザーIDカンマ区切り"
)
async def add(interaction: discord.Interaction, date: str, time: str, task_name: str,
              reminders: str = "", visible: str = ""):
    now = datetime.datetime.now(JST)
    try:
        if len(date) == 4:
            year = now.year
            due = datetime.datetime.strptime(f"{year}{date} {time}", "%Y%m%d %H%M").replace(tzinfo=JST)
            if due < now: due = due.replace(year=year+1)
        elif len(date) == 8:
            due = datetime.datetime.strptime(f"{date} {time}", "%Y%m%d %H%M").replace(tzinfo=JST)
        else:
            raise ValueError
    except ValueError:
        await interaction.response.send_message("❌ 日付形式が不正", ephemeral=True)
        return

    # リマインド
    if reminders:
        reminders_list = parse_reminders(reminders)
    else:
        reminders_list = [30, 14, 7, 3, 1, 0.125]

    filtered_reminders = []
    for r in reminders_list:
        if due - datetime.timedelta(days=r) > now:
            filtered_reminders.append(r)
    if due > now and 0 not in filtered_reminders:
        filtered_reminders.append(0)
    if not filtered_reminders:
        filtered_reminders = [0]
    filtered_reminders = sorted(filtered_reminders, reverse=True)

    # visible_toリスト
    visible_ids = [interaction.user.id]
    if visible:
        try:
            for s in visible.split(","):
                uid = int(s.strip())
                if uid not in visible_ids:
                    visible_ids.append(uid)
        except:
            await interaction.response.send_message("❌ visibleに不正なIDがあります", ephemeral=True)
            return

    task = {
        "task": task_name,
        "due": due,
        "channel_id": interaction.channel.id,
        "owner_id": interaction.user.id,
        "visible_to": visible_ids,
        "reminders": filtered_reminders,
        "notified": []
    }
    tasks_list.append(task)
    save_tasks()

    await interaction.response.send_message(
        f"✅ タスク登録: {task_name}（期限: {due.strftime('%Y-%m-%d %H:%M')}）\n"
        f"リマインド: {', '.join([reminder_label(r) for r in filtered_reminders])}\n"
        f"見れる人: {', '.join([f'<@{uid}>' for uid in visible_ids])}"
    )

# -----------------------
# タスク削除
# -----------------------
@tree.command(name="delete", description="タスク削除", guild=GUILD_OBJ)
@app_commands.describe(index="削除するタスク番号")
async def delete(interaction: discord.Interaction, index: int):
    user = interaction.user
    visible_tasks = [t for t in tasks_list if can_view(t, user)]
    if not (0 < index <= len(visible_tasks)):
        await interaction.response.send_message("❌ 無効な番号", ephemeral=True)
        return
    task = visible_tasks[index - 1]
    if not can_edit(task, user):
        await interaction.response.send_message("❌ 権限がありません", ephemeral=True)
        return
    tasks_list.remove(task)
    save_tasks()
    await interaction.response.send_message(f"🗑️ 削除しました\n📌 {task['task']}")

# -----------------------
# リマインダー処理
# -----------------------
@tasks.loop(seconds=30)
async def check_tasks():
    now = datetime.datetime.now(JST)
    to_remove = []
    for task in tasks_list:
        remaining = sorted([r for r in task["reminders"] if r not in task["notified"]], reverse=True)
        if not remaining:
            if now >= task["due"] + datetime.timedelta(days=30):
                to_remove.append(task)
            continue
        next_reminder = remaining[0]
        reminder_time = task["due"] - datetime.timedelta(days=next_reminder)
        if reminder_time <= now < reminder_time + datetime.timedelta(seconds=10):
            channel = bot.get_channel(task["channel_id"])
            if channel:
                await channel.send(f"⏰ {task['task']}\n🕒 {reminder_label(next_reminder)} / 期限: {task['due'].strftime('%m/%d %H:%M')}")
            task["notified"].append(next_reminder)
            save_tasks()
    for task in to_remove:
        tasks_list.remove(task)
        print(f"🗑️ タスク削除（期限+1か月）: {task['task']}")
    if to_remove:
        save_tasks()

# -----------------------
# 起動時
# -----------------------
@bot.event
async def on_ready():
    if GUILD_OBJ:
        await tree.sync(guild=GUILD_OBJ)
        print(f"サーバー専用コマンドを {GUILD_ID} に同期しました")
    else:
        await tree.sync()
        print("グローバルコマンドを同期しました")
    print(f"{bot.user} が起動しました！")
    load_tasks()
    check_tasks.start()

bot.run(os.environ.get("TOKEN"))