import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import worker, { type Env } from "../src/index";
import { parseCloudCommand, renderDigest, runCloudScheduled, textChunks, type BridgeSnapshot } from "../src/cloud";

// Run production SQL against SQLite rather than matching SQL strings in a mock.
class SQLiteD1 {
  db = new DatabaseSync(":memory:");
  constructor() {
    this.db.exec(readFileSync(new URL("../migrations/0001_initial.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0002_cloud_bot.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0003_digest_interactions.sql", import.meta.url), "utf8"));
  }
  prepare(sql: string) {
    let args: (string | number | null)[] = [];
    const statement = {
      bind(...values: (string | number | null)[]) { args = values; return statement; },
      first: async <T>() => (this.db.prepare(sql).get(...args) ?? null) as T | null,
      all: async () => ({ results: this.db.prepare(sql).all(...args), success: true }),
      run: async () => ({ meta: this.db.prepare(sql).run(...args), success: true }),
    };
    return statement;
  }
  async batch(statements: { run: () => Promise<unknown> }[]) {
    this.db.exec("BEGIN");
    try {
      const results = [];
      for (const statement of statements) results.push(await statement.run());
      this.db.exec("COMMIT");
      return results;
    } catch (error) { this.db.exec("ROLLBACK"); throw error; }
  }
}

const dataset = "00000000-0000-4000-8000-000000000001";
// Public test vector shared with Python's bytes(range(32)), not a deployed key.
const keyBytes = Uint8Array.from({ length: 32 }, (_, i) => i);
const b64 = (bytes: Uint8Array) => btoa(Array.from(bytes, b => String.fromCharCode(b)).join(""))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const encodedKey = b64(keyBytes);
function fixture(): BridgeSnapshot {
  return {
    dataset_id: dataset, horizon_days: 7,
    health: { last_sync: "2026-10-20T20:00:00+08:00", last_run: { status: "success" }, pending_or_failed: 0 },
    counts: { information_count: 3 },
    tasks: [1, 2].map(id => ({
      id, status: "open", description: `Private task ${id} <review>`, deadline: "2026-10-22",
      urgency: "normal", sender_name: "Example", sender_address: "", subject: "Private subject",
    })),
  };
}
async function envelope(data: BridgeSnapshot) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const metadata = {
    bundle_id: crypto.randomUUID(), generated_at: new Date().toISOString(), intended_date: "2026-10-21",
    last_sync_at: data.health.last_sync, nonce: b64(iv), schema_version: 1,
  };
  const payload = { schema_version: 1, digest_chunks: ["digest"], bridge: data,
    cached_commands: { list: [], status: [], tasks: Object.fromEntries(["all", "open", "waiting", "done", "dismissed"].map(s => [s, []])) } };
  const key = await crypto.subtle.importKey("raw", keyBytes, "AES-GCM", false, ["encrypt"]);
  const encrypted = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: new TextEncoder().encode(JSON.stringify(metadata)) },
    key, new TextEncoder().encode(JSON.stringify(payload)));
  return { ...metadata, ciphertext: b64(new Uint8Array(encrypted)) };
}

