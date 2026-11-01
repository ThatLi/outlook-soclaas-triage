ALTER TABLE interactive_digests ADD COLUMN sealed_layout TEXT;
ALTER TABLE interactive_digests ADD COLUMN task_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE interactive_digests ADD COLUMN feedback_event INTEGER;
ALTER TABLE interactive_digests ADD COLUMN feedback_text TEXT NOT NULL DEFAULT '';
CREATE TABLE cloud_task_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES cloud_events(id),
    command_message_id INTEGER,
    expires_at INTEGER,
    allow_fallback INTEGER NOT NULL DEFAULT 1,
    fallback_sent INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX cloud_task_feedback_pending ON cloud_task_feedback(delivered_at,id);
