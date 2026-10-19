CREATE TABLE bundles (
    bundle_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    intended_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    last_sync_at TEXT,
    nonce TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE INDEX idx_bundles_generated ON bundles(generated_at DESC, received_at DESC);

CREATE TABLE deliveries (
    delivery_date TEXT PRIMARY KEY,
    bundle_id TEXT REFERENCES bundles(bundle_id),
    next_chunk INTEGER NOT NULL DEFAULT 0,
    total_chunks INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'no_data')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error TEXT
);
CREATE INDEX idx_deliveries_status ON deliveries(status, delivery_date);