let database: SQLiteD1;
let env: Env;
let sent: string[];
let sequence: number;
let requests: { method: string; body: Record<string, unknown> }[];
let messages: Map<number, string>;
let nextMessage: number;
function telegramOk(url: string | URL | Request, init?: RequestInit) {
  const method = String(url).split("/").at(-1)!;
  const body = JSON.parse(String(init?.body));
  requests.push({ method, body });
  if (method === "sendMessage") {
    sent.push(body.text);
    const message_id = ++nextMessage;
    messages.set(message_id, body.text);
    return Response.json({ ok: true, result: { message_id } });
  }
  if (method === "editMessageText") messages.set(body.message_id, body.text);
  return Response.json({ ok: true, result: true });
}
async function drain() {
  for (let i = 0; i < 100; i++) {
    if (!database.db.prepare("SELECT id FROM cloud_outbox WHERE delivered_at IS NULL").get()) return;
    await poll();
  }
  throw new Error("Outbox did not drain");
}
async function digest() { await command("/list"); await drain(); }
function displayed() { return [...messages.values()].join("\n"); }
async function publish(data = fixture()) {
  return worker.fetch(new Request("https://cloud.test/v2/snapshot", {
    method: "POST", headers: { authorization: "Bearer relay" }, body: JSON.stringify(await envelope(data)),
  }), env);
}
async function command(text: string, id = ++sequence, overrides = {}) {
  return worker.fetch(new Request("https://cloud.test/telegram/webhook", {
    method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "webhook" },
    body: JSON.stringify({ update_id: id, message: { message_id: id, date: Math.floor(Date.now() / 1000), chat: { id: 7, type: "private" }, from: { is_bot: false }, text, ...overrides } }),
  }), env);
}
async function poll() {
  const response = await worker.fetch(new Request("https://cloud.test/v2/bridge/poll", {
    headers: { authorization: "Bearer relay", "X-Bridge-Dataset": dataset },
  }), env);
  return response.json() as Promise<{ job: { id: number; task_ids: number[]; completed_ids: number[]; status: string } | null }>;
}
async function ack(body: object) {
  return worker.fetch(new Request("https://cloud.test/v2/bridge/ack", {
    method: "POST", headers: { authorization: "Bearer relay", "X-Bridge-Dataset": dataset }, body: JSON.stringify(body),
  }), env);
}
function state(id: number) { return database.db.prepare("SELECT * FROM cloud_tasks WHERE id=?").get(id); }

beforeEach(async () => {
  database = new SQLiteD1(); sent = []; sequence = 100; requests = []; messages = new Map(); nextMessage = 1000;
  env = { DB: database as unknown as D1Database, RELAY_TOKEN: "relay", RELAY_ENCRYPTION_KEY: encodedKey,
    TELEGRAM_BOT_TOKEN: "bot", TELEGRAM_CHAT_ID: "7", TELEGRAM_WEBHOOK_SECRET: "webhook", CLOUD_BOT_ENABLED: "true" };
  vi.stubGlobal("fetch", vi.fn(async (url, init) => telegramOk(url, init)));
  expect((await publish()).status).toBe(200);
});
afterEach(() => { database.db.close(); vi.unstubAllGlobals(); });

