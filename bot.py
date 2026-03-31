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
            SELECT id, task, due, reminders, notified, channel_id, owner_id, visible_to, status, guild_id
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
                "guild_id": t["guild_id"],
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
def insert_task(task_name, due, channel_id, user_id, guild_id, reminders):
    db, cursor = get_cursor()

    cursor.execute("""
    INSERT INTO tasks 
    (task, due, channel_id, owner_id, visible_to, roles, reminders, notified, mention, everyone, status, guild_id)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        task_name,
        due,
        channel_id,
        user_id,
        json.dumps([]),
        json.dumps([]),
        json.dumps(reminders),  # ←そのまま保存
        json.dumps([]),
        False,
        False,
        "todo",
        guild_id
    ))

    db.commit()
    db.close()

# -----------------------
# /view
# -----------------------
class DeleteConfirmView(discord.ui.View):
    def __init__(self, task):
        super().__init__(timeout=30)
        self.task = task
        self.message = None  # ←追加

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except:
            pass

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):

        # 先に即レスポンス（これが重要）
        await interaction.response.edit_message(
            content=f"⏳ 削除中...",
            view=None
        )

        try:
            # 後で処理
            await asyncio.to_thread(delete_task, self.task["id"])
            await asyncio.to_thread(load_tasks)
            print("✅ 削除完了:", self.task["id"], self.task["task"])

        except Exception as e:
            print("削除エラー:", e)
            await interaction.followup.send("❌ 削除失敗", ephemeral=True)
            return

        # 完了通知
        await interaction.followup.send(
            f"✅ 削除: {self.task['task']}",
            ephemeral=True
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="❌ キャンセルしました",
            view=None
        )

# -----------------------
# /status
# -----------------------
def update_status(task_id, status):
    db, cursor = get_cursor()

    cursor.execute(
        "UPDATE tasks SET status=%s WHERE id=%s",
        (status, task_id)
    )

    db.commit()
    db.close()

@tree.command(name="status", description="タスク状態変更")
async def status_cmd(
    interaction: discord.Interaction,
    task_id: int,
    status: str
):

    print("status変更", task_id, status)

    if status not in ["todo", "done"]:
        await interaction.response.send_message("todo か done を指定", ephemeral=True)
        return

    try:
        await interaction.response.send_message("更新中...", ephemeral=True)
    except:
        pass

    await asyncio.to_thread(load_tasks)

    task = next((t for t in tasks_list if t["id"] == task_id), None)

    if not task:
        await interaction.edit_original_response(content="タスクが見つからない")
        return

    try:
        await asyncio.to_thread(update_status, task_id, status)
        await asyncio.to_thread(load_tasks)
    except Exception as e:
        print("status更新エラー:", e)
        await interaction.edit_original_response(content="更新失敗")
        return

    await interaction.edit_original_response(
        content=f"状態更新\n[{task_id}] {task['task']} → {status}"
    )


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
            filtered.append({
                "label": label,
                "days": days
            })

    try:
        await asyncio.to_thread(
            insert_task,
            task_name,
            due,
            interaction.channel.id,
            interaction.user.id,
            interaction.guild.id,
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
async def delete_task_cmd(interaction: discord.Interaction, task_id: int):

    print("delete開始", task_id)

    try:
        await interaction.response.defer(ephemeral=True)
    except Exception as e:
        print("defer失敗:", e)

    await asyncio.to_thread(load_tasks)

    print("タスク数:", len(tasks_list))

    # IDで取得
    task = next((t for t in tasks_list if t["id"] == task_id), None)

    if not task:
        await interaction.edit_original_response(content="タスクが見つからない")
        return

    print("削除対象:", task["task"], task["id"])

    view = DeleteConfirmView(task)

    await interaction.edit_original_response(
        content=f"削除しますか\n{task['task']}",
        view=view
    )

# -----------------------
# /edit
# -----------------------
def update_task_full(task_id, task_name, due, reminders):
    db, cursor = get_cursor()

    cursor.execute(
        "UPDATE tasks SET task=%s, due=%s, reminders=%s WHERE id=%s",
        (task_name, due, json.dumps(reminders), task_id)
    )

    db.commit()
    db.close()

@tree.command(name="edit", description="タスク編集")
async def edit_task_cmd(
    interaction: discord.Interaction,
    task_id: int,
    task_name: str = None,
    date_str: str = None,
    time_str: str = None,
    reminders: str = None
):

    print("edit開始", task_id)

    try:
        await interaction.response.send_message("処理中...", ephemeral=True)
    except:
        pass

    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    await asyncio.to_thread(load_tasks)

    # IDで検索
    task = next((t for t in tasks_list if t["id"] == task_id), None)

    if not task:
        await interaction.edit_original_response(content="タスクが見つからない")
        return
    
    db, cursor = get_cursor()
    cursor.execute(
        "UPDATE tasks SET notified=%s WHERE id=%s",
        (json.dumps([]), task["id"])
    )
    db.commit()
    db.close()

    old_due = task["due"]
    if old_due.tzinfo is None:
        old_due = old_due.replace(tzinfo=JST)

    new_name = task_name if task_name else task["task"]

    # reminders
    try:
        if reminders:
            reminder_data = parse_reminders(reminders)
            new_reminders = [
                {"label": label, "days": days}
                for label, days in reminder_data
            ]
        else:
            new_reminders = task.get("reminders", [])
    except:
        await interaction.edit_original_response(content="リマインド形式エラー")
        return

    try:
        # 日付と時間どっちも未指定
        if not date_str and not time_str:
            new_due = old_due

        # 日付なし
        elif not date_str:
            t = parse_time(time_str)
            new_due = datetime.datetime.combine(old_due.date(), t).replace(tzinfo=JST)

        # 時間なし
        elif not time_str:
            d = parse_date(date_str)
            new_due = datetime.datetime.combine(d, old_due.time()).replace(tzinfo=JST)

        # 両方あり
        else:
            d = parse_date(date_str)
            t = parse_time(time_str)
            new_due = datetime.datetime.combine(d, t).replace(tzinfo=JST)

    except:
        await interaction.edit_original_response(content="日時形式エラー")
        return

    try:
        await asyncio.to_thread(
            update_task_full,
            task["id"],
            new_name,
            new_due,
            new_reminders
        )
        await asyncio.to_thread(load_tasks)

    except Exception as e:
        print("編集エラー:", e)
        await interaction.edit_original_response(content="編集失敗")
        return

    await interaction.edit_original_response(
        content=(
            f"更新完了\n"
            f"[{task_id}] {new_name}\n"
            f"{new_due.strftime('%m/%d %H:%M')}\n"
            f"reminders: {', '.join(new_reminders) if new_reminders else 'なし'}"
        )
    )

# -----------------------
# /channel
# -----------------------
# def update_channel(task_id, channel_id):
#     db, cursor = get_cursor()

#     cursor.execute(
#         "UPDATE tasks SET channel_id=%s WHERE id=%s",
#         (channel_id, task_id)
#     )

#     db.commit()
#     db.close()

# @tree.command(name="channel", description="送信チャンネル変更")
# async def set_channel(
#     interaction: discord.Interaction,
#     task_id: int,
#     channel: discord.TextChannel
# ):

#     try:
#         await interaction.response.send_message("変更中...", ephemeral=True)
#     except:
#         pass

#     await asyncio.to_thread(load_tasks)

#     task = next((t for t in tasks_list if t["id"] == task_id), None)

#     if not task:
#         await interaction.edit_original_response(content="タスクが見つからない")
#         return

#     try:
#         await asyncio.to_thread(update_channel, task_id, channel.id)
#         await asyncio.to_thread(load_tasks)
#     except Exception as e:
#         print("チャンネル更新エラー:", e)
#         await interaction.edit_original_response(content="更新失敗")
#         return

#     await interaction.edit_original_response(
#         content=f"送信先変更\n[{task_id}] → #{channel.name}"
#     )


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

    # サーバーエラー用
    if not interaction.guild:
        await interaction.edit_original_response(content="サーバー内で使ってください")
        return
    
    msg = "📋 タスク一覧\n"

    i = 1
    for t in tasks_list:

        # サーバー分離
        if t.get("guild_id") != interaction.guild.id:
            continue

        # チャンネル分離
        if t["channel_id"] != interaction.channel.id:
            continue

        if t["status"] != "todo":
            continue

        msg += f"{i}. [{t['id']}] {t['task']}\n"
        
        due = t["due"]

        # タイムゾーン補正
        if due.tzinfo is None:
            due = due.replace(tzinfo=JST)

        msg += f"📅 {due.strftime('%m/%d %H:%M')}\n"
        
        i += 1

        remaining = []

        for r in t.get("reminders", []):
            label = r["label"]
            days = r["days"]

            remind_time = due - datetime.timedelta(days=days)

            if remind_time <= now:
                continue

            if label not in t.get("notified", []):
                remaining.append(label_to_text(label))

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

        # -----------------------
        # 日次リマインド（NEW）
        # -----------------------
        today_str = now.strftime("%Y-%m-%d")
        notified = t.get("notified", [])

        # doneならスキップ
        if t.get("status") != "done":

            # 0:00〜0:01で1回だけ
            if now.hour == 0 and now.minute <= 1:

                if today_str not in notified:
                    print("🌙 日次リマインド")

                    channel = bot.get_channel(t["channel_id"])
                    if channel:
                        await channel.send(f"🔁 未完了タスク: {t['task']}")

                    notified.append(today_str)

                    db, cursor = get_cursor()
                    cursor.execute(
                        "UPDATE tasks SET notified=%s WHERE id=%s",
                        (json.dumps(notified), t["id"])
                    )
                    db.commit()
                    db.close()

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

        for r in reminder_settings:
            label = r["label"]
            days = r["days"]

            remind_time = due - datetime.timedelta(days=days)

            if label in notified:
                continue

            if remind_time <= now <= remind_time + datetime.timedelta(seconds=30):

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
    print("コマンド一覧:", [c.name for c in tree.get_commands()])
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