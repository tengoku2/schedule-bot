ALTER TABLE tasks
  ADD COLUMN category VARCHAR(32) NOT NULL DEFAULT 'general'
  AFTER status;