describe("cloud bot with real SQLite", () => {
  it("does not replay commands already processed by the local listener at cutover", async () => {
    const data = fixture(); data.processed_telegram_updates = [42];
    await publish(data);
    await command("/waiting #1", 42);
    expect(state(1)?.status).toBe("open");
    expect(sent).toHaveLength(0);
  });
  it("authenticates webhook and ignores group, bot and foreign chat updates", async () => {
    const denied = await worker.fetch(new Request("https://cloud.test/telegram/webhook", { method: "POST", body: "{}" }), env);
    expect(denied.status).toBe(401);
    await command("/done #1", 1, { chat: { id: 8, type: "private" } });
    await command("/done #1", 2, { chat: { id: 7, type: "group" } });
    await command("/done #1", 3, { from: { is_bot: true } });
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_events").get()?.n).toBe(0);
    expect(sent).toHaveLength(0);
  });
  it("keeps cloud state authoritative over stale local uploads and duplicate updates", async () => {
    await command("/waiting #1", 1);
    const count = sent.length;
    await command("/waiting #1", 1);
    expect(sent.length).toBe(count);
    expect(state(1)?.status).toBe("waiting");
    await publish();
    expect(state(1)?.status).toBe("waiting");
    await command("/tasks waiting");
    expect(sent.at(-1)).toContain("#1 [waiting]");
  });
  it("validates the whole batch before changing any task or queuing mailbox work", async () => {
    await command("/waiting #1 #999");
    await command("/done #1 #999");
    expect(state(1)?.status).toBe("open");
    expect((await poll()).job).toBeNull();
    expect(sent.at(-1)).toContain("No changes made");
  });
  it("queues list while returning a dated digest and preserves task ids", async () => {
    await command("/list");
    expect(sent.join("\n")).toContain("Last successful Outlook sync");
    expect(sent.join("\n")).toContain("<code>#1</code> Private task 1 &lt;review&gt;");
    expect((await poll()).job?.task_ids).toEqual([]);
    await command("/read #1 #1 2");
    const pending = database.db.prepare("SELECT task_ids FROM cloud_events WHERE action='read'").get();
    expect(pending?.task_ids).toBe("[1,2]");
  });
  it("does not mark done until mailbox acknowledgement; newer cloud edits win", async () => {
    await command("/done #1 #2");
    const job = (await poll()).job!;
    expect(state(1)?.status).toBe("open");
    await command("/reopen #1");
    expect((await ack({ event_id: job.id, task_id: 1 })).status).toBe(200);
    expect(state(1)?.status).toBe("open");
    expect((await poll()).job?.completed_ids).toEqual([1]);
    await ack({ event_id: job.id, task_id: 2 });
    expect(state(2)?.status).toBe("done");
    expect((await poll()).job).toBeNull();
    const count = sent.length;
    await ack({ event_id: job.id, task_id: 2 });
    expect(sent.length).toBe(count);
  });
  it("keeps partial completion when later mailbox work fails", async () => {
    await command("/dismiss #1 #2");
    const job = (await poll()).job!;
    await ack({ event_id: job.id, task_id: 1 });
    await ack({ event_id: job.id, failed: true });
    expect(state(1)?.status).toBe("dismissed");
    expect(state(2)?.status).toBe("open");
    expect(sent.at(-1)).toContain("1/2 mailbox items completed");
    expect((await poll()).job).toBeNull();
  });
  it("retains encrypted replies after Telegram failure and resumes without repeating mutation", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    expect((await command("/waiting #1", 1)).status).toBe(200);
    const pending = database.db.prepare("SELECT * FROM cloud_outbox WHERE delivered_at IS NULL").get();
    expect(pending).toBeDefined();
    expect(String(pending?.sealed_chunks)).not.toContain("marked waiting");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: true })));
    await command("/waiting #1", 1);
    expect(database.db.prepare("SELECT * FROM cloud_outbox WHERE delivered_at IS NULL").get()).toBeUndefined();
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_events").get()?.n).toBe(1);
  });
  it("generates a fresh daily cloud digest using changed states once per Singapore date", async () => {
    await command("/waiting #1");
    const start = sent.length;
    await runCloudScheduled(env, new Date("2026-10-20T23:59:00Z"));
    expect(sent.length).toBe(start);
    await runCloudScheduled(env, new Date("2026-10-21T00:00:00Z"));
    const count = sent.length;
    expect(sent.slice(start).join("\n")).toContain("<b>Inbox brief — 2026-10-21</b>");
    expect(sent.slice(start).join("\n")).toContain("<b>WAITING / FOLLOW-UP · 1</b>");
    expect(sent.slice(start).join("\n")).toContain("<code>#1</code>");
    await runCloudScheduled(env, new Date("2026-10-21T00:05:00Z"));
    expect(sent.length).toBe(count);
    await runCloudScheduled(env, new Date("2026-10-22T00:00:00Z"));
    expect(sent.length).toBeGreaterThan(count);
  });
  it("refuses a different local dataset and wrong acknowledgement task", async () => {
    const data = fixture(); data.dataset_id = "00000000-0000-4000-8000-000000000002";
    expect((await publish(data)).status).toBe(409);
    await command("/done #1");
    const job = (await poll()).job!;
    expect((await ack({ event_id: job.id, task_id: 2 })).status).toBe(400);
    expect(state(1)?.status).toBe("open");
  });
  it("provides cached previews only when uploaded explicitly", async () => {
    await command("/show #1"); expect(sent.at(-1)).toContain("No cached body preview");
    const data = fixture(); data.tasks[0].preview = "<secret> & text";
    await publish(data); await command("/show #1");
    expect(sent.at(-1)).toContain("&lt;secret&gt; &amp; text");
    expect(database.db.prepare("SELECT envelope_json FROM cloud_snapshot").get()?.envelope_json).not.toContain("<secret>");
  });
});

