import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import worker, { decryptEnvelope, type Env } from "../src/index";
import { parseCloudCommand, renderDigest, runCloudScheduled, textChunks, validateBridge, type BridgeSnapshot } from "../src/cloud";
import { digestMessage, type DigestRow } from "../src/interactive-digest";
import { unseal } from "../src/cloud";
import { digestGroups } from "../src/task-feedback";
import { renderRichDigest } from "../src/rich-digest";

const legacyFixture = vi.hoisted(() => ({ enabled: false }));
// Exercise saved pre-upgrade editions through the real legacy delivery and callback paths.
vi.mock("../src/interactive-digest", async importOriginal => {
  const actual = await importOriginal<typeof import("../src/interactive-digest")>();
  return { ...actual, queueDigest: async (env: Env, now = new Date()) => {
    const queued = await actual.queueDigest(env, now);
    if (legacyFixture.enabled) {
      const { snapshot, renderDigest, seal } = await import("../src/cloud");
      const data = await snapshot(env);
      const pages = data ? renderDigest(data, now) : ["No bridge snapshot yet. The digest will become available after Windows publishes one.", "No active tasks."];
      queued.statements.push(env.DB.prepare("UPDATE interactive_digests SET format='legacy',sealed_pages=? WHERE id=?")
        .bind(await seal(env, queued.id, pages), queued.id));
    }
    return queued;
  } };
});

