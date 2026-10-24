import { afterEach, describe, expect, it, vi } from "vitest";

import worker, {
  decryptEnvelope, runScheduled, singaporeClock, validateEnvelope,
  type BundleEnvelope, type Env,
} from "../src/index";

// Public test vector shared with Python's bytes(range(32)), not a deployed key.
const keyBytes = Uint8Array.from({ length: 32 }, (_, i) => i);
const key = btoa(String.fromCharCode(...keyBytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const pythonFixture: BundleEnvelope = {
  schema_version: 1,
  bundle_id: "9c36de71-c074-44f3-bc5e-07e9489c5147",
  intended_date: "2026-10-11",
  generated_at: "2026-10-10T23:50:00+08:00",
  last_sync_at: "2026-10-10T23:49:58+08:00",
  nonce: "AAECAwQFBgcICQoL",
  ciphertext: "PCC1eqaNp3_SIvjm3IgWCfD0vU_SFzYPTEXf3j9VYoxnedaI2rN3uBDNGIj78xQXjGdC0Hb00K5e419qOtmuzINIpw6mogQ8MHbeD5zkfcoC8esDGg9xUqagsu4cmcrgoosqg8Bj8kHDMBzWJxtEXYA8P4fBqlX_I91tlt_KTHnaO3Sv2rDpaVO8NdwR-CirUZGQQWpy-z_iNJtVwInL0vGp-HyRi7kMwkO5k7jl9c-h4agcqfZfnha8qKIcpOiFdj75k2Kz9PMGK-Sa7-TjBXeobBlP8CJTYBI",
};

type Row = Record<string, unknown>;

class MemoryStatement {
  args: unknown[] = [];
  constructor(private database: MemoryD1, private sql: string) {}
  bind(...args: unknown[]) { this.args = args; return this; }
  async first<T = Row>(): Promise<T | null> { return this.database.first(this.sql, this.args) as T | null; }
  async run() { this.database.run(this.sql, this.args); return { success: true }; }
}

class MemoryD1 {
  bundles = new Map<string, Row>();
  deliveries = new Map<string, Row>();
  prepare(sql: string) { return new MemoryStatement(this, sql); }
  async batch(statements: MemoryStatement[]) { for (const statement of statements) await statement.run(); return []; }

  first(sql: string, args: unknown[]): Row | null {
    if (sql.includes("FROM bundles WHERE bundle_id=?")) return this.bundles.get(String(args[0])) ?? null;
    if (sql.includes("FROM deliveries WHERE delivery_date=?")) return this.deliveries.get(String(args[0])) ?? null;
    if (sql.includes("FROM deliveries WHERE status='pending'")) {
      return [...this.deliveries.values()]
        .filter((row) => row.status === "pending")
        .sort((a, b) => String(a.delivery_date).localeCompare(String(b.delivery_date)))[0] ?? null;
    }
    if (sql.includes("FROM bundles ORDER BY generated_at DESC")) {
      const rows = [...this.bundles.values()].sort((a, b) => String(b.generated_at).localeCompare(String(a.generated_at)));
      return rows[0] ?? null;
    }
    throw new Error(`Unhandled first SQL: ${sql}`);
  }

  run(sql: string, args: unknown[]) {
    if (sql.includes("INSERT INTO bundles")) {
      const [bundle_id, schema_version, intended_date, generated_at, last_sync_at, nonce, ciphertext, received_at] = args;
      this.bundles.set(String(bundle_id), { bundle_id, schema_version, intended_date, generated_at, last_sync_at, nonce, ciphertext, received_at });
      return;
    }
    if (sql.includes("INSERT INTO deliveries")) {
      const [delivery_date, bundle_id, total_chunks, created_at, updated_at] = args;
      this.deliveries.set(String(delivery_date), {
        delivery_date, bundle_id, next_chunk: 0, total_chunks, status: "pending",
        created_at, updated_at, delivered_at: null, last_error: null,
      });
      return;
    }
    const delivery = this.deliveries.get(String(args.at(-1)));
    if (sql.includes("SET next_chunk=")) Object.assign(delivery!, { next_chunk: args[0], updated_at: args[1], last_error: null });
    else if (sql.includes("SET status=")) Object.assign(delivery!, { status: args[0], delivered_at: args[1], updated_at: args[2], last_error: null });
    else if (sql.includes("SET updated_at=") && sql.includes("last_error=")) Object.assign(delivery!, { updated_at: args[0], last_error: args[1] });
    else if (sql.startsWith("DELETE FROM")) return;
    else throw new Error(`Unhandled run SQL: ${sql}`);
  }
}

function environment(database = new MemoryD1()): Env {
  return {
    DB: database as unknown as D1Database,
    RELAY_TOKEN: "upload-token",
    RELAY_ENCRYPTION_KEY: key,
    TELEGRAM_BOT_TOKEN: "bot-token",
    TELEGRAM_CHAT_ID: "7",
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("relay contracts", () => {
  it("calculates the Singapore delivery day independently of host timezone", () => {
    expect(singaporeClock(new Date("2026-10-10T23:59:00Z"))).toEqual({ date: "2026-10-11", hour: 7 });
    expect(singaporeClock(new Date("2026-10-11T00:00:00Z"))).toEqual({ date: "2026-10-11", hour: 8 });
  });

  it("rejects malformed envelopes", () => {
    expect(() => validateEnvelope({ schema_version: 1 })).toThrow(/bundle_id/);
    expect(() => validateEnvelope({ ...pythonFixture, nonce: "AA" })).toThrow(/nonce/);
  });

  it("decrypts the committed Python interoperability fixture", async () => {
    const payload = await decryptEnvelope(pythonFixture, key);
    expect(payload.digest_chunks).toEqual(["<b>fixture digest</b>"]);
  });

  it("authenticates uploads, stores only ciphertext, and is idempotent", async () => {
    const database = new MemoryD1();
    const env = environment(database);
    const unauthorized = await worker.fetch(new Request("https://relay.test/v1/bundles", { method: "POST" }), env);
    expect(unauthorized.status).toBe(401);

    const request = () => new Request("https://relay.test/v1/bundles", {
      method: "POST",
      headers: { authorization: "Bearer upload-token", "content-type": "application/json" },
      body: JSON.stringify(pythonFixture),
    });
    expect((await worker.fetch(request(), env)).status).toBe(201);
    expect((await worker.fetch(request(), env)).status).toBe(200);
    expect(JSON.stringify([...database.bundles.values()])).not.toContain("fixture digest");
  });

  it("freezes a bundle, resumes after a partial Telegram failure, and completes once", async () => {
    const database = new MemoryD1();
    database.bundles.set(pythonFixture.bundle_id, { ...pythonFixture, received_at: "2026-10-10T16:00:00Z" });
    const env = environment(database);
    let calls = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      calls += 1;
      return calls === 2
        ? new Response(JSON.stringify({ ok: false }), { status: 503 })
        : Response.json({ ok: true });
    }));

    await expect(runScheduled(env, new Date("2026-10-11T00:00:00Z"))).rejects.toThrow(/Telegram/);
    expect(database.deliveries.get("2026-10-11")?.next_chunk).toBe(1);
    expect(database.deliveries.get("2026-10-11")?.status).toBe("pending");

    await runScheduled(env, new Date("2026-10-11T00:30:00Z"));
    expect(database.deliveries.get("2026-10-11")?.status).toBe("delivered");
    const completedCalls = calls;
    await runScheduled(env, new Date("2026-10-11T01:00:00Z"));
    expect(calls).toBe(completedCalls);
  });

  it("sends and records one no-data notice when no snapshot exists", async () => {
    const database = new MemoryD1();
    const env = environment(database);
    const send = vi.fn(async () => Response.json({ ok: true }));
    vi.stubGlobal("fetch", send);
    await runScheduled(env, new Date("2026-10-11T00:00:00Z"));
    expect(send).toHaveBeenCalledOnce();
    expect(database.deliveries.get("2026-10-11")?.status).toBe("no_data");
  });
});
