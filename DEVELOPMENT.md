# Local Validation Setup

## 1. Python environment

```powershell
.\scripts\dev_setup.ps1
.\.venv\Scripts\Activate.ps1
```

## 2. Configure `.env`

`.env.example` is copied to `.env` on first setup.

Set these values:

- `TOKEN`: Discord bot token for real bot startup
- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASS`, `DB_NAME`: local or remote MySQL
- `GUILD_ID`: test guild id

## 3. Prepare MySQL schema

Run `db/schema.sql` against your MySQL server.

If you already have an existing database, also apply the migration in
`db/migrations/20260402_add_task_notify_channel.sql`,
`db/migrations/20260404_add_task_priority_and_updated_at.sql`,
`db/migrations/20260405_add_task_category.sql`,
and `db/migrations/20260415_add_loop_reminder_columns.sql`.

Example:

```sql
SOURCE db/schema.sql;
```

## 4. Validate without Discord login

```powershell
python scripts/check_env.py
pytest
```

`check_env.py` verifies:

- `.env` loading
- `bot.py` import
- datetime/reminder parser behavior
- MySQL connection
- required tables existence
- required column existence, including `tasks.notify_channel_id`
- required column existence, including `tasks.category`
- required column existence, including loop reminder columns

## 5. Start the bot

```powershell
python bot.py
```

## Notes

- Docker is not available in the current workstation, so the local validation path is based on Python venv plus MySQL.
- The app still requires a real Discord token and reachable MySQL for end-to-end command testing.