describe("digest interactions", () => {
  function eventId(action: string): number {
    return Number(database.db.prepare("SELECT id FROM cloud_events WHERE action=? ORDER BY id DESC LIMIT 1").get(action)!.id);
  }

  it("shows pending, deletes accepted commands after edits, and checks off each confirmed item quietly", async () => {
    await digest();
    const count = sent.length;
    const start = requests.length;
    await command("/done #1 #2", 800);
    await drain();
    expect(state(1)?.status).toBe("open");
    expect(displayed()).toContain("☐ <code>#1</code> <i>· ⏳ Done pending</i>");
    const edits = requests.slice(start);
    expect(edits.at(-1)).toEqual({ method: "deleteMessage", body: { chat_id: "7", message_id: 800 } });
    expect(edits.slice(0, -1).every(r => r.method === "editMessageText")).toBe(true);
    const event_id = eventId("done");
    await ack({ event_id, task_id: 1 });
    await drain();
    expect(displayed()).toContain("☑ <code>#1</code> Private task 1 &lt;review&gt;");
    expect(displayed()).toContain("1 remaining / 2");
    expect(displayed()).toContain("☐ <code>#2</code> <i>· ⏳ Done pending</i>");
    await ack({ event_id, task_id: 2 });
    await drain();
    expect(displayed()).toContain("☑ <code>#2</code>");
    expect(displayed()).toContain("0 remaining / 2");
    expect(displayed()).not.toContain("Done pending");
    expect(sent.length).toBe(count);
    const delivered = requests.length;
    await ack({ event_id, task_id: 2 });
    await command("/done #1 #2", 800);
    expect(requests.length).toBe(delivered);
  });

  it("renders newer waiting/reopen states when an older mailbox command acknowledges", async () => {
    await digest();
    await command("/done #1");
    await drain();
    const event_id = eventId("done");
    await command("/waiting #1");
    await drain();
    expect(displayed()).toContain("☐ <code>#1</code> <i>· 💤 Waiting</i>");
    expect(displayed()).not.toContain("Done pending");
    await ack({ event_id, task_id: 1 });
    await drain();
    expect(state(1)?.status).toBe("waiting");
    expect(displayed()).not.toContain("☑ <code>#1</code>");
    await command("/reopen #1");
    await drain();
    expect(displayed()).toContain("☐ <code>#1</code> Private task");
  });

  it("keeps dismissed rows, marks only partial successes, and sends actionable failures", async () => {
    await digest();
    const count = sent.length;
    await command("/dismiss #1 #2");
    await drain();
    const event_id = eventId("dismiss");
    await ack({ event_id, task_id: 1 });
    await ack({ event_id, failed: true });
    await drain();
    expect(displayed()).toContain("☒ <code>#1</code> Private task");
    expect(displayed()).toContain("☐ <code>#2</code> <i>· ⚠ /dismiss failed</i>");
    expect(sent.slice(count)).toHaveLength(1);
    expect(sent.at(-1)).toContain("1/2 mailbox items completed");
    expect(sent.at(-1)).toContain("resend to retry");
  });

  it("edits the latest digest per task and leaves earlier digests as history", async () => {
    await digest();
    const first = new Set(messages.keys());
    await digest();
    const start = requests.length;
    await command("/waiting #1");
    await drain();
    const edits = requests.slice(start).filter(r => r.method === "editMessageText");
    expect(edits.length).toBeGreaterThan(0);
    expect(edits.every(r => !first.has(Number(r.body.message_id)))).toBe(true);
    expect([...first].map(id => messages.get(id)).join("\n")).not.toContain("Waiting</i>");
  });

  it("falls back to a bounded reply without a tracked digest and leaves invalid commands intact", async () => {
    await command("/done #1", 810);
    await drain();
    expect(sent.at(-1)).toContain("Pending Outlook confirmation: /done");
    expect(requests.at(-1)?.method).toBe("deleteMessage");
    const start = requests.length;
    await command("/done #999", 811);
    await command("/done invalid", 812);
    expect(requests.slice(start).every(r => r.method === "sendMessage")).toBe(true);
    expect(sent.at(-1)).toContain("invalid arguments");
  });

  it("uses one fallback when digest messages disappear and still completes the command", async () => {
    await digest();
    const count = sent.length;
    vi.stubGlobal("fetch", vi.fn(async (url, init) => {
      if (String(url).endsWith("/editMessageText")) return Response.json({ ok: false,
        description: "Bad Request: message to edit not found" }, { status: 400 });
      return telegramOk(url, init);
    }));
    await command("/done #1", 820);
    await drain();
    expect(sent.slice(count)).toHaveLength(1);
    expect(requests.at(-1)?.method).toBe("deleteMessage");
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_digest_messages WHERE unavailable=1").get()?.n).toBeGreaterThan(0);
    await ack({ event_id: eventId("done"), task_id: 1 });
    await drain();
    expect(state(1)?.status).toBe("done");
    expect(sent.at(-1)).toContain("done: #1");
  });

  it("retries Telegram edits without repeating task mutations or deleting commands prematurely", async () => {
    await digest();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    await command("/waiting #1", 830);
    expect(state(1)?.status).toBe("waiting");
    expect(requests.some(r => r.method === "deleteMessage" && r.body.message_id === 830)).toBe(false);
    const row = database.db.prepare("SELECT sealed_chunks FROM cloud_outbox WHERE delivered_at IS NULL").get()!;
    expect(String(row.sealed_chunks)).not.toContain("Private task");
    vi.stubGlobal("fetch", vi.fn(async (url, init) => telegramOk(url, init)));
    await command("/waiting #1", 830);
    await drain();
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_events WHERE dedupe_key='tg:830'").get()?.n).toBe(1);
    expect(requests.at(-1)?.method).toBe("deleteMessage");
  });

  it("accepts unchanged edits and skips expired or permanently rejected cleanup", async () => {
    await digest();
    vi.stubGlobal("fetch", vi.fn(async (url, init) => {
      if (String(url).endsWith("/editMessageText")) return Response.json({ ok: false,
        description: "Bad Request: message is not modified" }, { status: 400 });
      if (String(url).endsWith("/deleteMessage")) return Response.json({ ok: false,
        description: "Bad Request: message can't be deleted" }, { status: 400 });
      return telegramOk(url, init);
    }));
    const count = sent.length;
    await command("/reopen #1", 840);
    await drain();
    await command("/reopen #1", 841, { date: Math.floor(Date.now() / 1000) - 49 * 3600 });
    await drain();
    expect(sent.length).toBe(count);
    expect(database.db.prepare("SELECT id FROM cloud_outbox WHERE delivered_at IS NULL").get()).toBeUndefined();
  });

  it("tracks read confirmation without completing the task", async () => {
    await digest();
    await command("/read #1");
    await drain();
    expect(displayed()).toContain("Read pending");
    await ack({ event_id: eventId("read"), task_id: 1 });
    await drain();
    expect(displayed()).toContain("☐ <code>#1</code> <i>· Email read</i>");
    expect(state(1)?.status).toBe("open");
  });

  it("clears a previous failure label after a successful retry", async () => {
    await digest();
    await command("/done #1");
    await drain();
    await ack({ event_id: eventId("done"), failed: true });
    await drain();
    expect(displayed()).toContain("/done failed");
    await command("/done #1");
    await drain();
    expect(displayed()).not.toContain("/done failed</i>");
    await ack({ event_id: eventId("done"), task_id: 1 });
    await drain();
    expect(displayed()).toContain("☑ <code>#1</code> Private task");
    expect(displayed()).not.toContain("pending</i>");
  });

  it("respects Telegram retry-after before resuming edit and cleanup work", async () => {
    await digest();
    let attempts = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      attempts++;
      return Response.json({ ok: false, parameters: { retry_after: 60 } }, { status: 429 });
    }));
    await command("/waiting #1", 850);
    await poll();
    expect(attempts).toBe(1);
    const now = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(now + 61000);
    try {
      vi.stubGlobal("fetch", vi.fn(async (url, init) => telegramOk(url, init)));
      await drain();
      expect(requests.at(-1)?.method).toBe("deleteMessage");
    } finally { clock.mockRestore(); }
  });

  it("updates scheduled and post-sync digests and preserves legacy queued text replies", async () => {
    await runCloudScheduled(env, new Date("2026-10-21T00:00:00Z"));
    await drain();
    await command("/waiting #1");
    await drain();
    expect(displayed()).toContain("💤 Waiting</i>");
    await command("/list");
    await drain();
    await ack({ event_id: eventId("list") });
    await drain();
    const start = requests.length;
    await command("/reopen #1");
    await drain();
    const latest = database.db.prepare("SELECT message_id FROM cloud_digest_messages WHERE digest_id=(SELECT id FROM cloud_digests ORDER BY rowid DESC LIMIT 1)").all();
    const ids = latest.map(m => m.message_id);
    expect(requests.slice(start).filter(r => r.method === "editMessageText").every(r => ids.includes(Number(r.body.message_id)))).toBe(true);
    // Pending pre-upgrade outbox rows used encrypted arrays of plain HTML strings.
    const { seal } = await import("../src/cloud_crypto");
    database.db.prepare("INSERT INTO cloud_outbox(id,sealed_chunks,created_at) VALUES(?,?,?)")
      .run("legacy", await seal(env, "legacy", ["Legacy queued response"]), new Date().toISOString());
    await drain();
    expect(sent.at(-1)).toBe("Legacy queued response");
  });

  it("reserves room for status labels in large multi-message digests and encrypts saved content", async () => {
    const data = fixture();
    data.tasks = Array.from({ length: 100 }, (_, i) => ({ ...data.tasks[0], id: i + 1,
      description: "Private <task> & review 😀".repeat(2) }));
    await publish(data);
    await digest();
    const ids = data.tasks.map(t => `#${t.id}`).join(" ");
    await command(`/done ${ids}`);
    await drain();
    const edits = requests.filter(r => r.method === "editMessageText");
    expect(edits.length).toBeGreaterThan(2);
    expect(edits.every(r => String(r.body.text).length <= 4096)).toBe(true);
    const stored = database.db.prepare("SELECT * FROM cloud_digests").all();
    expect(JSON.stringify(stored)).not.toContain("Private");
    expect(JSON.stringify(stored)).not.toContain("review");
  });
});

