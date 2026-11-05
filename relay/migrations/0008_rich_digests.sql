ALTER TABLE interactive_digests ADD COLUMN format TEXT NOT NULL DEFAULT 'legacy' CHECK (format IN ('legacy', 'rich'));
CREATE TABLE interactive_digest_parts (
    digest_id TEXT NOT NULL REFERENCES interactive_digests(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (digest_id, part_index)
);
