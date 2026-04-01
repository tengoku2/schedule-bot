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
from typing import Literal
from discord import app_commands

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
# パース関数
# -----------------------
def parse_datetime_input(dt_str):
    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    if not dt_str:
        tomorrow = now.date() + datetime.timedelta(days=1)
        return datetime.datetime.combine(
            tomorrow,
            datetime.time(0, 0),
            tzinfo=JST
        )

    if not dt_str.isdigit():
        raise ValueError("数値で入力してください")

    n = len(dt_str)

    # -----------------------
    # 時間のみ（1〜4桁）
    # -----------------------
    if n <= 4:
        if n <= 2:
            hour = int(dt_str)
            minute = 0
        else:
            hour = int(dt_str[:-2])
            minute = int(dt_str[-2:])

        if hour > 24 or minute >= 60:
            raise ValueError("時間形式エラー")

        if hour == 24:
            hour = 0
            add_day = True
        else:
            add_day = False

        due = datetime.datetime.combine(
            now.date(),
            datetime.time(hour, minute),
            tzinfo=JST
        )

        if due <= now or add_day:
            due += datetime.timedelta(days=1)

        return due

    # -----------------------
    # 日付 + 時間（5〜8桁）
    # -----------------------
    time_part = dt_str[-4:]
    hour = int(time_part[:2])
    minute = int(time_part[2:])

    if hour >= 24 or minute >= 60:
        raise ValueError("時間形式エラー")

    date_part = dt_str[:-4]

    # MMDD or MD の曖昧解消ルール
    if len(date_part) == 2:
        month = int(date_part[0])
        day = int(date_part[1])

    elif len(date_part) == 3:
        month = int(date_part[0])
        day = int(date_part[1:3])

    elif len(date_part) == 4:
        month = int(date_part[:2])
        day = int(date_part[2:4])

    else:
        raise ValueError("日付形式エラー")

    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError("日付エラー")

    year = now.year

    try:
        due = datetime.datetime(year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        raise ValueError("存在しない日付です")

    if due <= now:
        due = due.replace(year=year + 1)

    return due

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
# /is_manager 権限管理！
# -----------------------
def is_manager(interaction):
    try:
        db, cursor = get_cursor()

        cursor.execute(
            "SELECT manager_role_id FROM guild_settings WHERE guild_id=%s",
            (interaction.guild.id,)
        )

        row = cursor.fetchone()
        db.close()

        if not row:
            return False

        role_id = row["manager_role_id"]

        return any(role.id == role_id for role in interaction.user.roles)

    except Exception as e:
        print("manager取得エラー:", e)
        return False


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
    status: Literal["todo", "done"]
):

    print("status変更", task_id, status)

    try:
        await interaction.response.send_message("更新中...", ephemeral=True)
    except:
        pass

    await asyncio.to_thread(load_tasks)

    task = next((t for t in tasks_list if t["id"] == task_id), None)

    # タスク確認
    if not task:
        await interaction.edit_original_response(content="タスクが見つからない")
        return
    
    # 権限確認
    if not (
        interaction.user.id == task["owner_id"]
        or is_manager(interaction)
    ):
        await interaction.edit_original_response(content="❌ 権限がありません")
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
    dt_str: str = None,
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
        due = parse_datetime_input(dt_str)
    except Exception as e:
        print("日時パースエラー:", e)
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

    notify_channel_id = get_notify_channel(interaction.guild.id)

    if notify_channel_id:
        ch = bot.get_channel(notify_channel_id)
        if ch:
            await ch.send(
                f"🆕 タスク追加: {task_name}\n"
                f"📅 {due.strftime('%m/%d %H:%M')}"
            )

    asyncio.create_task(asyncio.to_thread(load_tasks))

# /add Autocomplete
@add.autocomplete("dt_str")
async def add_dt_autocomplete(interaction: discord.Interaction, current: str):
    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    if not current:
        return [
            app_commands.Choice(name="明日の0時", value=""),
            app_commands.Choice(name="3時間後", value="300"),
            app_commands.Choice(name="今日18:00", value="1800"),
        ]

    if not current.isdigit():
        return []

    try:
        due = parse_datetime_input(current)
        diff = due - now

        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60

        parts = []
        if days > 0:
            parts.append(f"{days}日")
        if hours > 0:
            parts.append(f"{hours}時間")
        if minutes > 0:
            parts.append(f"{minutes}分")

        remain_text = "".join(parts) if parts else "すぐ"

        label = f"{current} → {due.strftime('%m/%d %H:%M')}（{remain_text}後）"

        return [
            app_commands.Choice(name=label, value=current)
        ]

    except Exception:
        return [
            app_commands.Choice(name="❌ 形式エラー", value=current)
        ]

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

    # タスク確認
    if not task:
        await interaction.edit_original_response(content="タスクが見つからない")
        return

    # 権限確認
    if not (
        interaction.user.id == task["owner_id"]
        or is_manager(interaction)
    ):
        await interaction.edit_original_response(content="❌ 権限がありません")
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
    dt_str: str = None,
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

    # タスク確認
    if not task:
        await interaction.edit_original_response(content="タスクが見つからない")
        return

    # 権限確認
    if not (
        interaction.user.id == task["owner_id"]
        or is_manager(interaction)
    ):
        await interaction.edit_original_response(content="❌ 権限がありません")
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

    # 日付処理
    try:
        if dt_str:
            new_due = parse_datetime_input(dt_str)
        else:
            new_due = old_due
    except Exception as e:
        print("edit日時エラー:", e)
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
            f"reminders: {', '.join([r['label'] for r in new_reminders]) if new_reminders else 'なし'}"
        )
    )

