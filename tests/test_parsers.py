import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot


class FixedDateTime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        base = cls(2026, 4, 2, 12, 0, 0)
        if tz is not None:
            return base.replace(tzinfo=tz)
        return base


class ParseDateTimeInputTests(unittest.TestCase):
    @patch("bot.datetime.datetime", FixedDateTime)
    def test_empty_input_defaults_to_tomorrow_midnight(self):
        due = bot.parse_datetime_input("")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 3, 0, 0))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_time_only_rolls_to_next_day_when_in_past(self):
        due = bot.parse_datetime_input("310")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 3, 3, 10))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_month_day_rolls_to_next_year_when_in_past(self):
        due = bot.parse_datetime_input("411000")
        self.assertEqual((due.year, due.month, due.day, due.hour, due.minute), (2027, 4, 1, 10, 0))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_tomorrow_defaults_to_midnight(self):
        due = bot.parse_datetime_input("明日")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 3, 0, 0))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_tomorrow_with_time_uses_next_day_absolute_time(self):
        due = bot.parse_datetime_input("明日 824")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 3, 8, 24))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_slash_date_parses_absolute_date(self):
        due = bot.parse_datetime_input("4/6 10:15")
        self.assertEqual((due.year, due.month, due.day, due.hour, due.minute), (2026, 4, 6, 10, 15))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_relative_hours_parses_from_now(self):
        due = bot.parse_datetime_input("3時間後")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 2, 15, 0))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_relative_days_default_to_midnight(self):
        due = bot.parse_datetime_input("2日後")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 4, 0, 0))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_relative_days_without_after_suffix_default_to_midnight(self):
        due = bot.parse_datetime_input("2日")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 4, 0, 0))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_relative_days_with_compact_time(self):
        due = bot.parse_datetime_input("2日後 1830")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 4, 18, 30))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_relative_days_with_now_alias_uses_current_time(self):
        due = bot.parse_datetime_input("1日後 now")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 3, 12, 0))

    @patch("bot.datetime.datetime", FixedDateTime)
    def test_relative_days_with_hiragana_now_alias_uses_current_time(self):
        due = bot.parse_datetime_input("1日 いま")
        self.assertEqual((due.month, due.day, due.hour, due.minute), (4, 3, 12, 0))

    def test_now_alone_is_invalid(self):
        with self.assertRaises(ValueError):
            bot.parse_datetime_input("now")

    def test_invalid_non_numeric_input(self):
        with self.assertRaises(ValueError):
            bot.parse_datetime_input("ab")


class ParseRemindersTests(unittest.TestCase):
    def test_parse_reminders(self):
        parsed = bot.parse_reminders("1d,3h")
        self.assertEqual(parsed, [("1day", 1), ("3hour", 3 / 24)])

    def test_parse_multi_character_units(self):
        parsed = bot.parse_reminders("10m,5h,1mo,2y")
        self.assertEqual(parsed[0][0], "10minute")
        self.assertAlmostEqual(parsed[0][1], 10 / 1440)
        self.assertEqual(parsed[1][0], "5hour")
        self.assertAlmostEqual(parsed[1][1], 5 / 24)
        self.assertEqual(parsed[2], ("1month", 30))
        self.assertEqual(parsed[3], ("2year", 730))

    def test_parse_reminders_with_def_expands_defaults(self):
        parsed = bot.parse_reminders("def,3h")
        self.assertEqual(
            parsed,
            [
                ("1month", 30),
                ("2week", 14),
                ("1week", 7),
                ("3day", 3),
                ("24hour", 1),
                ("3hour", 3 / 24),
            ],
        )


class ParseLoopIntervalTests(unittest.TestCase):
    def test_parse_loop_interval_aliases_and_short_units(self):
        self.assertEqual(bot.parse_loop_interval("1d"), 1440)
        self.assertEqual(bot.parse_loop_interval("毎日"), 1440)
        self.assertEqual(bot.parse_loop_interval("2d"), 2880)
        self.assertEqual(bot.parse_loop_interval("1w"), 10080)
        self.assertEqual(bot.parse_loop_interval("毎週"), 10080)
        self.assertEqual(bot.parse_loop_interval("1h"), 60)

    def test_loop_interval_to_text(self):
        self.assertEqual(bot.loop_interval_to_text(1440), "毎日")
        self.assertEqual(bot.loop_interval_to_text(2880), "2日おき")
        self.assertEqual(bot.loop_interval_to_text(10080), "1週間おき")
        self.assertEqual(bot.loop_interval_to_text(60), "1時間おき")


class ParseTaskIdsTests(unittest.TestCase):
    def test_parse_task_ids(self):
        self.assertEqual(bot.parse_task_ids("1,2, 3"), [1, 2, 3])

    def test_parse_task_ids_rejects_invalid(self):
        with self.assertRaises(ValueError):
            bot.parse_task_ids("1,a")


