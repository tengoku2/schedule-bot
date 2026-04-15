ALTER TABLE tasks
  ADD COLUMN loop_remind_start DATETIME DEFAULT NULL AFTER category,
  ADD COLUMN loop_remind_interval_minutes INT DEFAULT NULL AFTER loop_remind_start,
  ADD COLUMN loop_remind_last_sent_at DATETIME DEFAULT NULL AFTER loop_remind_interval_minutes;