# /edit Autocomplete
@edit_task_cmd.autocomplete("dt_str")
async def edit_dt_autocomplete(interaction: discord.Interaction, current: str):
    JST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(JST)

    if not current:
        return [
            app_commands.Choice(name="明日の0時", value=""),
            app_commands.Choice(name="3時間後", value="300"),
            app_commands.Choice(name="今日18:00", value="1800"),
        ]

    if not current.isdigit():
        return []

    try:
        due = parse_datetime_input(current)
        diff = due - now

        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60

        parts = []
        if days > 0:
            parts.append(f"{days}日")
        if hours > 0:
            parts.append(f"{hours}時間")
        if minutes > 0:
            parts.append(f"{minutes}分")

        remain_text = "".join(parts) if parts else "すぐ"

        label = f"{current} → {due.strftime('%m/%d %H:%M')}（{remain_text}後）"

        return [
            app_commands.Choice(name=label, value=current)
        ]

    except Exception:
        return [
            app_commands.Choice(name="❌ 形式エラー", value=current)
        ]

# -----------------------
# /list
# -----------------------
@tree.command(name="list", description="タスク一覧")
async def list_tasks(
    interaction: discord.Interaction,
    mode: Literal["todo", "done", "all"] = "todo"
):

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
    
    msg = f"📋 タスク一覧 ({mode})\n"

    i = 1
    for t in tasks_list:

        # サーバー分離
        if t.get("guild_id") != interaction.guild.id:
            continue

        # チャンネル分離と合同
        if mode != "all":
            if t["channel_id"] != interaction.channel.id:
                continue
        
        # ステータス
        if mode == "todo":
            if t["status"] != "todo":
                continue

        elif mode == "done":
            if t["status"] != "done":
                continue

        elif mode == "all":
            pass

        else:
            await interaction.edit_original_response(content="modeは todo / done / all")
            return

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
        if t.get("status") != "done" and now >= due:

            # 1回だけ
            if now.hour == 0 and now.minute == 0:

                if today_str not in notified:
                    print("🌙 日次リマインド")

                    notify_channel_id = get_notify_channel(t["guild_id"])

                    target_channel_id = notify_channel_id or t["channel_id"]

                    channel = bot.get_channel(target_channel_id)
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

        # 期限通知
        notified = t.get("notified", [])

        if "due" not in notified:
            if now >= due:
                print("🔥 期限通知発火")

                notify_channel_id = get_notify_channel(t["guild_id"])

                target_channel_id = notify_channel_id or t["channel_id"]

                channel = bot.get_channel(target_channel_id)
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

                notify_channel_id = get_notify_channel(t["guild_id"])

                target_channel_id = notify_channel_id or t["channel_id"]

                channel = bot.get_channel(target_channel_id)
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
# /channel
# -----------------------
def get_notify_channel(guild_id):
    db, cursor = get_cursor()

    cursor.execute(
        "SELECT notify_channel_id FROM guild_settings WHERE guild_id=%s",
        (guild_id,)
    )

    row = cursor.fetchone()
    db.close()

    return row["notify_channel_id"] if row else None

@tree.command(name="set_notify_channel", description="通知チャンネル設定")
async def set_notify_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("管理者のみ設定可能", ephemeral=True)
        return

    db, cursor = get_cursor()

    cursor.execute("""
    INSERT INTO guild_settings (guild_id, notify_channel_id)
    VALUES (%s, %s)
    ON DUPLICATE KEY UPDATE notify_channel_id=%s
    """, (
        interaction.guild.id,
        channel.id,
        channel.id
    ))

    db.commit()
    db.close()

    await interaction.response.send_message(
        f"✅ 通知チャンネル設定: {channel.name}",
        ephemeral=True
    )

# -----------------------
# /set_manager_role
# -----------------------
@tree.command(name="set_manager_role", description="管理ロール設定")
async def set_manager_role(
    interaction: discord.Interaction,
    role: discord.Role
):

    # サーバー外対策
    if not interaction.guild:
        await interaction.response.send_message("サーバー内で使ってください", ephemeral=True)
        return

    # 権限チェック（初期設定用）
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "サーバー管理者のみ設定できます",
            ephemeral=True
        )
        return
        

    try:
        await interaction.response.send_message("設定中...", ephemeral=True)
    except:
        pass

    try:
        db, cursor = get_cursor()

        cursor.execute("""
        INSERT INTO guild_settings (guild_id, manager_role_id)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE manager_role_id=%s
        """, (
            interaction.guild.id,
            role.id,
            role.id
        ))

        db.commit()
        db.close()

    except Exception as e:
        print("設定エラー:", e)
        await interaction.edit_original_response(content="❌ 設定失敗")
        return

    await interaction.edit_original_response(
        content=f"✅ 管理ロール設定: {role.name}"
    )

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

    # Flaskを裏で起動
    threading.Thread(target=run_web, daemon=True).start()

    # Botをメインで起動（超重要）
    bot.run(os.environ.get("TOKEN"))