// Run production SQL against SQLite rather than matching SQL strings in a mock.
class SQLiteD1 {
  db = new DatabaseSync(":memory:");
  constructor() {
    this.db.exec("PRAGMA foreign_keys=ON");
    this.db.exec(readFileSync(new URL("../migrations/0001_initial.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0002_cloud_bot.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0003_digest_interactions.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0003_interactive_digests.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0004_digest_payload_hash.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0005_quiet_task_feedback.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0006_list_reply_cleanup.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0007_news_trial.sql", import.meta.url), "utf8"));
    this.db.exec(readFileSync(new URL("../migrations/0008_rich_digests.sql", import.meta.url), "utf8"));
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
function allSectionsFixture(): BridgeSnapshot {
  const data = fixture();
  data.tasks = [
    { ...data.tasks[0], id: 1, deadline: "2026-10-31" },
    { ...data.tasks[0], id: 2, deadline: "2026-11-01" },
    { ...data.tasks[0], id: 3, deadline: "2026-11-03" },
    { ...data.tasks[0], id: 4, deadline: null },
    { ...data.tasks[0], id: 5, status: "waiting", deadline: null },
  ];
  return data;
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
let telegramCalls: { method: string; body: Record<string, any> }[];
let nextMessage: number;
function telegramMock() {
  return vi.fn(async (url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    const method = url.split("/").at(-1)!;
    telegramCalls.push({ method, body });
    if (method === "sendMessage") sent.push(body.text);
    if (method === "sendRichMessage") sent.push(body.rich_message.html);
    return Response.json({ ok: true, result: { message_id: body.message_id ?? ++nextMessage } });
  });
}
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
function digestRows() { return database.db.prepare("SELECT * FROM interactive_digests ORDER BY digest_date,id").all() as unknown as DigestRow[]; }
async function storedPages(row: DigestRow) { return unseal(env, row.id, row.sealed_pages); }
async function callback(row: DigestRow, view = "overview", page = 0, changes = {}) {
  return worker.fetch(new Request("https://cloud.test/telegram/webhook", {
    method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "webhook" }, body: JSON.stringify({ update_id: ++sequence,
      callback_query: { id: "tap", data: `digest:${row.id}:${view}:${page}`, from: { id: 7 },
        message: { message_id: row.message_id, chat: { id: 7, type: "private" } }, ...changes } }),
  }), env);
}

beforeEach(async () => {
  legacyFixture.enabled = false;
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-01T02:00:00Z"));
  database = new SQLiteD1(); sent = []; sequence = 100; telegramCalls = []; nextMessage = 1000;
  env = { DB: database as unknown as D1Database, RELAY_TOKEN: "relay", RELAY_ENCRYPTION_KEY: encodedKey,
    TELEGRAM_BOT_TOKEN: "bot", TELEGRAM_CHAT_ID: "7", TELEGRAM_WEBHOOK_SECRET: "webhook", CLOUD_BOT_ENABLED: "true" };
  vi.stubGlobal("fetch", telegramMock());
  expect((await publish()).status).toBe(200);
});
afterEach(() => { database.db.close(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("preview retention", () => {
  async function saved() {
    const row = database.db.prepare("SELECT envelope_json,generated_ms,last_seen FROM cloud_snapshot").get()!;
    const value = JSON.parse(String(row.envelope_json));
    const payload = await decryptEnvelope(value, env.RELAY_ENCRYPTION_KEY);
    return { row, envelope: value, data: payload.bridge! };
  }
  function dataWithPreviews(): BridgeSnapshot {
    const data = fixture();
    data.tasks.forEach(task => { task.preview = `task body ${task.id}`; });
    data.information = [{ id: 1, summary: "FYI", subject: "FYI", sender_name: "Sender", sender_address: "sender@test",
      received_at: new Date(Date.now() - 71 * 3600000).toISOString(), is_read: false, preview: "information body" }];
    return data;
  }
  it("purges closed and read previews from stored encrypted snapshots, retaining eligible bodies", async () => {
    const data = dataWithPreviews();
    database.db.prepare("UPDATE cloud_tasks SET status='done' WHERE id=1").run();
    data.information![0].is_read = true;
    await publish(data);
    const stored = await saved();
    expect(stored.data.tasks[0].preview).toBeUndefined();
    expect(stored.data.tasks[1].preview).toBe("task body 2");
    expect(stored.data.information![0].preview).toBeUndefined();
    expect(stored.data.information![0].summary).toBe("FYI");
    expect(String(stored.row.envelope_json)).not.toContain("task body 2");
    await command("/show #1");
    expect(sent.at(-1)).toContain("no longer eligible");
  });
  it("expires informational bodies while Windows is offline, preserving freshness and using a new nonce", async () => {
    await publish(dataWithPreviews());
    const before = await saved();
    vi.setSystemTime(new Date(Date.now() + 3600000));
    await command("/show #i1");
    expect(sent.at(-1)).toContain("no longer eligible");
    await runCloudScheduled(env);
    const after = await saved();
    expect(after.data.information![0].preview).toBeUndefined();
    expect(after.data.tasks[0].preview).toBe("task body 1");
    expect(after.row.generated_ms).toBe(before.row.generated_ms);
    expect(after.row.last_seen).toBe(before.row.last_seen);
    expect(after.envelope.generated_at).toBe(before.envelope.generated_at);
    expect(after.envelope.nonce).not.toBe(before.envelope.nonce);
    const sealed = after.row.envelope_json;
    await runCloudScheduled(env);
    expect((await saved()).row.envelope_json).toBe(sealed);
  });
  it.each(["done", "dismiss"])("removes a task preview after %s confirmation and waits for a new publication after reopen", async action => {
    await publish(dataWithPreviews());
    await command(`/${action} #1`);
    expect((await saved()).data.tasks[0].preview).toBe("task body 1");
    const job = (await poll()).job!;
    await ack({ event_id: job.id, task_id: 1 });
    expect((await saved()).data.tasks[0].preview).toBeUndefined();
    expect((await saved()).data.tasks[1].preview).toBe("task body 2");
    await command("/reopen #1");
    await command("/show #1");
    expect(sent.at(-1)).toContain("No cached body preview");
  });
  it("keeps open and waiting previews, drops expired or unknown-read previews on upload, and clears disabled caching", async () => {
    const data = dataWithPreviews();
    database.db.prepare("UPDATE cloud_tasks SET status='waiting' WHERE id=1").run();
    data.information![0].is_read = null;
    data.information!.push({ ...data.information![0], id: 2, is_read: false, received_at: new Date(Date.now() - 73 * 3600000).toISOString() });
    await publish(data);
    expect((await saved()).data.tasks.every(task => task.preview !== undefined)).toBe(true);
    expect((await saved()).data.information!.every(item => item.preview === undefined)).toBe(true);
    data.tasks.forEach(task => { delete task.preview; });
    await publish(data);
    expect((await saved()).data.tasks.every(task => task.preview === undefined)).toBe(true);
  });
});

describe("informational emails", () => {
  function withInformation(): BridgeSnapshot {
    return { ...fixture(), information: [{ id: 1, subject: "<FYI>", summary: "Saved <summary> & detail",
      sender_name: "Sender", sender_address: "sender@test", received_at: new Date().toISOString(), is_read: false,
      preview: "Original <body>" }] };
  }
  it("keeps informational command IDs separate and rejects mixed or mutating commands", () => {
    expect(parseCloudCommand("/read@bot #i1 I2 #i1")).toMatchObject({ ids: [1, 2], information: true });
    expect(parseCloudCommand("/show i1")).toMatchObject({ ids: [1], information: true });
    for (const text of ["/read #i1 #1", "/done #i1", "/waiting #i1", "/show i1 i2", "/read i0", "/read i9007199254740992"]) {
      expect(parseCloudCommand(text)).toBeNull();
    }
  });
  it("validates optional collections and rejects malformed informational data", () => {
    expect(validateBridge(fixture()).information).toBeUndefined();
    for (const change of [{ id: 0 }, { is_read: 0 }, { received_at: "invalid" }, { preview: "x".repeat(12001) }, { summary: "" }]) {
      const data = withInformation(); Object.assign(data.information![0], change);
      expect(() => validateBridge(data)).toThrow();
    }
    const data = withInformation(); data.information!.push(data.information![0]);
    expect(() => validateBridge(data)).toThrow();
  });
  it("uses a strict 72-hour window and renders escaped summaries after waiting", () => {
    const data = withInformation();
    for (const [id, hours, read] of [[2, 72, false], [3, 71, true], [4, -1, false], [5, 71, false]] as const) {
      data.information!.push({ ...data.information![0], id, received_at: new Date(Date.now() - hours * 3600000).toISOString(), is_read: read });
    }
    const plain = renderDigest(data).join("\n"), rich = renderRichDigest(data).join("\n");
    for (const rendered of [plain, rich]) {
      expect(rendered.indexOf("INFORMATIONAL EMAILS")).toBeGreaterThan(rendered.indexOf("WAITING / FOLLOW-UP"));
      expect(rendered).toContain("Saved &lt;summary&gt; &amp; detail");
      expect(rendered).toContain("/show #i1"); expect(rendered).toContain("/read #i5");
      for (const id of [2, 3, 4]) expect(rendered).not.toContain(`/show #i${id}`);
    }
  });
  it("queues reads until bridge confirmation and never changes matching task IDs", async () => {
    const data = withInformation(); await publish(data);
    await command("/show #i1"); expect(sent.join("\n")).toContain("Original &lt;body&gt;");
    await command("/read #i1");
    const job = (await poll()).job!;
    expect(database.db.prepare("SELECT action FROM cloud_events WHERE id=?").get(job.id)!.action).toBe("read_information");
    expect(job.task_ids).toEqual([1]);
    expect(renderDigest(data).join("\n")).toContain("/read #i1");
    data.information![0].is_read = true; await publish(data);
    await ack({ event_id: job.id, task_id: 1 });
    expect(state(1)!.status).toBe("open");
    expect(database.db.prepare("SELECT COUNT(*) AS count FROM cloud_task_feedback WHERE event_id=?").get(job.id)!.count).toBe(0);
    expect(sent.join("\n")).toContain("#i1");
    expect(renderDigest(data).join("\n")).not.toContain("/read #i1");
  });
  it("freezes edition membership and summaries while removing confirmed reads", async () => {
    const data = withInformation(); await publish(data); await command("/list");
    const row = digestRows()[0];
    const layout = JSON.parse((await unseal(env, row.id, row.sealed_layout!))[0]);
    expect(layout.information[0].preview).toBeUndefined();
    expect(layout.information[0].summary).toBe("Saved <summary> & detail");
    data.information![0].summary = "Changed summary";
    data.information!.push({ ...data.information![0], id: 2, summary: "New arrival" });
    await publish(data);
    let html = (await storedPages(digestRows()[0])).join("\n");
    expect(html).toContain("Saved &lt;summary&gt;"); expect(html).not.toContain("Changed summary"); expect(html).not.toContain("New arrival");
    data.information![0].is_read = true; await publish(data);
    html = (await storedPages(digestRows()[0])).join("\n");
    expect(html).not.toContain("/show #i1");
    data.information = data.information!.filter(item => item.id !== 1); await publish(data);
    expect((await storedPages(digestRows()[0])).join("\n")).not.toContain("/show #i1");
    await command("/list");
    const fresh = digestRows().find(digest => digest.id !== row.id)!;
    expect((await storedPages(fresh)).join("\n")).toContain("New arrival");
  });
});

describe("temporary /list acknowledgement messages", () => {
  function replies(event: number) {
    return database.db.prepare("SELECT command_message_id,delivered_at FROM cloud_task_feedback WHERE event_id=? ORDER BY id").all(event);
  }
  it("deletes pending and completed bot replies only after a successful digest refresh", async () => {
    await command("/list", 900);
    const job = (await poll()).job!;
    const queued = Number(replies(job.id)[0].command_message_id);
    const digest = digestRows()[0];
    expect(telegramCalls.filter(call => call.method === "deleteMessage")).toHaveLength(0);
    expect(replies(job.id)[0].delivered_at).toBeNull();
    await ack({ event_id: job.id });
    const notices = replies(job.id);
    expect(notices).toHaveLength(2);
    expect(notices.every(row => row.delivered_at !== null)).toBe(true);
    expect(telegramCalls.filter(call => call.method === "deleteMessage").map(call => call.body.message_id))
      .toEqual([queued, Number(notices[1].command_message_id)]);
    expect(telegramCalls.filter(call => call.method === "deleteMessage").every(call => ![900, digest.message_id].includes(call.body.message_id))).toBe(true);
    const count = telegramCalls.length;
    await ack({ event_id: job.id }); await command("/list", 900);
    expect(telegramCalls).toHaveLength(count);
    expect(digestRows()).toHaveLength(1);
  });
  it("keeps acknowledgement messages until a required digest edit succeeds", async () => {
    await command("/list"); const job = (await poll()).job!;
    await database.prepare("UPDATE interactive_digests SET last_payload_sha256=NULL").run();
    const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("editMessageText")
      ? Response.json({ ok: false }, { status: 503 }) : normal(url, init)));
    await ack({ event_id: job.id });
    expect(digestRows()[0].dirty).toBe(1);
    expect(telegramCalls.filter(call => call.method === "deleteMessage")).toHaveLength(0);
    expect(replies(job.id)).toHaveLength(2);
    vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", telegramMock());
    await poll();
    expect(digestRows()[0].dirty).toBe(0);
    expect(telegramCalls.filter(call => call.method === "deleteMessage")).toHaveLength(2);
  });
  it("retries rate-limited cleanup without resending replies or repeating synchronization", async () => {
    await command("/list"); const job = (await poll()).job!;
    const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("deleteMessage")
      ? Response.json({ ok: false, parameters: { retry_after: 30 } }, { status: 429 }) : normal(url, init)));
    await ack({ event_id: job.id });
    const fetcher = telegramMock(); vi.stubGlobal("fetch", fetcher);
    await poll(); expect(fetcher).not.toHaveBeenCalled();
    vi.setSystemTime(new Date(Date.now() + 31000)); await poll();
    expect(telegramCalls.filter(call => call.method === "deleteMessage")).toHaveLength(2);
    expect(sent.filter(text => text.includes("/list queued"))).toHaveLength(1);
    expect(sent.filter(text => text.includes("/list completed"))).toHaveLength(1);
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_events WHERE action='list'").get()?.n).toBe(1);
    expect((await poll()).job).toBeNull();
  });
  it("leaves failure messages visible", async () => {
    await command("/list"); const job = (await poll()).job!;
    await ack({ event_id: job.id, failed: true });
    expect(sent.at(-1)).toContain("/list failed on the bridge");
    expect(telegramCalls.filter(call => call.method === "deleteMessage")).toHaveLength(0);
    expect(database.db.prepare("SELECT id FROM cloud_task_feedback WHERE delivered_at IS NULL").get()).toBeUndefined();
  });
  it("skips expired pending-notice cleanup while deleting the new completion notice", async () => {
    await command("/list"); const job = (await poll()).job!;
    const queued = Number(replies(job.id)[0].command_message_id);
    vi.setSystemTime(new Date(Date.now() + 49 * 3600000));
    await ack({ event_id: job.id });
    const deleted = telegramCalls.filter(call => call.method === "deleteMessage").map(call => call.body.message_id);
    expect(deleted).not.toContain(queued);
    expect(deleted).toEqual([Number(replies(job.id)[1].command_message_id)]);
  });
  it("keeps /sync and /retry replies unchanged", async () => {
    for (const text of ["/sync", "/retry"]) {
      await command(text); await ack({ event_id: (await poll()).job!.id });
    }
    expect(sent).toHaveLength(4);
    expect(telegramCalls.filter(call => call.method === "deleteMessage")).toHaveLength(0);
  });
});

describe("PR #13 quiet task feedback with interactive pages", () => {
  beforeEach(() => { legacyFixture.enabled = true; });
  async function trackedDigest() {
    await command("/list");
    await ack({ event_id: (await poll()).job!.id });
    await callback(digestRows().at(-1)!, "outlook");
  }
  function latestEvent(action: string) {
    return Number(database.db.prepare("SELECT id FROM cloud_events WHERE action=? ORDER BY id DESC").get(action)!.id);
  }
  async function saved() { return (await storedPages(digestRows()[0])).join("\n"); }
  it("shows pending and confirmed rows quietly, deleting commands only after feedback", async () => {
    await trackedDigest(); const count = sent.length; telegramCalls = [];
    await command("/done #1 #2", 800);
    expect(await saved()).toContain("☐ <code>#1</code> <i>· ⏳ Done pending</i>");
    expect(state(1)?.status).toBe("open");
    expect(telegramCalls.map(call => call.method)).toEqual(["editMessageText", "deleteMessage"]);
    expect(telegramCalls.at(-1)?.body.message_id).toBe(800);
    const event_id = latestEvent("done");
    await ack({ event_id, task_id: 1 });
    expect(await saved()).toContain("☑ <code>#1</code> Private task");
    expect(await saved()).toContain("1 remaining / 2");
    expect(await saved()).toContain("☐ <code>#2</code> <i>· ⏳ Done pending</i>");
    await ack({ event_id, task_id: 2 });
    expect(await saved()).toContain("☑ <code>#2</code>");
    expect(await saved()).toContain("0 remaining / 2");
    expect(await saved()).not.toContain("Done pending");
    expect(sent).toHaveLength(count);
    const calls = telegramCalls.length;
    await ack({ event_id, task_id: 2 }); await command("/done #1 #2", 800);
    expect(telegramCalls).toHaveLength(calls);
    const originalId = digestRows()[0].id;
    await command("/list");
    expect((await storedPages(digestRows().find(row => row.id !== originalId)!)).join("\n")).toContain("No active tasks.");
  });
  it("preserves original section membership while fresh digests regroup tasks", async () => {
    await trackedDigest(); const original = digestRows()[0];
    await command("/waiting #1");
    const pages = await storedPages(digestRows()[0]);
    expect(pages[1]).toContain("OVERDUE · 2");
    expect(pages[1]).toContain("☐ <code>#1</code> <i>· 💤 Waiting</i>");
    expect(pages[1]).not.toContain("<b>WAITING / FOLLOW-UP");
    await command("/list");
    const fresh = digestRows().find(row => row.id !== original.id)!;
    expect((await storedPages(fresh)).join("\n")).toContain("<b>WAITING / FOLLOW-UP · 1</b>");
  });
  it("keeps partial checkmarks, reports failure once, and clears failure after retry", async () => {
    await trackedDigest(); const count = sent.length;
    await command("/dismiss #1 #2"); const event_id = latestEvent("dismiss");
    await ack({ event_id, task_id: 1 }); await ack({ event_id, failed: true });
    expect(await saved()).toContain("☒ <code>#1</code> Private task");
    expect(await saved()).toContain("☐ <code>#2</code> <i>· ⚠ /dismiss failed</i>");
    expect(sent.slice(count)).toHaveLength(1);
    expect(sent.at(-1)).toContain("1/2 mailbox items completed");
    await command("/dismiss #2");
    expect(await saved()).not.toContain("/dismiss failed</i>");
    expect(await saved()).toContain("Dismiss pending");
    await ack({ event_id: latestEvent("dismiss"), task_id: 2 });
    expect(await saved()).toContain("☒ <code>#2</code>");
  });
  it("falls back for missing or older digests and leaves invalid commands intact", async () => {
    await command("/done #1", 810);
    expect(sent.at(-1)).toContain("Pending Outlook confirmation: /done");
    expect(telegramCalls.at(-1)?.method).toBe("deleteMessage");
    const start = telegramCalls.length;
    await command("/done #1 #999", 811); await command("/done invalid", 812);
    expect(telegramCalls.slice(start).every(call => call.method === "sendMessage")).toBe(true);
    await trackedDigest(); const historical = await saved();
    vi.setSystemTime(new Date("2026-11-02T02:00:00Z")); telegramCalls = [];
    await command("/waiting #1");
    expect(telegramCalls.map(call => call.method)).toEqual(["sendMessage", "deleteMessage"]);
    expect(await saved()).toBe(historical);
  });
  it("falls back when the only digest is deleted and still reports completion", async () => {
    await trackedDigest(); const count = sent.length; const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("editMessageText")
      ? Response.json({ ok: false, description: "Bad Request: message to edit not found" }, { status: 400 }) : normal(url, init)));
    await command("/done #1", 820);
    expect(sent.slice(count)).toHaveLength(1);
    expect(sent.at(-1)).toContain("Pending Outlook confirmation: /done");
    expect(telegramCalls.at(-1)?.method).toBe("deleteMessage");
    await ack({ event_id: latestEvent("done"), task_id: 1 });
    expect(state(1)?.status).toBe("done"); expect(sent.at(-1)).toContain("done: #1");
  });
  it("never deletes commands before a failed edit is retried", async () => {
    await trackedDigest(); telegramCalls = [];
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    await command("/waiting #1", 830);
    expect(state(1)?.status).toBe("waiting");
    expect(telegramCalls).toHaveLength(0);
    expect(database.db.prepare("SELECT id FROM cloud_task_feedback WHERE delivered_at IS NULL").get()).toBeDefined();
    vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", telegramMock());
    await command("/waiting #1", 830);
    expect(telegramCalls.map(call => call.method)).toEqual(["editMessageText", "deleteMessage"]);
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_events WHERE dedupe_key='tg:830'").get()?.n).toBe(1);
  });
  it("does not repeat fallback feedback if cleanup fails and skips expired cleanup", async () => {
    const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("deleteMessage")
      ? Response.json({ ok: false }, { status: 503 }) : normal(url, init)));
    await command("/waiting #1", 840);
    expect(sent).toHaveLength(1);
    vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", telegramMock());
    await poll(); expect(sent).toHaveLength(1);
    expect(telegramCalls.at(-1)?.method).toBe("deleteMessage");
    telegramCalls = [];
    await command("/reopen #1", 841, { date: Math.floor(Date.now() / 1000) - 49 * 3600 });
    expect(telegramCalls.every(call => call.method === "sendMessage")).toBe(true);
    const accepted = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("deleteMessage")
      ? Response.json({ ok: false }, { status: 400 }) : accepted(url, init)));
    await command("/waiting #1", 842);
    expect(state(1)?.status).toBe("waiting");
    expect(database.db.prepare("SELECT id FROM cloud_task_feedback WHERE delivered_at IS NULL").get()).toBeUndefined();
  });
  it("keeps annotations within the page budget and encrypts retained layouts", async () => {
    const data = fixture(); data.tasks = Array.from({ length: 100 }, (_, index) => ({ ...data.tasks[0], id: index + 1,
      description: "Private <task> & review 😀".repeat(2), preview: "Body preview must not be retained in layouts" }));
    await publish(data); await trackedDigest();
    await command('/done '+data.tasks.map(task => `#${task.id}`).join(' '));
    const row = digestRows()[0]; const pages = await storedPages(row);
    expect(pages.join("\n").match(/Done pending/g)).toHaveLength(100);
    for (let page = 0; page < pages.length - 1; page++) {
      const text = digestMessage({ ...row, view: "outlook", page }, pages, row.feedback_text).payload.text;
      expect(text.length).toBeLessThanOrEqual(3400);
    }
    expect(row.sealed_layout).not.toContain("Private");
    const layout = (await unseal(env, row.id, row.sealed_layout!))[0];
    expect(layout).not.toContain("Body preview");
  });
  it("backfills existing interactive layouts before changing task state", async () => {
    await trackedDigest();
    await database.prepare("UPDATE interactive_digests SET sealed_layout=NULL,task_ids_json='[]'").run();
    await command("/done #1"); await ack({ event_id: latestEvent("done"), task_id: 1 });
    expect(await saved()).toContain("☑ <code>#1</code>");
    expect(digestRows()[0].task_ids_json).toBe("[1,2]");
  });
  it("retains command history but cleans delivered layouts and feedback after 30 days", async () => {
    await trackedDigest(); const original = digestRows()[0].id;
    await command("/waiting #1", 860);
    await database.prepare("INSERT INTO cloud_digests(id,sealed_chunks,groups_json,created_at) VALUES(?,?,?,?)")
      .bind("old-pr13", "encrypted", "[]", new Date().toISOString()).run();
    await database.prepare("INSERT INTO cloud_digest_messages(digest_id,chunk_index,message_id) VALUES(?,0,?)").bind("old-pr13", 2000).run();
    vi.setSystemTime(new Date(Date.now() + 31 * 86400000));
    await runCloudScheduled(env);
    expect(digestRows().some(row => row.id === original)).toBe(false);
    expect(database.db.prepare("SELECT id FROM cloud_digests WHERE id='old-pr13'").get()).toBeUndefined();
    expect(database.db.prepare("SELECT id FROM cloud_events WHERE dedupe_key='tg:860'").get()).toBeDefined();
    expect(database.db.prepare("SELECT id FROM cloud_task_feedback WHERE event_id=?").get(latestEvent("waiting"))).toBeUndefined();
  });
  it("defers layout cleanup while deliveries are pending", async () => {
    await trackedDigest(); const original = digestRows()[0].id;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    await command("/help");
    vi.setSystemTime(new Date(Date.now() + 31 * 86400000));
    await runCloudScheduled(env);
    expect(digestRows().some(row => row.id === original)).toBe(true);
  });
  it("keeps the PR #13 schema and saved messages when applying interactive migrations", () => {
    const prior = new DatabaseSync(":memory:");
    try {
      for (const file of ["0001_initial.sql", "0002_cloud_bot.sql", "0003_digest_interactions.sql"]) prior.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
      prior.prepare("INSERT INTO cloud_digests VALUES(?,?,?,?)").run("pr13", "encrypted", "[]", "2026-11-01");
      prior.prepare("INSERT INTO cloud_digest_messages(digest_id,chunk_index,message_id) VALUES(?,0,?)").run("pr13", 42);
      for (const file of ["0003_interactive_digests.sql", "0004_digest_payload_hash.sql", "0005_quiet_task_feedback.sql"]) prior.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
      expect(prior.prepare("SELECT message_id FROM cloud_digest_messages WHERE digest_id='pr13'").get()?.message_id).toBe(42);
    } finally { prior.close(); }
  });
});