class TaskChoiceFormattingTests(unittest.TestCase):
    def test_format_task_choice_name(self):
        task = {
            "id": 12,
            "task": "sample",
            "status": "todo",
            "category": "composer",
            "due": datetime.datetime(2026, 4, 3, 9, 30),
        }
        self.assertEqual(bot.format_task_choice_name(task), "[12] sample (todo / composer / 04/03 09:30)")

    def test_filter_task_choices_matches_id_and_name(self):
        tasks = [
            {"id": 12, "task": "sample"},
            {"id": 34, "task": "review"},
        ]
        self.assertEqual(bot.filter_task_choices(tasks, "12")[0]["id"], 12)
        self.assertEqual(bot.filter_task_choices(tasks, "rev")[0]["id"], 34)


class FilteredTasksTests(unittest.TestCase):
    def test_get_filtered_tasks_for_user_respects_channel_status_and_mine_only(self):
        original = bot.tasks_list
        try:
            bot.tasks_list = [
                {"id": 1, "task": "a", "guild_id": 100, "channel_id": 10, "owner_id": 5, "status": "todo", "category": "composer", "due": datetime.datetime(2026, 4, 3, 9, 0)},
                {"id": 2, "task": "b", "guild_id": 100, "channel_id": 10, "owner_id": 6, "status": "done", "category": "general", "due": datetime.datetime(2026, 4, 4, 9, 0)},
                {"id": 3, "task": "c", "guild_id": 100, "channel_id": 11, "owner_id": 5, "status": "todo", "category": "vocal", "due": datetime.datetime(2026, 4, 5, 9, 0)},
            ]
            tasks = bot.get_filtered_tasks_for_user(100, 5, False, channel_id=10, status="todo", mine_only=False)
            self.assertEqual([task["id"] for task in tasks], [1])
        finally:
            bot.tasks_list = original


class SortTasksTests(unittest.TestCase):
    def test_sort_tasks_by_task_desc(self):
        tasks = [
            {"id": 1, "task": "alpha", "status": "todo", "category": "general", "due": datetime.datetime(2026, 4, 3, 9, 0)},
            {"id": 2, "task": "gamma", "status": "todo", "category": "general", "due": datetime.datetime(2026, 4, 3, 8, 0)},
            {"id": 3, "task": "beta", "status": "todo", "category": "general", "due": datetime.datetime(2026, 4, 3, 7, 0)},
        ]
        sorted_tasks = bot.sort_tasks(tasks, "task", "desc")
        self.assertEqual([task["id"] for task in sorted_tasks], [2, 3, 1])

    def test_sort_tasks_by_category(self):
        tasks = [
            {"id": 1, "task": "a", "status": "todo", "category": "vocal", "due": datetime.datetime(2026, 4, 3, 9, 0)},
            {"id": 2, "task": "b", "status": "todo", "category": "composer", "due": datetime.datetime(2026, 4, 3, 8, 0)},
        ]
        sorted_tasks = bot.sort_tasks(tasks, "category", "asc")
        self.assertEqual([task["id"] for task in sorted_tasks], [2, 1])


class TaskGuildValidationTests(unittest.TestCase):
    def test_is_valid_task_guild_rejects_zero(self):
        task = {"id": 1, "guild_id": 0, "task": "ghost"}
        self.assertFalse(bot.is_valid_task_guild(task))

    def test_get_filtered_tasks_for_user_allows_manager_scope(self):
        original = bot.tasks_list
        try:
            bot.tasks_list = [
                {"id": 1, "task": "a", "guild_id": 100, "channel_id": 10, "owner_id": 5, "status": "todo", "category": "composer", "due": datetime.datetime(2026, 4, 3, 9, 0)},
                {"id": 2, "task": "b", "guild_id": 100, "channel_id": 10, "owner_id": 6, "status": "todo", "category": "vocal", "due": datetime.datetime(2026, 4, 4, 9, 0)},
            ]
            tasks = bot.get_filtered_tasks_for_user(100, 5, True, channel_id=10, status="todo", mine_only=False)
            self.assertEqual([task["id"] for task in tasks], [1, 2])
        finally:
            bot.tasks_list = original

    def test_get_filtered_tasks_for_user_respects_category(self):
        original = bot.tasks_list
        try:
            bot.tasks_list = [
                {"id": 1, "task": "a", "guild_id": 100, "channel_id": 10, "owner_id": 5, "status": "todo", "category": "composer", "due": datetime.datetime(2026, 4, 3, 9, 0)},
                {"id": 2, "task": "b", "guild_id": 100, "channel_id": 10, "owner_id": 5, "status": "todo", "category": "general", "due": datetime.datetime(2026, 4, 4, 9, 0)},
            ]
            tasks = bot.get_filtered_tasks_for_user(100, 5, True, channel_id=10, status="todo", mine_only=False, category="composer")
            self.assertEqual([task["id"] for task in tasks], [1])
        finally:
            bot.tasks_list = original


