import os
import discord
from discord.ext import tasks
from discord import app_commands
from flask import Flask
import threading
import datetime
import json
import asyncio
import mysql.connector

def get_db():
    return mysql.connector.connect(
    host=os.environ.get("DB_HOST"),
    port=int(os.environ.get("DB_PORT", 15042)),
    user=os.environ.get("DB_USER"),
    password=os.environ.get("DB_PASS"),
    database=os.environ.get("DB_NAME"),
    ssl_disabled=False
    )

def get_cursor():
    db = get_db()
    return db, db.cursor(dictionary=True)

DATA_FILE = "tasks.json"

# -----------------------
# JSTタイムゾーン
# -----------------------
JST = datetime.timezone(datetime.timedelta(hours=9))

# -----------------------
# MySQL読み込み/ load_tasks
# -----------------------
def load_tasks():
    global tasks_list
    print("🔄 load_tasks開始")

    db, cursor = get_cursor()

    cursor.execute("SELECT * FROM tasks")
    rows = cursor.fetchall()

    print("📦 rows取得:", len(rows))

    tasks_list = []

    for t in rows:
        tasks_list.append({
            "id": t["id"],
            "task": t["task"],
            "due": t["due"].astimezone(JST),
            "channel_id": t["channel_id"],
            "owner_id": t["owner_id"],
            "visible_to": json.loads(t["visible_to"] or "[]"),
            "reminders": json.loads(t["reminders"] or "[]"),
            "notified": json.loads(t["notified"] or "[]"),
            "mention": t["mention"],
            "roles": json.loads(t["roles"] or "[]"),
            "status": t["status"],
            "completed_by": t["completed_by"],
            "completed_at": t["completed_at"],
            "everyone": t["everyone"],
        })

    db.close()

# -----------------------
# Flaskヘルスチェック & Discord Bot設定
# -----------------------
app = Flask(__name__)

started = False

@app.route("/")
def home():
    global started

    if not started:
        print("🔥 Bot起動")
        threading.Thread(target=start_bot, daemon=True).start()
        started = True

    return "OK", 200
def run_web():
    port = int(os.environ.get("PORT", 8000))
    print(f"Flask starting on {port}")
    app.run(host="0.0.0.0", port=port)


# Discord Bot設定
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
tasks_list = []

# サーバー専用 or グローバル
GUILD_ID = os.environ.get("GUILD_ID")

try:
    GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
except:
    GUILD_OBJ = None
# -----------------------
# 権限チェック
# -----------------------
def can_view(task, user):
    # 管理者は全部見れる
    if user.guild_permissions.administrator:
        return True

    # 👇 これ追加（超重要）
    if not task["visible_to"]:
        return True

    # 個別指定
    if user.id in task["visible_to"]:
        return True

    # ロール指定
    user_role_ids = [r.id for r in user.roles]
    return any(rid in user_role_ids for rid in task.get("roles", []))

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
# Flask画面追加
# -----------------------
from flask import render_template_string, request, redirect

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Task Manager</title>
</head>
<body>
    <h1>タスク一覧</h1>

    <form method="POST" action="/add_web">
        <input name="task" placeholder="タスク名">
        <input type="datetime-local" name="date">
        <button type="submit">追加</button>
    </form>

    <ul>
    {% for i, t in tasks %}
        <li>
            {{t["task"]}} | {{t["due"]}} | {{t["status"]}}
            <a href="/done_web/{{i}}">完了</a>
            <a href="/delete_web/{{i}}">削除</a>
        </li>
    {% endfor %}
    </ul>
</body>
</html>
"""

@app.route("/dashboard")
def dashboard():
    return render_template_string(HTML, tasks=list(enumerate(tasks_list)))

# -----------------------
# Webから追加 /add_web
# -----------------------
from flask import session
app.secret_key = os.environ.get("FLASK_SECRET", "devkey")

@app.route("/add_web", methods=["POST"])
def add_web():
    task_name = request.form.get("task")
    date_str = request.form.get("date")  # ← これ追加

    if not date_str:
        due = datetime.datetime.now(JST).replace(hour=23, minute=59)
    else:
        try:
            due = datetime.datetime.fromisoformat(date_str).replace(tzinfo=JST)
        except:
            return "日付エラー"
    
    task = {
        "task": task_name,
        "due": due,
        "channel_id": None,
        "owner_id": session.get("user_id", 0), # Discord OAuth入れたら変える
        "visible_to": [],
        "reminders": [0],
        "notified": [],
        "mention": False,
        "roles": [],
        "status": "todo",
        "completed_by": None,
        "completed_at": None
    }

    # DB保存
    try:
        db, cursor = get_cursor()

        cursor.execute("""
        INSERT INTO tasks 
        (task, due, channel_id, owner_id, visible_to, roles, reminders, notified, mention, everyone, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            task_name,
            due,
            None,
            session.get("user_id"),
            json.dumps([]),
            json.dumps([]),
            json.dumps([0]),
            json.dumps([]),
            False,
            False,
            "todo"
        ))

        db.commit()
        db.close()
        load_tasks()

        return redirect(f"/dashboard?key={SECRET}")
    
    except Exception as e:
        print(e)
        return "❌ DBエラー", 500

