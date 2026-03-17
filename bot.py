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
            "reminders": t["reminders"],
            "notified": t["notified"]
        })
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# 読み込み関数
def load_tasks():
    global tasks_list
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)

        tasks_list = []
        for t in data:
            tasks_list.append({
                "task": t["task"],
                "due": datetime.datetime.fromisoformat(t["due"]).astimezone(JST),
                "channel_id": t["channel_id"],
                "reminders": t["reminders"],
                "notified": t["notified"]
            })
    except FileNotFoundError:
        tasks_list = []

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
# タスク一覧
# -----------------------

@tree.command(name="list", description="タスク一覧を表示", guild=GUILD_OBJ)
async def list_tasks(interaction: discord.Interaction):
    if not tasks_list:
        await interaction.response.send_message("📭 タスクはありません")
        return

    msg = "📋 タスク一覧\n"
    for i, task in enumerate(tasks_list, start=1):
        msg += (
            f"{i}. {task['task']}\n"
            f"📅 {task['due'].strftime('%m/%d %H:%M')}\n"
            f"🔔 {', '.join([reminder_label(r) for r in task['reminders']])}\n\n"
        )

    await interaction.response.send_message(msg)

# -----------------------
# タスク編集
# -----------------------
    
@tree.command(name="edit", description="タスクを編集します", guild=GUILD_OBJ)
@app_commands.describe(
    index="編集するタスク番号",
    date="MMDDまたはYYYYMMDD（省略可）",
    time="HHMM（省略可）",
    task_name="新しいタスク名（省略可）",
    reminders="例:1か月,2週間,1週間（省略可）"
)
async def edit(
    interaction: discord.Interaction,
    index: int,
    date: str = None,
    time: str = None,
    task_name: str = None,
    reminders: str = None
):
    if not (0 < index <= len(tasks_list)):
        await interaction.response.send_message("❌ 無効な番号です", ephemeral=True)
        return

    task = tasks_list[index - 1]
    now = datetime.datetime.now(JST)

    # -----------------------
    # タスク名変更
    # -----------------------
    if task_name:
        task["task"] = task_name

    # -----------------------
    # 日付・時間変更
    # -----------------------
    if date or time:
        try:
            current_due = task["due"]

            new_date = date if date else current_due.strftime("%Y%m%d")
            new_time = time if time else current_due.strftime("%H%M")

            if len(new_date) == 4:
                year = now.year
                due_naive = datetime.datetime.strptime(f"{year}{new_date} {new_time}", "%Y%m%d %H%M")
                due = due_naive.replace(tzinfo=JST)
                if due < now:
                    due = due.replace(year=year+1)
            elif len(new_date) == 8:
                due_naive = datetime.datetime.strptime(f"{new_date} {new_time}", "%Y%m%d %H%M")
                due = due_naive.replace(tzinfo=JST)
            else:
                raise ValueError

            task["due"] = due

            # 🔥 ここ重要：通知リセット
            task["notified"] = []

        except ValueError:
            await interaction.response.send_message("❌ 日付形式が不正です", ephemeral=True)
            return

    # -----------------------
    # リマインド変更
    # -----------------------
    if reminders:
        new_reminders = parse_reminders(reminders)
        if not new_reminders:
            await interaction.response.send_message("❌ remindersが不正です", ephemeral=True)
            return

        # 過去リマインド除外
        filtered = []
        for r in new_reminders:
            reminder_time = task["due"] - datetime.timedelta(days=r)
            if reminder_time > now:
                filtered.append(r)

        # 当日追加
        if task["due"] > now and 0 not in filtered:
            filtered.append(0)

        if not filtered:
            filtered = [0]

        task["reminders"] = sorted(filtered, reverse=True)
        task["notified"] = []

    # -----------------------
    # 完了メッセージ
    # -----------------------
    await interaction.response.send_message(
        f"✏️ 編集完了\n"
        f"📌 {task['task']}\n"
        f"📅 {task['due'].strftime('%Y-%m-%d %H:%M')}\n"
        f"🔔 {', '.join([reminder_label(r) for r in task['reminders']])}"
    )

    # 保存
    save_tasks()

