CREATE DATABASE IF NOT EXISTS schedule_bot
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE schedule_bot;

CREATE TABLE IF NOT EXISTS tasks (
  id BIGINT NOT NULL AUTO_INCREMENT,
  task TEXT NOT NULL,
  due DATETIME NOT NULL,
  channel_id BIGINT NOT NULL,
  notify_channel_id BIGINT DEFAULT NULL,
  owner_id BIGINT NOT NULL,
  visible_to JSON DEFAULT NULL,
  roles JSON DEFAULT NULL,
  reminders JSON DEFAULT NULL,
  notified JSON DEFAULT NULL,
  mention BOOLEAN NOT NULL DEFAULT FALSE,
  everyone BOOLEAN NOT NULL DEFAULT FALSE,
  status VARCHAR(16) NOT NULL DEFAULT 'todo',
  category VARCHAR(32) NOT NULL DEFAULT 'general',
  loop_remind_start DATETIME DEFAULT NULL,
  loop_remind_interval_minutes INT DEFAULT NULL,
  loop_remind_last_sent_at DATETIME DEFAULT NULL,
  priority VARCHAR(10) DEFAULT NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  guild_id BIGINT NOT NULL,
  PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id BIGINT NOT NULL,
  manager_role_id BIGINT DEFAULT NULL,
  notify_channel_id BIGINT DEFAULT NULL,
  PRIMARY KEY (guild_id)
);
