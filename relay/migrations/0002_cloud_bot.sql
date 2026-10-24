-- Separate tables leave the original outbound relay usable until explicit cutover.
CREATE TABLE cloud_snapshot (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    dataset_id TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    generated_ms INTEGER NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE cloud_tasks (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('open','waiting','done','dismissed')),
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE cloud_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    task_ids TEXT NOT NULL DEFAULT '[]',
    completed_ids TEXT NOT NULL DEFAULT '[]',
    retry_limit INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL CHECK(status IN ('pending','done','failed')),
    created_at TEXT NOT NULL,
    error_category TEXT
);
CREATE INDEX cloud_events_pending ON cloud_events(status,id);
-- Do not send a second digest on the day an existing relay already delivered it.
INSERT INTO cloud_events(dedupe_key,action,status,created_at)
    SELECT 'digest:' || delivery_date,'digest','done',created_at
    FROM deliveries WHERE status IN ('delivered','no_data');
CREATE TABLE cloud_outbox (
    id TEXT PRIMARY KEY,
    sealed_chunks TEXT NOT NULL,
    next_chunk INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX cloud_outbox_pending ON cloud_outbox(delivered_at,created_at);
CREATE TABLE cloud_lock (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    owner TEXT,
    expires_ms INTEGER NOT NULL DEFAULT 0
);
INSERT INTO cloud_lock(singleton) VALUES(1);
