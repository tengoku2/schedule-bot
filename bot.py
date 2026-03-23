import os
import discord
from discord import app_commands
import datetime
import json
import asyncio
import mysql.connector
import threading
from flask import Flask
from waitress import serve

# -----------------------
# Flask（Koyeb用）
# -----------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 8000))
    serve(app, host="0.0.0.0", port=port)

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
# 日付パース
# -----------------------
def parse_date(date_str):
    now = datetime.datetime.now()

    # YYYY-MM-DD
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    except:
        pass

    # 数字（3桁 or 4桁）
    if date_str.isdigit():
        if len(date_str) in [3, 4]:
            if len(date_str) == 3:
                month = int(date_str[0])
                day = int(date_str[1:])
            else:
                month = int(date_str[:2])
                day = int(date_str[2:])

            year = now.year
            d = datetime.date(year, month, day)

            if d < now.date():
                d = datetime.date(year + 1, month, day)

            return d

    # M/D
    if "/" in date_str:
        try:
            m, d = map(int, date_str.split("/"))
            year = now.year
            d = datetime.date(year, m, d)

            if d < now.date():
                d = datetime.date(year + 1, m, d)

            return d
        except:
            pass

    # 最後にエラー
    raise ValueError("日付形式エラー")

# -----------------------
# 時間パース
# -----------------------
def parse_time(time_str):
    if ":" in time_str:
        return datetime.datetime.strptime(time_str, "%H:%M").time()

    if time_str.isdigit():
        if len(time_str) <= 2:
            return datetime.time(int(time_str), 0)
        elif len(time_str) in [3, 4]:
            hour = int(time_str[:-2])
            minute = int(time_str[-2:])
            return datetime.time(hour, minute)

    raise ValueError("時間形式エラー")

# -----------------------
# REMINDERパース
# -----------------------
def parse_reminders(reminder_str):
    mapping = {
        "m": ("month", 30),
        "w": ("week", 7),
        "d": ("day", 1),
        "h": ("hour", 1/24),
    }

    result = []

    for part in reminder_str.split(","):
        part = part.strip().lower()

        num = int(part[:-1])
        unit = part[-1]

        if unit not in mapping:
            raise ValueError("単位エラー")

        name, base = mapping[unit]

        label = f"{num}{name}"
        days = num * base

        result.append((label, days))

    return result

# -----------------------
# データ
# -----------------------
tasks_list = []

def load_tasks():
    global tasks_list
    print("🔄 load_tasks開始")

    try:
        db, cursor = get_cursor()

        cursor.execute("""
            SELECT id, task, due, reminders, notified, channel_id, owner_id, visible_to, status 
            FROM tasks
        """)

        rows = cursor.fetchall()

        new_list = []
        for t in rows:
            new_list.append({
                "id": t["id"],
                "task": t["task"],
                "due": t["due"],
                "channel_id": t["channel_id"],
                "owner_id": t["owner_id"],
                "visible_to": json.loads(t["visible_to"] or "[]"),
                "status": t["status"],
                "notified": json.loads(t["notified"] or "[]"),
                "reminders": json.loads(t["reminders"] or "[]"),
            })

        tasks_list = new_list
        db.close()

    except Exception as e:
        print("❌ load_tasks失敗:", e)

# -----------------------
# Discord設定
# -----------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------
# DB INSERT
# -----------------------
def insert_task(task_name, due, channel_id, user_id, reminders):
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
        json.dumps(reminders),  # ←ここ修正🔥
        json.dumps([]),
        False,
        False,
        "todo"
    ))

    db.commit()
    db.close()