describe("command and rendering contracts", () => {
  it("preserves command grammar and rejects invalid limits and ids", () => {
    expect(parseCloudCommand("/retry 100")?.limit).toBe(100);
    for (const text of ["/retry 101", "/retry 0", "/sync now", "/show 1 2", "/done 0", "/read 9007199254740992"]) expect(parseCloudCommand(text)).toBeNull();
    expect(parseCloudCommand("/done@bot #2 #2 1")?.ids).toEqual([2, 1]);
    expect(parseCloudCommand("/start")?.action).toBe("help");
  });
  it("bounds escaped chunks without splitting entities and omits closed tasks", () => {
    const chunks = textChunks("<>&😀".repeat(3000));
    expect(chunks.every(c => c.length <= 3400 && !c.endsWith("&"))).toBe(true);
    const data = fixture(); data.tasks[0].status = "done";
    expect(renderDigest(data).join("\n")).not.toContain("<code>#1</code> Private task");
    expect(renderDigest(data).join("\n")).toContain("<code>#2</code> Private task");
  });
  it("restores digest headings, task IDs, deadline emphasis and escaped senders", () => {
    const data = fixture();
    data.tasks[0].sender_name = "A & B <team>";
    const rendered = renderDigest(data, new Date("2026-10-22T00:00:00Z")).join("\n");
    expect(rendered).toContain("📬 <b>Inbox brief — 2026-10-22</b>");
    expect(rendered).toContain("<b>OPEN ACTIONS</b>");
    expect(rendered).toContain("🟠 <b>URGENT / DUE TODAY · 2</b>");
    expect(rendered).toContain("☐ <code>#1</code> Private task 1 &lt;review&gt;");
    expect(rendered).toContain("Due <b>today</b>");
    expect(rendered).toContain("<i>A &amp; B &lt;team&gt;</i>");
    expect(rendered).toContain("Informational: <b>3</b>");
    expect(rendered).not.toContain("&lt;b&gt;");
  });
  it("keeps long digest chunks balanced and within Telegram limits without losing task content", () => {
    const data = fixture();
    data.tasks[0].description = "<>&😀".repeat(3000);
    data.tasks[0].sender_name = "<sender&>".repeat(1000);
    const chunks = renderDigest(data);
    expect(chunks.length).toBeGreaterThan(3);
    for (const chunk of chunks) {
      expect(chunk.length).toBeLessThanOrEqual(3400);
      const stack: string[] = [];
      for (const match of chunk.matchAll(/<(\/?)(b|i|code)>/g)) {
        if (match[1]) expect(stack.pop()).toBe(match[2]);
        else stack.push(match[2]);
      }
      expect(stack).toEqual([]);
      expect(chunk).not.toMatch(/&(?!amp;|lt;|gt;)/);
    }
    const joined = chunks.join("\n");
    expect(joined.match(/&lt;&gt;&amp;😀/g)).toHaveLength(3000);
    expect(joined).toContain("PART 2 OF");
    expect(joined).toContain("<i>(continued)</i>");
  });
});
