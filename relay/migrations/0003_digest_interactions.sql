-- Frozen digest layouts and Telegram IDs let task commands edit existing messages.
-- Content remains encrypted; only task membership and delivery metadata are clear.
CREATE TABLE cloud_digests (
    id TEXT PRIMARY KEY,
    sealed_chunks TEXT NOT NULL,
    groups_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE cloud_digest_messages (
    digest_id TEXT NOT NULL REFERENCES cloud_digests(id),
    chunk_index INTEGER NOT NULL,
    message_id INTEGER,
    unavailable INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(digest_id, chunk_index)
);
ALTER TABLE cloud_outbox ADD COLUMN fallback_sent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cloud_outbox ADD COLUMN retry_after_ms INTEGER NOT NULL DEFAULT 0;
