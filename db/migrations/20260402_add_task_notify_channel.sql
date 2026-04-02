ALTER TABLE tasks
ADD COLUMN notify_channel_id BIGINT NULL AFTER channel_id;