class NotificationRoutingTests(unittest.TestCase):
    @patch("bot.get_guild_settings", return_value={"notify_channel_id": 200, "manager_role_id": 300})
    def test_resolve_notification_channel_prefers_task_channel(self, _mock_settings):
        task = {"notify_channel_id": 100, "channel_id": 10, "guild_id": 1}
        self.assertEqual(bot.resolve_notification_channel_id(task), 100)

    @patch("bot.get_guild_settings", return_value={"notify_channel_id": 200, "manager_role_id": 300})
    def test_resolve_notification_channel_falls_back_to_guild(self, _mock_settings):
        task = {"notify_channel_id": None, "channel_id": 10, "guild_id": 1}
        self.assertEqual(bot.resolve_notification_channel_id(task), 200)

    @patch("bot.get_guild_settings", return_value={"notify_channel_id": 200, "manager_role_id": 300})
    def test_build_manager_mention_only_for_guild_notify(self, _mock_settings):
        task = {"notify_channel_id": None, "channel_id": 10, "guild_id": 1}
        self.assertEqual(bot.build_manager_mention(task), "<@&300> ")

    @patch("bot.get_guild_settings", return_value={"notify_channel_id": 200, "manager_role_id": 300})
    def test_build_manager_mention_skips_task_specific_channel(self, _mock_settings):
        task = {"notify_channel_id": 100, "channel_id": 10, "guild_id": 1}
        self.assertEqual(bot.build_manager_mention(task), "")


class LabelToTextTests(unittest.TestCase):
    def test_label_to_text_supports_minutes_and_existing_months(self):
        self.assertEqual(bot.label_to_text("10minute"), "10分前")
        self.assertEqual(bot.label_to_text("1month"), "1ヶ月前")


class DeleteLogFormattingTests(unittest.TestCase):
    def test_format_delete_log_message(self):
        tasks = [
            {"id": 7, "task": "review", "status": "done", "due": datetime.datetime(2026, 4, 3, 10, 0)},
            {"id": 8, "task": "meeting", "status": "todo", "due": datetime.datetime(2026, 4, 4, 11, 30)},
        ]
        self.assertEqual(
            bot.format_delete_log_message("alice", tasks),
            "🗑【削除】\n実行者: alice\n2件削除\n[7] review (done)\n[8] meeting (todo)",
        )

    def test_format_delete_log_message_with_target_owner(self):
        tasks = [
            {"id": 7, "task": "review", "status": "done", "due": datetime.datetime(2026, 4, 3, 10, 0)},
        ]
        self.assertEqual(
            bot.format_delete_log_message("alice", tasks, target_owner_name="bob"),
            "🗑【削除】\n実行者: alice\n対象: bob\n1件削除\n[7] review (done)",
        )

    def test_format_done_log_message(self):
        tasks = [
            {"id": 7, "task": "review", "due": datetime.datetime(2026, 4, 3, 10, 0)},
        ]
        self.assertEqual(
            bot.format_done_log_message("alice", tasks),
            "✅【完了】\n実行者: alice\n1件完了\n[7] review (04/03 10:00)",
        )

    def test_format_status_bulk_log_message(self):
        tasks = [
            {"id": 12, "task": "taskA"},
            {"id": 15, "task": "taskB"},
            {"id": 18, "task": "taskC"},
            {"id": 19, "task": "taskD"},
            {"id": 20, "task": "taskE"},
            {"id": 21, "task": "taskF"},
        ]
        self.assertEqual(
            bot.format_status_bulk_log_message("@alice", "@bob", tasks, "done"),
            "🔄 一括ステータス更新\n実行者: @alice\n対象: @bob\n件数: 6件\n→ done\n[12] taskA\n[15] taskB\n[18] taskC\n[19] taskD\n[20] taskE\nその他1件...",
        )


class ManagerMemberTests(unittest.TestCase):
    def test_is_manager_member_true_when_role_matches(self):
        member = SimpleNamespace(roles=[SimpleNamespace(id=10), SimpleNamespace(id=20)])
        settings = {"manager_role_id": 20}
        self.assertTrue(bot.is_manager_member(member, settings))

    def test_is_manager_member_false_without_member(self):
        settings = {"manager_role_id": 20}
        self.assertFalse(bot.is_manager_member(None, settings))


if __name__ == "__main__":
    unittest.main()