describe("interactive digests", () => {
  beforeEach(() => { legacyFixture.enabled = true; });
  it("skips unchanged visible messages while saving refreshed off-screen pages", async () => {
    await command("/list");
    const row = digestRows()[0];
    expect(row.last_payload_sha256).toMatch(/^[a-f0-9]{64}$/);
    telegramCalls = [];
    const data = fixture(); data.tasks[0].description = "New off-screen description";
    await publish(data);
    expect(telegramCalls.filter(c => c.method === "editMessageText")).toHaveLength(0);
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("New off-screen description");
    expect(digestRows()[0].last_payload_sha256).toBe(row.last_payload_sha256);
    await callback(row, "outlook");
    expect(telegramCalls.at(-1)?.body.text).toContain("New off-screen description");
    const count = telegramCalls.filter(c => c.method === "editMessageText").length;
    await callback(row, "outlook");
    expect(telegramCalls.filter(c => c.method === "editMessageText")).toHaveLength(count);
    expect(telegramCalls.at(-1)?.method).toBe("answerCallbackQuery");
  });
  it("updates a legacy message once when no successful payload hash is recorded", async () => {
    await command("/list");
    await database.prepare("UPDATE interactive_digests SET last_payload_sha256=NULL,dirty=1").run();
    telegramCalls = []; await poll();
    expect(telegramCalls.filter(c => c.method === "editMessageText")).toHaveLength(1);
    telegramCalls = []; await publish();
    expect(telegramCalls.filter(c => c.method === "editMessageText")).toHaveLength(0);
  });
  it("does not remember a failed edit as successfully displayed", async () => {
    await command("/list"); const before = digestRows()[0].last_payload_sha256;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    await command("/waiting #1");
    expect(digestRows()[0].last_payload_sha256).toBe(before);
    vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", telegramMock());
    await poll();
    expect(digestRows()[0].last_payload_sha256).not.toBe(before);
    expect(digestRows()[0].dirty).toBe(0);
  });
  it("retries a pending edit at the next minute without sending another daily digest", async () => {
    await runCloudScheduled(env);
    await callback(digestRows()[0], "outlook");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    await command("/waiting #1");
    vi.setSystemTime(new Date(Date.now() + 60000)); vi.stubGlobal("fetch", telegramMock());
    await runCloudScheduled(env);
    expect(digestRows()).toHaveLength(1);
    expect(digestRows()[0].dirty).toBe(0);
    expect(telegramCalls.filter(c => c.method === "sendMessage" && c.body.reply_markup)).toHaveLength(1);
  });
  it("sends one digest per list and edits it on bridge completion", async () => {
    await command("/list");
    const row = digestRows()[0];
    expect(row.message_id).toBeGreaterThan(0);
    expect(telegramCalls.filter(c => c.method === "sendMessage" && c.body.reply_markup)).toHaveLength(1);
    expect(row.sealed_pages).not.toContain("Private task");
    const job = (await poll()).job!;
    await ack({ event_id: job.id });
    expect(digestRows()).toHaveLength(1);
    expect(telegramCalls.filter(c => c.method === "sendMessage" && c.body.reply_markup)).toHaveLength(1);
    expect(telegramCalls.filter(c => c.method === "editMessageText")).toHaveLength(0);
    expect(digestRows()[0].dirty).toBe(0);
  });
  it("explains missing news and acknowledges repeated navigation taps", async () => {
    await command("/list");
    const row = digestRows()[0];
    await callback(row, "news");
    expect(telegramCalls.at(-1)?.body.text).toContain("News trial is disabled or no selection was saved");
    const count = sent.length;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => url.endsWith("editMessageText")
      ? Response.json({ ok: false, description: "Bad Request: message is not modified" }, { status: 400 })
      : Response.json({ ok: true })));
    expect((await callback(row, "news")).status).toBe(200);
    expect(digestRows()[0].dirty).toBe(0);
    expect(sent).toHaveLength(count);
  });
  it("shares frozen daily news with list digests and preserves it through refresh and next-day navigation", async () => {
    env.NEWS_TRIAL_ENABLED = "true";
    let requests = 0;
    const telegram = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      if (!url.startsWith("https://hacker-news.firebaseio.com/")) return telegram(url, init);
      requests++;
      return Response.json(url.endsWith("topstories.json") ? [42] : {
        id: 42, type: "story", title: "Trial headline <safe>", time: Date.now() / 1000 - 60,
      });
    }));
    await runCloudScheduled(env);
    const daily = digestRows()[0];
    expect(daily.sealed_news).not.toContain("Trial headline");
    await callback(daily, "news");
    expect(telegramCalls.at(-1)?.body.text).toContain("Trial headline &lt;safe&gt;");
    await command("/list");
    await command("/waiting #1");
    expect(requests).toBe(2);
    for (const row of digestRows()) expect((await unseal(env, row.id, row.sealed_news!))[0]).toContain("Trial headline");
    vi.setSystemTime(new Date(Date.now() + 86400000));
    await callback(daily, "outlook");
    await callback(daily, "news");
    expect(requests).toBe(2);
    expect(telegramCalls.at(-1)?.body.text).toContain("Trial headline");
    await runCloudScheduled(env);
    expect(requests).toBe(4);
  });
  it("delivers the daily brief without a bridge snapshot even when news fails", async () => {
    env.NEWS_TRIAL_ENABLED = "true";
    await database.prepare("DELETE FROM cloud_snapshot").run();
    const telegram = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      if (url.startsWith("https://hacker-news.firebaseio.com/")) throw new Error("source down");
      return telegram(url, init);
    }));
    await runCloudScheduled(env);
    const row = digestRows()[0];
    expect(row.message_id).toBeGreaterThan(0);
    await callback(row, "news");
    expect(telegramCalls.at(-1)?.body.text).toContain("News could not be loaded");
    await runCloudScheduled(env);
    expect(digestRows()).toHaveLength(1);
  });
  it("rejects forged users, chats, message IDs, views, namespaces and webhook secrets", async () => {
    await command("/list"); const row = digestRows()[0];
    const count = telegramCalls.length;
    for (const changes of [
      { from: { id: 8 } }, { from: { id: 7, is_bot: true } },
      { message: { message_id: row.message_id, chat: { id: 8, type: "private" } } },
      { message: { message_id: row.message_id, chat: { id: 7, type: "group" } } },
      { message: { message_id: 999, chat: { id: 7, type: "private" } } },
      { data: `digest:${row.id}:delete:0` }, { data: `digest:${row.id}:outlook:-1` }, { data: "navtrial:news" }, { from: null },
    ]) expect((await callback(row, "overview", 0, changes)).status).toBe(200);
    expect(telegramCalls).toHaveLength(count);
    expect((await worker.fetch(new Request("https://cloud.test/telegram/webhook", { method: "POST", body: "{}" }), env)).status).toBe(401);
  });
  it("updates all today's digests and preserves the selected section", async () => {
    await command("/list"); await command("/list");
    const [first, second] = digestRows();
    await callback(first, "outlook"); await callback(second, "news");
    telegramCalls = [];
    await command("/waiting #1");
    const edits = telegramCalls.filter(c => c.method === "editMessageText");
    expect(edits).toHaveLength(2);
    expect(edits.find(c => c.body.message_id === first.message_id)?.body.text).toContain("💤 Waiting</i>");
    expect(edits.find(c => c.body.message_id === second.message_id)?.body.text).toContain("Completed: /waiting");
    expect((await storedPages(digestRows().find(r => r.id === second.id)!)).join("\n")).toContain("💤 Waiting</i>");
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("💤 Waiting</i>");
    await command("/reopen #1");
    expect((await storedPages(digestRows()[0])).join("\n")).not.toContain("<b>WAITING / FOLLOW-UP");
  });
  it("waits for Outlook and keeps partial successes and newer cloud states", async () => {
    await command("/list"); await ack({ event_id: (await poll()).job!.id });
    await command("/done #1 #2"); const job = (await poll()).job!;
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("<code>#1</code>");
    await ack({ event_id: job.id, task_id: 1 });
    let pages = (await storedPages(digestRows()[0])).join("\n");
    expect(pages).toContain("☑ <code>#1</code>"); expect(pages).toContain("<code>#2</code> <i>· ⏳ Done pending</i>");
    await command("/waiting #2");
    await ack({ event_id: job.id, task_id: 2 });
    pages = (await storedPages(digestRows()[0])).join("\n");
    expect(pages).toContain("💤 Waiting</i>"); expect(state(2)?.status).toBe("waiting");
    await command("/dismiss #2");
    await ack({ event_id: (await poll()).job!.id, task_id: 2 });
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("☒ <code>#2</code>");
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("0 remaining / 2");
  });
  it("shows read confirmation without completing the task", async () => {
    await command("/list"); await ack({ event_id: (await poll()).job!.id });
    await command("/read #1"); const job = (await poll()).job!;
    telegramCalls = [];
    await ack({ event_id: job.id, task_id: 1 });
    expect(telegramCalls.filter(c => c.method === "editMessageText")).toHaveLength(1);
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("☐ <code>#1</code> <i>· Email read</i>");
    expect(state(1)?.status).toBe("open");
  });
  it("refreshes snapshots and freezes earlier editions while retaining navigation", async () => {
    await command("/list"); const row = digestRows()[0];
    const data = fixture(); data.tasks[0].description = "Updated action";
    await publish(data);
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("Updated action");
    vi.setSystemTime(new Date("2026-11-02T02:00:00Z"));
    telegramCalls = [];
    await command("/waiting #1");
    expect(telegramCalls.filter(c => c.method === "editMessageText")).toHaveLength(0);
    await callback(row, "outlook");
    expect(telegramCalls.at(-1)?.body.text).toContain("Updated action");
    expect(telegramCalls.at(-1)?.body.text).not.toContain("WAITING / FOLLOW-UP");
  });
  it("retains dismissed rows and clamps the Outlook page when refreshed text reduces page count", async () => {
    const data = fixture(); data.tasks[0].description = "Long action ".repeat(1500);
    await publish(data); await command("/list");
    await callback(digestRows()[0], "outlook", 999);
    expect(digestRows()[0].page).toBeGreaterThan(0);
    await command("/dismiss #1");
    await ack({ event_id: database.db.prepare("SELECT id FROM cloud_events WHERE action='dismiss'").get()!.id, task_id: 1 });
    expect((await storedPages(digestRows()[0])).join("\n")).toContain("☒ <code>#1</code>");
    data.tasks[0].description = "Short action";
    await publish(data);
    expect(digestRows()[0].view).toBe("outlook"); expect(digestRows()[0].page).toBe(1);
    const edit = telegramCalls.filter(c => c.method === "editMessageText").at(-1)!;
    expect(edit.body.text).toContain("Daily brief — 2026-11-01 (Page 2 / 2)</b>");
    expect(JSON.stringify(edit.body.reply_markup)).not.toMatch(/Next/);
  });
  it("retries refresh failures and respects Telegram retry delays without losing mutations", async () => {
    await command("/list");
    await callback(digestRows()[0], "outlook");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false, parameters: { retry_after: 30 } }, { status: 429 })));
    await command("/waiting #1");
    expect(state(1)?.status).toBe("waiting"); expect(digestRows()[0].dirty).toBe(1);
    const fetcher = telegramMock(); vi.stubGlobal("fetch", fetcher);
    await poll(); expect(fetcher).not.toHaveBeenCalled();
    vi.setSystemTime(new Date(Date.now() + 31000));
    await poll();
    expect(digestRows()[0].dirty).toBe(0);
    expect(telegramCalls.some(c => c.method === "editMessageText" && c.body.text.includes("💤 Waiting</i>"))).toBe(true);
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_events WHERE action='waiting'").get()?.n).toBe(1);
  });
  it("retires a deleted digest without blocking other refreshes or replies", async () => {
    await command("/list"); await command("/list");
    const count = sent.length;
    const missing = digestRows()[0]; const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body));
      return url.endsWith("editMessageText") && body.message_id === missing.message_id
        ? Response.json({ ok: false, description: "Bad Request: message to edit not found" }, { status: 400 }) : normal(url, init);
    }));
    await command("/waiting #1");
    expect(digestRows().find(r => r.id === missing.id)?.disabled).toBe(1);
    expect(digestRows().find(r => r.id !== missing.id)?.dirty).toBe(0);
    expect(sent).toHaveLength(count);
    expect(telegramCalls.at(-1)?.method).toBe("deleteMessage");
  });
  it("resumes unsent interactive delivery without losing its stored message ID", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    await command("/list"); expect(digestRows()[0].message_id).toBeNull();
    vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", telegramMock());
    await poll(); const row = digestRows()[0]; expect(row.message_id).toBeGreaterThan(0);
    await poll();
    expect(telegramCalls.filter(c => c.method === "sendMessage" && c.body.reply_markup)).toHaveLength(1);
  });
  it("resumes legacy chunk delivery from its saved checkpoint after migration", async () => {
    const { seal } = await import("../src/cloud");
    await database.prepare("INSERT INTO cloud_outbox(id,sealed_chunks,next_chunk,created_at) VALUES(?,?,?,?)")
      .bind("legacy", await seal(env, "legacy", ["Already sent", "Remaining"]), 1, new Date().toISOString()).run();
    await poll(); expect(sent).toEqual(["Remaining"]);
  });
  it("migrates an existing queued delivery without changing its checkpoint", () => {
    const prior = new DatabaseSync(":memory:");
    try {
      for (const name of ["0001_initial.sql", "0002_cloud_bot.sql"]) prior.exec(readFileSync(new URL(`../migrations/${name}`, import.meta.url), "utf8"));
      prior.prepare("INSERT INTO cloud_outbox(id,sealed_chunks,next_chunk,created_at) VALUES(?,?,?,?)").run("pending", "encrypted", 2, "2026-10-31");
      prior.exec(readFileSync(new URL("../migrations/0003_interactive_digests.sql", import.meta.url), "utf8"));
      expect(prior.prepare("SELECT * FROM cloud_outbox").get()).toMatchObject({ id: "pending", sealed_chunks: "encrypted", next_chunk: 2, digest_id: null });
    } finally { prior.close(); }
  });
});

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
    expect(telegramCalls.find(call => call.method === "sendRichMessage")?.body.rich_message.html)
      .toContain("<code>#1</code> Private task 1 &lt;review&gt;");
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
    const pending = database.db.prepare("SELECT * FROM cloud_task_feedback WHERE delivered_at IS NULL").get();
    expect(pending).toBeDefined();
    expect(JSON.stringify(pending)).not.toContain("Private task");
    vi.setSystemTime(new Date(Date.now() + 6000));
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: true })));
    await command("/waiting #1", 1);
    expect(database.db.prepare("SELECT * FROM cloud_task_feedback WHERE delivered_at IS NULL").get()).toBeUndefined();
    expect(database.db.prepare("SELECT count(*) AS n FROM cloud_events").get()?.n).toBe(1);
  });
  it("generates a fresh daily cloud digest using changed states once per Singapore date", async () => {
    await command("/waiting #1");
    const start = sent.length;
    await runCloudScheduled(env, new Date("2026-10-20T23:59:00Z"));
    expect(sent.length).toBe(start);
    await runCloudScheduled(env, new Date("2026-10-21T00:00:00Z"));
    const count = sent.length;
    expect(sent.slice(start).join("\n")).toContain("<h2>📬 Daily brief — 2026-10-21</h2>");
    const pages = await storedPages(digestRows()[0]);
    expect(pages.join("\n")).toContain("<b>WAITING / FOLLOW-UP · 1</b>");
    expect(pages.join("\n")).toContain("<code>#1</code>");
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

describe("rich digests", () => {
  const richCalls = () => telegramCalls.filter(call => call.body.rich_message);
  const parts = (id = digestRows()[0].id) => database.db.prepare("SELECT * FROM interactive_digest_parts WHERE digest_id=? ORDER BY part_index").all(id);
  function largeFixture() {
    const data = fixture();
    data.tasks = Array.from({ length: 65 }, (_, index) => ({ ...data.tasks[0], id: index + 1, description: `Task ${index + 1} ` + "Review this action. ".repeat(65) }));
    return data;
  }
  it("renders every priority collapsed with counts, escaped tasks, visible health and collapsed activity", () => {
    const data = allSectionsFixture();
    data.health.last_run = { status: "failed" }; data.health.pending_or_failed = 2;
    data.tasks[0].sender_name = "A & B <team>";
    const rendered = renderRichDigest(data, new Date(), undefined, new Map(), "✅ Completed: /done · 1/1 confirmed");
    expect(rendered).toHaveLength(1);
    const html = rendered[0];
    expect(html.match(/<details>/g)).toHaveLength(7);
    expect(html).not.toMatch(/<details\s+open|callback_data|inline_keyboard/);
    for (const id of [1, 2, 3, 4, 5]) expect(html).toContain(`<code>#${id}</code>`);
    expect(html).toContain("OVERDUE · 1"); expect(html).toContain("WAITING / FOLLOW-UP · 1");
    expect(html).toContain("&lt;review&gt;"); expect(html).toContain("A &amp; B &lt;team&gt;");
    const visible = html.slice(0, html.indexOf("<details>"));
    expect(visible).toContain("5 open tasks"); expect(visible).toContain("latest mailbox synchronization did not finish");
    expect(visible).toContain("2</b> pending or failed"); expect(visible).toContain("✅ Completed");
    expect(visible).toContain("Last successful Outlook sync");
    expect(html).toContain("<details><summary>📊 Recent activity");
    expect(html).not.toContain("📰 News");
  });
  it("shows empty priorities as zero-count lines and includes enabled news collapsed", () => {
    const data = fixture(); data.tasks = [];
    const html = renderRichDigest(data, new Date(), undefined, new Map(), "", "Trial news")[0];
    expect(html).toContain("No active tasks."); expect(html).toContain("OVERDUE · 0");
    expect(html.match(/<details>/g)).toHaveLength(3);
    expect(html).toContain("<details><summary>📰 News</summary><p>Trial news</p></details>");
  });
  it("keeps Unicode and long fragments complete under the byte and block budgets", () => {
    const data = fixture();
    data.tasks = data.tasks.slice(0, 1);
    data.tasks[0].description = "<邮件&😀>".repeat(7000);
    data.tasks[0].sender_name = "发送者😀".repeat(2000);
    const rendered = renderRichDigest(data);
    expect(rendered.length).toBeGreaterThan(2);
    const all = rendered.join("");
    for (const character of ["邮", "件"]) expect(all.split(character).length - 1).toBe(7000);
    for (const character of ["发", "送", "者"]) expect(all.split(character).length - 1).toBe(2000);
    expect(all.match(/😀/g)).toHaveLength(9000);
    expect(all.match(/&lt;/g)).toHaveLength(7000);
    expect(all.match(/&amp;/g)).toHaveLength(7000);
    expect(all.match(/&gt;/g)).toHaveLength(7000);
    for (const [index, html] of rendered.entries()) {
      expect(new TextEncoder().encode(html).length).toBeLessThanOrEqual(30000);
      expect(html.match(/<(?:h2|p|details|summary)>/g)!.length).toBeLessThanOrEqual(400);
      expect(html.match(/<details>/g)?.length ?? 0).toBe(html.match(/<\/details>/g)?.length ?? 0);
      expect(html.match(/<p>/g)?.length ?? 0).toBe(html.match(/<\/p>/g)?.length ?? 0);
      expect(html).not.toContain("�");
      expect(html).toContain(`· ${index + 1} / ${rendered.length}`);
      expect(html).toContain("<code>#1</code>");
    }
    expect(all).toContain("(continued)");
  });
  it("also splits many short tasks at the block budget without losing task IDs", () => {
    const data = fixture();
    data.tasks = Array.from({ length: 900 }, (_, index) => ({ ...data.tasks[0], id: index + 1, description: "X", sender_name: "", deadline: null }));
    const rendered = renderRichDigest(data);
    expect(rendered.length).toBeGreaterThan(2);
    for (const html of rendered) expect(html.match(/<(?:h2|p|details|summary)>/g)!.length).toBeLessThanOrEqual(400);
    const ids = [...rendered.join("").matchAll(/<code>#(\d+)<\/code>/g)].map(match => Number(match[1]));
    expect(ids).toEqual(data.tasks.map(task => task.id));
  });
  it("sends a complete rich digest, ignores navigation callbacks and suppresses unchanged edits", async () => {
    await command("/list");
    const row = digestRows()[0];
    expect(row.format).toBe("rich"); expect(parts()).toHaveLength(1);
    expect(richCalls()).toHaveLength(1); expect(richCalls()[0].method).toBe("sendRichMessage");
    expect(richCalls()[0].body).not.toHaveProperty("reply_markup");
    expect(richCalls()[0].body).not.toHaveProperty("parse_mode");
    expect(richCalls()[0].body.rich_message.html).toContain("<code>#2</code>");
    expect(row.sealed_pages).not.toContain("Private task"); expect(row.sealed_layout).not.toContain("Private task");
    telegramCalls = [];
    await callback(row, "outlook"); await publish(); await poll();
    expect(richCalls()).toHaveLength(0);
    expect(telegramCalls.some(call => call.method === "answerCallbackQuery")).toBe(false);
    expect((await storedPages(digestRows()[0]))[0]).toContain("<details>");
  });
  it("retains section membership, pending feedback, checkmarks and quiet command cleanup", async () => {
    await command("/list"); await ack({ event_id: (await poll()).job!.id });
    await command("/waiting #1");
    let html = (await storedPages(digestRows()[0])).join("");
    expect(html).toContain("OVERDUE · 2"); expect(html).toContain("☐ <code>#1</code> <i>· 💤 Waiting</i>");
    await command("/reopen #1"); await command("/read #1");
    let event = (await poll()).job!; await ack({ event_id: event.id, task_id: 1 });
    expect((await storedPages(digestRows()[0])).join("")).toContain("Email read</i>");
    await command("/done #1 #2", 900);
    expect(telegramCalls.at(-1)?.method).toBe("deleteMessage");
    html = (await storedPages(digestRows()[0])).join("");
    expect(html).toContain("Done pending</i>");
    expect(html.indexOf("Pending Outlook confirmation")).toBeLessThan(html.indexOf("<details>"));
    event = (await poll()).job!;
    await ack({ event_id: event.id, task_id: 1 }); await ack({ event_id: event.id, task_id: 2 });
    html = (await storedPages(digestRows()[0])).join("");
    expect(html).toContain("☑ <code>#1</code>"); expect(html).toContain("☑ <code>#2</code>");
    expect(html).toContain("0 remaining / 2"); expect(html).not.toContain("Done pending");
    expect(digestRows()[0].dirty).toBe(0);
  });
  it("saves partial sends and resumes remaining parts without resending successful parts", async () => {
    await publish(largeFixture());
    const normal = telegramMock(); let attempts = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      if (url.endsWith("sendRichMessage") && ++attempts === 2) return Response.json({ ok: false }, { status: 503 });
      return normal(url, init);
    }));
    await command("/list");
    const row = digestRows()[0];
    expect(row.dirty).toBe(1); expect(row.message_id).toBeGreaterThan(0); expect(parts()).toHaveLength(1);
    vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", normal);
    await poll();
    const saved = await storedPages(digestRows()[0]);
    expect(saved.length).toBeGreaterThan(2); expect(parts()).toHaveLength(saved.length);
    expect(richCalls().filter(call => call.method === "sendRichMessage")).toHaveLength(saved.length);
    expect(richCalls().filter(call => call.method === "editMessageText")).toHaveLength(0);
    expect(digestRows()[0].dirty).toBe(0); expect(digestRows()[0].message_id).toBe(row.message_id);
    const count = richCalls().length; await poll(); expect(richCalls()).toHaveLength(count);
  });
  it("edits only changed parts and retries failed edits without claiming success", async () => {
    const data = largeFixture(); await publish(data); await command("/list");
    const before = parts().map(part => part.payload_sha256);
    data.tasks.at(-1)!.description = data.tasks.at(-1)!.description.replace("Review", "Check!");
    const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("editMessageText")
      ? Response.json({ ok: false }, { status: 503 }) : normal(url, init)));
    await publish(data);
    expect(digestRows()[0].dirty).toBe(1); expect(parts().map(part => part.payload_sha256)).toEqual(before);
    telegramCalls = []; vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", normal);
    await poll();
    expect(richCalls()).toHaveLength(1); expect(richCalls()[0].method).toBe("editMessageText");
    expect(richCalls()[0].body.message_id).toBe(parts().at(-1)!.message_id);
    expect(digestRows()[0].dirty).toBe(0);
  });
  it("keeps command cleanup pending until every continuation edit succeeds", async () => {
    await publish(largeFixture()); await command("/list"); await ack({ event_id: (await poll()).job!.id });
    const last = parts().at(-1)!.message_id; const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("editMessageText") && JSON.parse(String(init.body)).message_id === last
      ? Response.json({ ok: false }, { status: 503 }) : normal(url, init)));
    telegramCalls = []; await command("/waiting #65", 950);
    expect(digestRows()[0].dirty).toBe(1);
    expect(telegramCalls.some(call => call.method === "deleteMessage")).toBe(false);
    vi.setSystemTime(new Date(Date.now() + 6000)); vi.stubGlobal("fetch", normal); await poll();
    expect(digestRows()[0].dirty).toBe(0);
    expect(telegramCalls.at(-1)?.body.message_id).toBe(950);
  });
  it("removes surplus parts after replacement succeeds and retries rate-limited cleanup", async () => {
    const data = largeFixture(); await publish(data); await command("/list");
    const old = parts(); expect(old.length).toBeGreaterThan(2);
    data.tasks.forEach(task => { task.description = "Short action"; });
    const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("deleteMessage")
      ? Response.json({ ok: false, parameters: { retry_after: 30 } }, { status: 429 }) : normal(url, init)));
    telegramCalls = []; await publish(data);
    expect(digestRows()[0].dirty).toBe(1); expect(parts()).toHaveLength(old.length);
    expect(richCalls()).toHaveLength(1);
    const count = telegramCalls.length; await poll(); expect(telegramCalls).toHaveLength(count);
    vi.setSystemTime(new Date(Date.now() + 31000)); vi.stubGlobal("fetch", normal); await poll();
    expect(parts()).toHaveLength(1); expect(digestRows()[0].dirty).toBe(0);
    expect(richCalls()).toHaveLength(1);
    expect(telegramCalls.filter(call => call.method === "deleteMessage").map(call => call.body.message_id))
      .toEqual(old.slice(1).map(part => part.message_id));
  });
  it("tolerates an already-deleted surplus continuation", async () => {
    const data = largeFixture(); await publish(data); await command("/list");
    data.tasks.forEach(task => { task.description = "Short action"; });
    const normal = telegramMock();
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => url.endsWith("deleteMessage")
      ? Response.json({ ok: false, description: "Bad Request: message to delete not found" }, { status: 400 }) : normal(url, init)));
    await publish(data);
    expect(parts()).toHaveLength(1); expect(digestRows()[0].dirty).toBe(0); expect(digestRows()[0].disabled).toBe(0);
  });
  it("keeps a failed first send frozen when retrying on the next day", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: false }, { status: 503 })));
    await command("/list");
    const row = digestRows()[0]; const saved = await storedPages(row);
    vi.setSystemTime(new Date(Date.now() + 86400000)); vi.stubGlobal("fetch", telegramMock());
    const data = fixture(); data.tasks[0].description = "Next-day change"; await publish(data); await poll();
    expect(richCalls()[0].body.rich_message.html).toBe(saved[0]);
    expect(richCalls()[0].body.rich_message.html).toContain("Private task 1");
    expect(richCalls()[0].body.rich_message.html).not.toContain("Next-day change");
  });
  it("delivers missing-snapshot warnings and frozen news without navigation", async () => {
    await database.prepare("DELETE FROM cloud_snapshot").run(); env.NEWS_TRIAL_ENABLED = "true";
    const normal = telegramMock(); let requests = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      if (url.startsWith("https://hacker-news.firebaseio.com/")) { requests++; throw new Error("unavailable"); }
      return normal(url, init);
    }));
    await command("/list");
    expect(richCalls()[0].body.rich_message.html).toContain("No bridge snapshot yet");
    expect(richCalls()[0].body.rich_message.html).toContain("News could not be loaded");
    const initialRequests = requests;
    await publish();
    expect((await storedPages(digestRows()[0]))[0]).toContain("Private task 1");
    expect(requests).toBe(initialRequests);
    await command("/list"); expect(requests).toBe(initialRequests);
    expect(digestRows().every(row => row.sealed_news && !row.sealed_news.includes("News could not"))).toBe(true);
  });
  it("defaults migrated records to legacy and cascades continuation-record retention cleanup", async () => {
    await database.prepare("INSERT INTO interactive_digests(id,digest_date,sealed_pages) VALUES('legacy','2026-11-01','encrypted')").run();
    expect(digestRows().find(row => row.id === "legacy")!.format).toBe("legacy");
    await database.prepare("DELETE FROM interactive_digests WHERE id='legacy'").run();
    await command("/list"); await ack({ event_id: (await poll()).job!.id });
    const id = digestRows()[0].id;
    expect(parts(id)).toHaveLength(1);
    vi.setSystemTime(new Date(Date.now() + 31 * 86400000)); await runCloudScheduled(env);
    expect(parts(id)).toHaveLength(0);
  });
});