# -----------------------
# /add
# -----------------------
@tree.command(name="add", description="タスク追加")
async def add(
    interaction: discord.Interaction,
    task_name: str,
    date_str: str = None,
    time_str: str = None,
    reminders: str = None
):

    print("🔥 add開始")

    # deferやめる
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("⏳ 追加中...", ephemeral=True)
        else:
            await interaction.followup.send("⏳ 追加中...", ephemeral=True)
    except:
        pass  # ← ここ重要（握りつぶす）

    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    try:
        # 両方なし
        if not date_str and not time_str:
            tomorrow = now.date() + datetime.timedelta(days=1)
            due = datetime.datetime.combine(tomorrow, datetime.time(0, 0)).replace(tzinfo=JST)

        # dateなし
        elif not date_str:
            t = parse_time(time_str)
            today_due = datetime.datetime.combine(now.date(), t).replace(tzinfo=JST)

            if today_due > now:
                due = today_due
            else:
                due = today_due + datetime.timedelta(days=1)

        # timeなし
        elif not time_str:
            d = parse_date(date_str)

            if d == now.date():
                due = datetime.datetime.combine(d, datetime.time(23, 59)).replace(tzinfo=JST)
            else:
                due = datetime.datetime.combine(d, datetime.time(0, 0)).replace(tzinfo=JST)

        # 両方あり
        else:
            d = parse_date(date_str)
            t = parse_time(time_str)
            due = datetime.datetime.combine(d, t).replace(tzinfo=JST)

    except:
        await interaction.edit_original_response(content="❌ 日時形式エラー")
        return

    # リマインド
    try:
        if reminders:
            reminder_data = parse_reminders(reminders)
        else:
            reminder_data = DEFAULT_REMINDERS
    except:
        await interaction.edit_original_response(content="❌ リマインド形式エラー")
        return

    filtered = []
    for label, days in reminder_data:
        remind_time = due - datetime.timedelta(days=days)
        if remind_time > now:
            filtered.append(label)

    try:
        await asyncio.to_thread(
            insert_task,
            task_name,
            due,
            interaction.channel.id,
            interaction.user.id,
            filtered
        )
    except Exception as e:
        print(e)
        await interaction.edit_original_response(content="❌ DBエラー")
        return

    await interaction.edit_original_response(
        content=f"✅ 追加: {task_name}\n📅 {due.strftime('%m/%d %H:%M')}"
    )

    asyncio.create_task(asyncio.to_thread(load_tasks))

# -----------------------
# /delete
# -----------------------
def delete_task(task_id):
    db, cursor = get_cursor()

    cursor.execute("DELETE FROM tasks WHERE id=%s", (task_id,))

    db.commit()
    db.close()

@tree.command(name="delete", description="タスク削除")
async def delete_task_cmd(interaction: discord.Interaction, index: int):

    print("🔥 delete開始", index)

    # これが最重要
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print("defer失敗:", e)

    # 最新DB
    await asyncio.to_thread(load_tasks)

    print("タスク数:", len(tasks_list))

    if not tasks_list:
        await interaction.edit_original_response(content="📭 タスクなし")
        return

    if index < 1 or index > len(tasks_list):
        await interaction.edit_original_response(content="❌ 番号が不正")
        return

    task = tasks_list[index - 1]

    print("削除対象:", task["task"], task["id"])

    try:
        await asyncio.to_thread(delete_task, task["id"])
    except Exception as e:
        print("削除エラー:", e)
        await interaction.edit_original_response(content="❌ 削除失敗")
        return

    await interaction.edit_original_response(
        content=f"✅ 削除: {task['task']}"
    )

    await asyncio.to_thread(load_tasks)
# -----------------------
# /list
# -----------------------
@tree.command(name="list", description="タスク一覧")
async def list_tasks(interaction: discord.Interaction):

    await interaction.response.send_message("⏳ 読み込み中...", ephemeral=True)

    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    await asyncio.to_thread(load_tasks)

    if not tasks_list:
        await interaction.edit_original_response(content="📭 タスクなし")
        return

    msg = "📋 タスク一覧\n"

    for i, t in enumerate(tasks_list, 1):
        due = t["due"]

        # タイムゾーン補正
        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)

        msg += f"{i}. {t['task']}\n"
        msg += f"📅 {due.strftime('%m/%d %H:%M')}\n"

        remaining = []

        for r in t.get("reminders", []):
            if isinstance(r, list):
                r = r[0]

            import re
            match = re.match(r"(\d+)", r)
            if not match:
                continue

            num = int(match.group(1))

            if "month" in r:
                days = num * 30
            elif "week" in r:
                days = num * 7
            elif "day" in r:
                days = num
            elif "hour" in r:
                days = num / 24
            else:
                continue

            remind_time = due - datetime.timedelta(days=days)

            # JSTで比較
            if remind_time <= now:
                continue

            if r not in t.get("notified", []):
                remaining.append(label_to_text(r))

        if remaining:
            msg += "🔔 " + ", ".join(remaining) + "\n"

        msg += "\n"

    await interaction.edit_original_response(content=msg)