# Webから完了 /done_web
@app.route("/done_web/<int:index>")
def done_web(index):
    if 0 <= index < len(tasks_list):
        task = tasks_list[index]

        # DB保存
        try:
            db, cursor = get_cursor()

            cursor.execute("""
            UPDATE tasks 
            SET status=%s, completed_at=%s
            WHERE id=%s AND owner_id=%s
            """, (
                "done",
                datetime.datetime.now(JST),
                task["id"],
                task["owner_id"]
            ))

            db.commit()
            db.close()
            load_tasks()

            return redirect(f"/dashboard?key={SECRET}")

        except Exception as e:
            print(e)
            return "❌ DBエラー", 500

# Webから削除
@app.route("/delete_web/<int:index>")
def delete_web(index):
    if 0 <= index < len(tasks_list):
        task = tasks_list[index]

        #DB保存
        try:
            db, cursor = get_cursor()

            cursor.execute("""
            DELETE FROM tasks 
            WHERE id=%s AND owner_id=%s
            """, (
                task["id"],
                task["owner_id"]
            ))

            db.commit()
            db.close()
            load_tasks()

            return redirect(f"/dashboard?key={SECRET}")
        
        except Exception as e:
            print(e)
            return "❌ DBエラー", 500


# -----------------------
# /list コマンド
# -----------------------
@tree.command(name="list", description="タスク一覧を表示", guild=GUILD_OBJ)
async def list_tasks(interaction: discord.Interaction):

    await interaction.response.defer()

    user = interaction.user
    visible_tasks = [t for t in tasks_list if can_view(t, user) and t.get("status") != "done"]

    if not visible_tasks:
        await interaction.followup.send("📭 タスクはありません")
        return

    msg = "📋 タスク一覧\n"
    for i, task in enumerate(visible_tasks, start=1):
        status_emoji = {"todo":"📝","doing":"🚀","done":"✅"}

        if task.get("everyone"):
            viewers = "@everyone"
        elif not task["visible_to"] and not task.get("roles"):
            viewers = "全員"
        else:
            viewers = " ".join(
                [f"<@{uid}>" for uid in task["visible_to"]] +
                [f"<@&{rid}>" for rid in task.get("roles", [])]
            )

        msg += (
            f"{i}. {status_emoji.get(task['status'],'📝')} {task['task']}（作成者: <@{task['owner_id']}>）\n"
            f"📅 {task['due'].strftime('%m/%d %H:%M')}\n"
            f"🔔 {', '.join([reminder_label(r) for r in task['reminders']])}\n"
            f"👀 見れる人: {viewers}\n\n"
        )

    await interaction.followup.send(msg)

# -----------------------
# /add コマンド
# -----------------------