describe("command and rendering contracts", () => {
  it("puts Outlook pagination in the daily heading without a second Outlook heading", () => {
    const row = { id: crypto.randomUUID(), digest_date: "2026-11-02", view: "outlook", page: 1 };
    const pages = ["Overview content", "First section", "Second section", "Third section", "Fourth section"];
    expect(digestMessage(row, pages).payload.text).toBe("📬 <b>Daily brief — 2026-11-02 (Page 2 / 4)</b>\n\nSecond section");
    for (const view of ["overview", "news"]) expect(digestMessage({ ...row, view }, pages).payload.text.split("\n")[0])
      .toBe("📬 <b>Daily brief — 2026-11-02</b>");
  });
  it("separates immediate urgency sections and combines only the two lower-priority sections", () => {
    const pages = renderDigest(allSectionsFixture());
    expect(pages).toHaveLength(6);
    expect(pages[5]).toContain("INFORMATIONAL EMAILS · LAST 72 HOURS");
    expect(pages[0]).toContain("OPEN ACTIONS");
    expect(pages[1]).toContain("<b>OVERDUE · 1</b>");
    expect(pages[2]).toContain("<b>URGENT / DUE TODAY · 1</b>");
    expect(pages[3]).toContain("<b>DUE WITHIN 7 DAYS · 1</b>");
    expect(pages[4]).toContain("<b>OTHER OPEN ACTIONS · 1</b>");
    expect(pages[4]).toContain("<b>WAITING / FOLLOW-UP · 1</b>");
    for (let index = 1; index <= 3; index++) {
      expect([...pages[index].matchAll(/^[🔴🟠🟡⚪💤]+ <b>/gmu)]).toHaveLength(1);
      expect([...pages[index].matchAll(/<code>#(\d+)<\/code>/g)].map(match => Number(match[1]))).toEqual([index]);
    }
    expect(pages[3]).not.toContain("OTHER OPEN ACTIONS");
    expect(pages[3]).not.toContain("WAITING / FOLLOW-UP");
  });
  it("separates lower-priority sections when they cannot fit together", () => {
    const data = allSectionsFixture(); data.tasks = data.tasks.slice(3);
    data.tasks.forEach(task => { task.description = "Long action ".repeat(180); });
    const pages = renderDigest(data);
    expect(pages).toHaveLength(4);
    expect(pages[1]).toContain("OTHER OPEN ACTIONS"); expect(pages[1]).not.toContain("WAITING / FOLLOW-UP");
    expect(pages[2]).toContain("WAITING / FOLLOW-UP"); expect(pages[2]).not.toContain("OTHER OPEN ACTIONS");
  });
  it("starts the next urgency section after all continuation pages of a large section", () => {
    const data = allSectionsFixture();
    data.tasks[0].description = "<overdue & action> 😀".repeat(500);
    const pages = renderDigest(data);
    const urgent = pages.findIndex(page => page.includes("<b>URGENT / DUE TODAY ·"));
    expect(urgent).toBeGreaterThan(2);
    for (const page of pages.slice(1, urgent)) {
      expect(page).toContain("<b>OVERDUE · 1</b> — PART ");
      expect(page).not.toContain("URGENT / DUE TODAY");
      expect(page).not.toContain("DUE WITHIN");
    }
    expect(pages[urgent + 1]).toContain("<b>DUE WITHIN 7 DAYS · 1</b>");
    expect(pages.join("\n").match(/&lt;overdue &amp; action&gt; 😀/g)).toHaveLength(500);
  });
  it.each(["other", "waiting", "both"])("keeps oversized %s sections on their own continuation pages", (large) => {
    const data = allSectionsFixture(); data.tasks = data.tasks.slice(3);
    if (large !== "waiting") data.tasks[0].description = "<long other & action> 😀".repeat(400);
    if (large !== "other") data.tasks[1].description = "<long waiting & action> 😀".repeat(400);
    const pages = renderDigest(data);
    for (const page of pages.slice(1, -1)) {
      expect([...page.matchAll(/^[🔴🟠🟡⚪💤]+ <b>/gmu)]).toHaveLength(1);
      if (page.includes(large === "waiting" ? "WAITING / FOLLOW-UP" : "OTHER OPEN ACTIONS")) expect(page).toContain("— PART ");
    }
    const row = { id: crypto.randomUUID(), digest_date: "2026-11-01", view: "outlook", page: 0 };
    for (let page = 0; page < pages.length - 1; page++) {
      expect(digestMessage({ ...row, page }, pages, "⏳ Pending Outlook confirmation: /done · 0/2 confirmed").payload.text.length).toBeLessThanOrEqual(3400);
    }
  });
  it("retains completed rows and section boundaries when rendering an existing digest", () => {
    const data = allSectionsFixture(); const groups = digestGroups(renderDigest(data));
    data.tasks[0].status = "done"; data.tasks[1].status = "dismissed"; data.tasks[3].status = "waiting";
    const notes = new Map([[3, " <i>· ⏳ Done pending</i>"], [4, " <i>· 💤 Waiting</i>"]]);
    const pages = renderDigest(data, new Date(), groups, notes);
    expect(pages).toHaveLength(6);
    expect(pages[1]).toContain("OVERDUE · 0 remaining / 1"); expect(pages[1]).toContain("☑ <code>#1</code>");
    expect(pages[2]).toContain("URGENT / DUE TODAY · 0 remaining / 1"); expect(pages[2]).toContain("☒ <code>#2</code>");
    expect(pages[3]).toContain("Done pending</i>");
    expect(pages[4]).toContain("OTHER OPEN ACTIONS · 1"); expect(pages[4]).toContain("💤 Waiting</i>");
  });
  it("omits empty sections without filling another urgency page with lower-priority tasks", () => {
    const data = allSectionsFixture(); data.tasks = data.tasks.filter(task => [3, 5].includes(task.id));
    const pages = renderDigest(data);
    expect(pages).toHaveLength(4);
    expect(pages[1]).toContain("DUE WITHIN 7 DAYS"); expect(pages[1]).not.toContain("WAITING / FOLLOW-UP");
    expect(pages[2]).toContain("WAITING / FOLLOW-UP");
    data.tasks = [];
    expect(renderDigest(data)[1]).toContain("No active tasks.");
  });
  it("hides Outlook navigation at the edges and keeps every rendered view within budget", () => {
    const data = fixture(); data.tasks[0].description = "<>&😀".repeat(3000);
    data.tasks[0].sender_name = "<sender&>".repeat(1000);
    const pages = renderDigest(data);
    const row = { id: crypto.randomUUID(), digest_date: "2026-11-01", view: "outlook", page: 0 };
    for (let index = 0; index < pages.length - 1; index++) {
      const { payload } = digestMessage({ ...row, page: index }, pages);
      expect(payload.text.length).toBeLessThanOrEqual(3400);
      const buttons = payload.reply_markup.inline_keyboard.flat();
      expect(buttons.some(b => b.text.includes("Previous"))).toBe(index > 0);
      expect(buttons.some(b => b.text.includes("Next"))).toBe(index < pages.length - 2);
      expect(buttons.every(b => new TextEncoder().encode(b.callback_data).length <= 64)).toBe(true);
    }
    for (const view of ["overview", "news"]) expect(digestMessage({ ...row, view }, pages).payload.text.length).toBeLessThanOrEqual(3400);
    const single = digestMessage(row, ["Overview", "One action"]).payload;
    expect(JSON.stringify(single.reply_markup)).not.toMatch(/Previous|Next/);
    data.tasks.forEach(task => { task.status = "done"; });
    expect(digestMessage(row, renderDigest(data)).payload.text).toContain("No active tasks.");
  });
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