# -----------------------
# リマインド
# -----------------------
from discord.ext import tasks

DEFAULT_REMINDERS = [
    ("1month", 30),
    ("2weeks", 14),
    ("1week", 7),
    ("3days", 3),
    ("1day", 1),
    ("3hours", 3/24),
]

REMINDERS = [
    ("1month", 30),
    ("2weeks", 14),
    ("1week", 7),
    ("3days", 3),
    ("1day", 1),
    ("3hours", 3/24),
]

def label_to_text(label):
    import re

    # 先に複数形を処理
    label = label.replace("months", "month")
    label = label.replace("weeks", "week")
    label = label.replace("days", "day")
    label = label.replace("hours", "hour")

    # 数字だけ抜く
    match = re.match(r"(\d+)", label)
    if not match:
        return label

    num = match.group(1)

    # 表示
    if "month" in label:
        return f"{num}ヶ月前"
    elif "week" in label:
        return f"{num}週間前"
    elif "day" in label:
        return f"{num}日前"
    elif "hour" in label:
        return f"{num}時間前"

    return label

@tasks.loop(seconds=30)
async def reminder_loop():
    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    print("==== LOOP START ====")
    print("NOW:", now)

    # 最新DB読み込み
    await asyncio.to_thread(load_tasks)

    print("TASK COUNT:", len(tasks_list))

    for t in tasks_list:
        print("\n--- TASK ---")
        print("TASK:", t["task"])

        due = t["due"]

        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)

        print("DUE:", due)

        # 期限通知（ここ追加）
        notified = t.get("notified", [])

        if "due" not in notified:
            if now >= due:
                print("🔥 期限通知発火")

                channel = bot.get_channel(t["channel_id"])
                if channel:
                    await channel.send(f"⏰ 期限です: {t['task']}")

                notified.append("due")

                db, cursor = get_cursor()
                cursor.execute(
                    "UPDATE tasks SET notified=%s WHERE id=%s",
                    (json.dumps(notified), t["id"])
                )
                db.commit()
                db.close()

        notified = t.get("notified", [])
        reminder_settings = t.get("reminders", [])

        print("REMINDERS:", reminder_settings)
        print("NOTIFIED:", notified)

        import re

        for label in reminder_settings:
            match = re.match(r"(\d+)", label)
            if not match:
                print("SKIP: フォーマット不正", label)
                continue

            num = int(match.group(1))

            if "month" in label:
                days = num * 30
            elif "week" in label:
                days = num * 7
            elif "day" in label:
                days = num
            elif "hour" in label:
                days = num / 24
            else:
                print("SKIP: 単位不明", label)
                continue

            remind_time = due - datetime.timedelta(days=days)

            print(f"CHECK: {label}")
            print("  remind_time:", remind_time)

            if label in notified:
                print("  SKIP: 既に通知済み")
                continue

            if remind_time <= now <= remind_time + datetime.timedelta(seconds=30):
                print("  🔥 HIT!!!! SEND!!!!")

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
            else:
                print("  NO HIT")

from discord.ext import tasks

@tasks.loop(minutes=5)
async def keep_db_alive():
    try:
        db, cursor = get_cursor()
        cursor.execute("SELECT 1")
        db.close()
        print("💓 DB keep alive")
    except Exception as e:
        print("❌ DB keep alive error:", e)

# -----------------------
# 起動
# -----------------------
GUILD_ID = 1479381180146257950

@bot.event
async def on_ready():
    print("🚀 起動完了")

    guild = discord.Object(id=GUILD_ID)

    tree.clear_commands(guild=guild)  # ←重要
    await tree.sync(guild=guild)

    print("✅ ギルド同期完了")

    try:
        await asyncio.to_thread(load_tasks)
    except Exception as e:
        print("❌ 初回load失敗:", e)

    await tree.sync()
    print("✅ コマンド同期完了")

    if not reminder_loop.is_running():
        reminder_loop.start()

    if not keep_db_alive.is_running():
        keep_db_alive.start()

    print("🔔 リマインド開始")
# -----------------------
# 実行
# -----------------------
def start_bot():
    bot.run(os.environ.get("TOKEN"))

if __name__ == "__main__":
    import threading

    # 🔥 Flaskを裏で起動
    threading.Thread(target=run_web, daemon=True).start()

    # 🔥 Botをメインで起動（超重要）
    bot.run(os.environ.get("TOKEN"))