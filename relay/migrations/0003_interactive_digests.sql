CREATE TABLE interactive_digests (
    id TEXT PRIMARY KEY,
    digest_date TEXT NOT NULL,
    message_id INTEGER,
    view TEXT NOT NULL DEFAULT 'overview',
    page INTEGER NOT NULL DEFAULT 0,
    sealed_pages TEXT NOT NULL,
    dirty INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX interactive_digests_refresh ON interactive_digests(dirty,disabled,retry_at);
ALTER TABLE cloud_outbox ADD COLUMN digest_id TEXT REFERENCES interactive_digests(id);
ALTER TABLE cloud_events ADD COLUMN digest_id TEXT REFERENCES interactive_digests(id);
ALTER TABLE cloud_lock ADD COLUMN telegram_retry_at INTEGER NOT NULL DEFAULT 0;