@tree.command(name="add", description="新しいタスクを追加", guild=GUILD_OBJ)
async def add(
    interaction: discord.Interaction,
    task_name: str,
    date: str = None,
    time: str = None,
    channel: discord.TextChannel = None,
    mention: bool = False,
    reminders: str = None,
    visible: str = None,
    roles: str = None,
    everyone: bool = False,
):
    now = datetime.datetime.now(JST)
    print("① add開始")  # ←test
    await interaction.response.defer()  # 追加！！！
    print("② defer完了")  # ←test

    # 日付処理  
    if date and len(date)==3: date=date.zfill(4)
    if time and len(time)==3: time=time.zfill(4)
    
    try:
        if not date and not time:
            due=now.replace(hour=23,minute=59,second=0,microsecond=0)
        elif date and not time:
            if len(date)==4:
                year=now.year
                due=datetime.datetime.strptime(f"{year}{date} 0000","%Y%m%d %H%M").replace(tzinfo=JST)
                if due<now: due=due.replace(year=year+1)
            else:
                due=datetime.datetime.strptime(f"{date} 0000","%Y%m%d %H%M").replace(tzinfo=JST)
        elif time and not date:
            due=datetime.datetime.strptime(now.strftime("%Y%m%d")+" "+time,"%Y%m%d %H%M").replace(tzinfo=JST)
            if due<now: due+=datetime.timedelta(days=1)
        else:
            if len(date)==4:
                year=now.year
                due=datetime.datetime.strptime(f"{year}{date} {time}","%Y%m%d %H%M").replace(tzinfo=JST)
                if due<now: due=due.replace(year=year+1)
            else:
                due=datetime.datetime.strptime(f"{date} {time}","%Y%m%d %H%M").replace(tzinfo=JST)
    except:
        await interaction.followup.send("❌ 日付/時間形式が不正", ephemeral=True)
        return
    print("③ 日付処理OK", due) # test
    
    if reminders:
        reminders_list=parse_reminders(reminders)
    else:
        reminders_list=[30,14,7,3,1,0.125]
    filtered_reminders=[r for r in reminders_list if due-datetime.timedelta(days=r)>now]
    
    if due>now and 0 not in filtered_reminders: filtered_reminders.append(0)
    if not filtered_reminders: filtered_reminders=[0]
    
    filtered_reminders=sorted(filtered_reminders,reverse=True)
    
    channel_id = channel.id if channel else interaction.channel.id

    visible_ids = []
    if visible:
        visible_ids = [int(v.strip()) for v in visible.split(",")]

    role_ids = []
    if roles:
        role_ids = [int(r.strip()) for r in roles.split(",")]
    
    task={
        "task":task_name,
        "due":due,
        "channel_id":channel_id,
        "owner_id":interaction.user.id,
        "visible_to":visible_ids,
        "reminders":filtered_reminders,
        "notified":[],
        "mention":mention,
        "roles":role_ids,
        "status":"todo",
        "completed_by":None,
        "completed_at":None,
        "everyone": everyone,
    }
    
    tasks_list.append(task)

    print("④ DB接続前") # test
    

   # DB保存
    try:
        db, cursor = get_cursor()

        cursor.execute("""
            INSERT INTO tasks 
            (task, due, channel_id, owner_id, visible_to, roles, reminders, notified, mention, everyone, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            task_name,
            due,
            channel_id,
            interaction.user.id,
            json.dumps(visible_ids),
            json.dumps(role_ids),
            json.dumps(filtered_reminders),
            json.dumps([]),
            mention,
            everyone,
            "todo"
        ))

        db.commit()
        db.close()
        print("⑤ DB保存OK") # test
    
    except Exception as e:
        print(e)
        await interaction.followup.send("❌ DBエラー", ephemeral=True)
        return
    
    
    print("⑥ load_tasks前") # test
    # DBから再読み込み（超重要）
    await asyncio.to_thread(load_tasks)
    print("⑦ load_tasks後") # test
    print("⑧ 送信前") # test
    await interaction.followup.send(
        f"✅ タスク登録: {task_name}（期限: {due.strftime('%Y-%m-%d %H:%M')}）\n"
        f"リマインド: {', '.join([reminder_label(r) for r in filtered_reminders])}\n"
        f"見れる人: {', '.join([f'<@{uid}>' for uid in visible_ids])}\n"
        f"📢 チャンネル: <#{channel_id}>\n"
        f"💬 メンション: {'ON' if mention else 'OFF'}"
    )
    print("⑨ 完了") # test

# -----------------------
# /edit コマンド
# -----------------------
@tree.command(name="edit", description="タスク編集", guild=GUILD_OBJ)
@app_commands.describe(
    index="編集するタスク番号",
    task_name="新しいタスク名",
    date="MMDDまたはYYYYMMDD",
    time="HHMM",
    channel="通知チャンネル",
    mention="メンションON/OFF"
)

async def edit(
    interaction: discord.Interaction,
    index: int,
    task_name: str = None,
    date: str = None,
    time: str = None,
    channel: discord.TextChannel = None,
    mention: bool = None,
):
    
    now=datetime.datetime.now(JST)
    user=interaction.user
    await interaction.response.defer()  # 追加！！！
    
    visible_tasks=[t for t in tasks_list if can_view(t,user) and t.get("status")!="done"]
    if not (0<index<=len(visible_tasks)):
        await interaction.followup.send("❌ 無効な番号",ephemeral=True)
        return
    task=visible_tasks[index-1]
    if not can_edit(task,user):
        await interaction.followup.send("❌ 権限がありません",ephemeral=True)
        return
    if task_name: task["task"]=task_name
    current_due=task["due"]
    new_date_val=date or current_due.strftime("%Y%m%d")
    new_time_val=time or current_due.strftime("%H%M")
    
    if date and len(date)==3: new_date_val=date.zfill(4)
    if time and len(time)==3: new_time_val=time.zfill(4)
    
    try:
        if len(new_date_val)==4:
            year=now.year
            due=datetime.datetime.strptime(f"{year}{new_date_val} {new_time_val}","%Y%m%d %H%M").replace(tzinfo=JST)
            if due<now: due=due.replace(year=year+1)
        elif len(new_date_val)==8:
            due=datetime.datetime.strptime(f"{new_date_val} {new_time_val}","%Y%m%d %H%M").replace(tzinfo=JST)
        else:
            raise ValueError
    except:
        await interaction.followup.send("❌ 日付/時間形式が不正",ephemeral=True)
        return
    
    task["due"]=due
    task["notified"]=[]
    
    if mention is not None:
        task["mention"]=mention
    if channel:
        task["channel_id"]=channel.id

    

    # DB更新
    try:
        db, cursor = get_cursor()
        
        cursor.execute("""
        UPDATE tasks 
        SET task=%s, due=%s, channel_id=%s, mention=%s
        WHERE id=%s AND owner_id=%s
        """, (
            task["task"],
            task["due"],
            task["channel_id"],
            task["mention"],
            task["id"],
            task["owner_id"]
        ))

        db.commit()
        db.close()

    except Exception as e:
        print(e)
        await interaction.followup.send("❌ DBエラー", ephemeral=True)
        return

    # 更新処理
    await asyncio.to_thread(load_tasks)
    await interaction.followup.send(
        f"✅ タスク更新: {task['task']}（期限: {task['due'].strftime('%Y-%m-%d %H:%M')}）"
    )

# -----------------------
# /delete コマンド
# -----------------------
@tree.command(name="delete", description="タスク削除", guild=GUILD_OBJ)
@app_commands.describe(index="削除するタスク番号")
async def delete(interaction: discord.Interaction, index: int):

    await interaction.response.defer()  # 追加！！！
    user = interaction.user
    visible_tasks = [t for t in tasks_list if can_view(t, user) and t.get("status") != "done"]

    if not (0 < index <= len(visible_tasks)):
        await interaction.followup.send("❌ 無効な番号", ephemeral=True)
        return
    task = visible_tasks[index - 1]
    if not can_edit(task, user):
        await interaction.followup.send("❌ 権限がありません", ephemeral=True)
        return
    
    


    # DB保存
    try:
        db, cursor = get_cursor()

        cursor.execute("""
        DELETE FROM tasks 
        WHERE id=%s
        """, (task["id"],))

        db.commit()
        db.close()
        await asyncio.to_thread(load_tasks)

    except Exception as e:
        print(e)
        await interaction.followup.send("❌ DBエラー", ephemeral=True)
        return

    await interaction.followup.send(f"🗑️ 削除しました\n📌 {task['task']}")

# -----------------------
# /done コマンド
# -----------------------
@tree.command(name="done", description="タスクを完了にする", guild=GUILD_OBJ)
@app_commands.describe(index="完了するタスク番号")
async def done(interaction: discord.Interaction, index: int):

    await interaction.response.defer()  # 追加！！！
    user = interaction.user
    visible_tasks = [t for t in tasks_list if can_view(t, user) and t.get("status") != "done"]
    
    if not (0 < index <= len(visible_tasks)):
        await interaction.followup.send("❌ 無効な番号", ephemeral=True)
        return
    task = visible_tasks[index - 1]
    if not can_edit(task, user):
        await interaction.followup.send("❌ 権限がありません", ephemeral=True)
        return
    task["status"] = "done"
    task["completed_by"] = user.id
    task["completed_at"] = datetime.datetime.now(JST)


    # DB保存
    try:
        db, cursor = get_cursor()

        cursor.execute("""
        UPDATE tasks 
        SET status=%s, completed_by=%s, completed_at=%s
        WHERE id=%s
        """, (
            "done",
            user.id,
            datetime.datetime.now(JST),
            task["id"]
        ))

        db.commit()
        db.close()
        await asyncio.to_thread(load_tasks)

    except Exception as e:
        print(e)
        await interaction.followup.send("❌ DBエラー", ephemeral=True)
        return

    await interaction.followup.send(f"✅ 完了！\n📌 {task['task']}")

# -----------------------
# /history コマンド
# -----------------------
@tree.command(name="history", description="完了済みタスク一覧", guild=GUILD_OBJ)
async def history(interaction: discord.Interaction):

    await interaction.response.defer()  # 追加！！！
    user = interaction.user
    done_tasks = [t for t in tasks_list if can_view(t, user) and t.get("status") == "done"]

    if not done_tasks:
        await interaction.followup.send("📭 完了済みタスクなし")
        return
    msg="📜 完了履歴\n"
    for i,task in enumerate(done_tasks,start=1):
        completed_time = task["completed_at"] if task.get("completed_at") else None
        msg+=f"{i}. {task['task']}\n👤 完了者: <@{task.get('completed_by')}>\n📅 {completed_time.strftime('%m/%d %H:%M') if completed_time else '不明'}\n"
    await interaction.followup.send(msg)

# -----------------------
# /start コマンド
# -----------------------
@tree.command(name="start", description="タスクを開始", guild=GUILD_OBJ)
@app_commands.describe(index="開始するタスク番号")
async def start(interaction: discord.Interaction, index: int):

    await interaction.response.defer()  # 追加！！！
    user = interaction.user
    visible_tasks = [t for t in tasks_list if can_view(t, user) and t.get("status") != "done"]

    if not (0 < index <= len(visible_tasks)):
        await interaction.followup.send("❌ 無効な番号", ephemeral=True)
        return
    task = visible_tasks[index - 1]
    if not can_edit(task, user):
        await interaction.followup.send("❌ 権限がありません", ephemeral=True)
        return
    task["status"] = "doing"

    # DB保存
    try:
        db, cursor = get_cursor()

        cursor.execute("""
        UPDATE tasks SET status=%s WHERE id=%s
        """, ("doing", task["id"]))

        db.commit()
        db.close()
        await asyncio.to_thread(load_tasks)

    except Exception as e:
        print(e)
        await interaction.followup.send("❌ DBエラー", ephemeral=True)
        return
    
    await interaction.followup.send(f"🚀 進行中に変更！\n📌 {task['task']}")

# -----------------------
# リマインダー処理 check_tasks
# -----------------------
@tasks.loop(seconds=30)
async def check_tasks():
    now = datetime.datetime.now(JST)
    to_remove = []
    updated = False

    for task in tasks_list:
        if task.get("status") == "done":
            continue

        for r in task["reminders"]:
            if r in task["notified"]:
                continue

            reminder_time = task["due"] - datetime.timedelta(days=r)

            if reminder_time <= now < reminder_time + datetime.timedelta(seconds=30):
                channel = bot.get_channel(task["channel_id"])
                if not channel:
                    continue

                mention_text = ""

                if task.get("mention", False):
                    mentions = []

                    # 👇 everyone優先
                    if task.get("everyone", False):
                        mentions.append("@everyone")
                    else:
                        mentions += [f"<@{uid}>" for uid in task["visible_to"]]
                        mentions += [f"<@&{rid}>" for rid in task.get("roles", [])]

                    mention_text = " ".join(mentions)

                await channel.send(
                    f"{mention_text}\n⏰ {task['task']}\n🕒 {reminder_label(r)} / 期限: {task['due'].strftime('%m/%d %H:%M')}"
                )

                task["notified"].append(r)

                # DB保存
                try:
                    db, cursor = get_cursor()

                    cursor.execute("""
                    UPDATE tasks SET notified=%s
                    WHERE id=%s
                    """, (
                        json.dumps(task["notified"]),
                        task["id"]
                    ))

                    db.commit()
                    db.close()

                except Exception as e:
                    print(e)



# -----------------------
# 起動時
# -----------------------
@bot.event
async def on_ready():
    print("🚀 on_ready開始")

    await asyncio.to_thread(load_tasks)

    await tree.sync()  # ← 強制グローバル同期
    print("🌍 グローバルコマンド同期")

    print(f"{bot.user} が起動しました！")
    check_tasks.start()

# セキュリティチェック
SECRET = os.environ.get("SECRET", "mypassword")

# @app.before_request
# def check_auth():
    # open_paths = ["/", "/add_web", "/done_web", "/delete_web"]

    # if request.path not in open_paths:
    #     if request.args.get("key") != SECRET:
    #         return "Unauthorized", 403
        

# -----------------------
# 起動（←一番最後に置く）
# -----------------------

def start_bot():
    asyncio.run(bot.start(os.environ.get("TOKEN")))

if os.environ.get("PORT"):  # ← Koyebで確実にある
    threading.Thread(target=start_bot, daemon=True).start()