# -----------------------
# タスク追加
# -----------------------
@tree.command(name="add", description="タスクを追加します", guild=GUILD_OBJ)
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
    # フィルタ
    filtered_reminders = []

    for r in reminders_list:
        reminder_time = due - datetime.timedelta(days=r)
        if reminder_time > now:
            filtered_reminders.append(r)

    # 当日追加
    if due > now and 0 not in filtered_reminders:
        filtered_reminders.append(0)

    # 全部過去なら当日のみ
    if not filtered_reminders:
        filtered_reminders = [0]

    # ソート
    filtered_reminders = sorted(filtered_reminders, reverse=True)



    task = {
        "task": task_name,
        "due": due,
        "channel_id": interaction.channel.id,
        "reminders": filtered_reminders, 
        "notified": []
    }
    tasks_list.append(task)
    save_tasks()

    await interaction.response.send_message(
        f"✅ タスク登録: {task_name}（期限: {due.strftime('%Y-%m-%d %H:%M')}）\n"
        f"リマインド: {', '.join([reminder_label(r) for r in filtered_reminders])}"
    )

# -----------------------
# リマインダー処理（順番守る）
# -----------------------
@tasks.loop(seconds=30)
async def check_tasks():
    now = datetime.datetime.now(JST)
    to_remove = []

    for task in tasks_list:
        # 直近のリマインド以外は飛ばす
        remaining = sorted(
            [r for r in task["reminders"] if r not in task["notified"]],
            reverse=True
        )
        if not remaining:
            # 期限＋1か月経過でタスク削除
            if now >= task["due"] + datetime.timedelta(days=30):
                to_remove.append(task)
            continue

        next_reminder = remaining[0]
        reminder_time = task["due"] - datetime.timedelta(days=next_reminder)

        if reminder_time <= now < reminder_time + datetime.timedelta(seconds=10):
            channel = bot.get_channel(task["channel_id"])
            if channel:
                await channel.send(
                    f"⏰ {task['task']}\n"
                    f"🕒 {reminder_label(next_reminder)} / 期限: {task['due'].strftime('%m/%d %H:%M')}"
                )
            task["notified"].append(next_reminder)
            save_tasks()

    for task in to_remove:
        tasks_list.remove(task)
        print(f"🗑️ タスク削除（期限+1か月経過）: {task['task']}")

    if to_remove:
        save_tasks()

# -----------------------
# タスク削除
# -----------------------
@tree.command(name="delete", description="タスク削除", guild=GUILD_OBJ)
@app_commands.describe(index="削除するタスク番号")
async def delete(interaction: discord.Interaction, index: int):
    if not (0 < index <= len(tasks_list)):
        await interaction.response.send_message("❌ 無効な番号です", ephemeral=True)
        return

    task = tasks_list.pop(index - 1)

    save_tasks()

    await interaction.response.send_message(
        f"🗑️ 削除しました\n📌 {task['task']}"
    )

# -----------------------
# タスク完了扱い
# -----------------------
@tree.command(name="done", description="タスク完了", guild=GUILD_OBJ)
@app_commands.describe(index="完了したタスク番号")
async def done(interaction: discord.Interaction, index: int):
    if not (0 < index <= len(tasks_list)):
        await interaction.response.send_message("❌ 無効な番号です", ephemeral=True)
        return

    task = tasks_list.pop(index - 1)

    save_tasks()

    await interaction.response.send_message(
        f"✅ 完了！\n📌 {task['task']}"
    )

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
    load_tasks()
    check_tasks.start()

# -----------------------
# Bot起動
# -----------------------
bot.run(os.environ.get("TOKEN"))