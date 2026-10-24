import {
  decryptEnvelope, escapeHtml, singaporeClock, validateEnvelope,
  type BundlePayload, type Env,
} from "./index";

import { outbox, digestOutbox, taskFeedback, flush } from "./cloud_ui";

type Status = "open" | "waiting" | "done" | "dismissed";
export interface CloudTask {
  id: number; status: Status; description: string; deadline: string | null; urgency: string;
  sender_name: string; sender_address: string; subject: string; preview?: string;
}
export interface BridgeSnapshot {
  dataset_id: string;
  tasks: CloudTask[];
  horizon_days: number;
  health: { last_sync: string | null; last_run: { status: string } | null; pending_or_failed: number };
  counts: Record<string, number | null>;
  processed_telegram_updates?: number[];
}
interface Event {
  id: number; action: string; task_ids: string; completed_ids: string; retry_limit: number;
  status: string; created_at: string;
}
interface Command { action: string; ids: number[]; option?: string; limit: number }
const statuses = ["open", "waiting", "done", "dismissed"];
const mailboxActions = ["sync", "list", "retry", "read", "done", "dismiss"];
const help = [
  "☁️ <b>OUTLOOK TRIAGE · CLOUD BOT</b>",
  "/list — latest cloud digest; queue a mailbox sync",
  "/sync — queue a mailbox sync", "/retry [1-100] — queue classification retries",
  "/status — freshness, bridge and queue health", "/tasks [open|waiting|done|dismissed|all]",
  "/waiting #id [...] — set cloud tasks waiting", "/reopen #id [...] — reopen cloud tasks",
  "/read #id [...] — queue marking original emails read",
  "/done #id [...] — mark done after Outlook confirms read",
  "/dismiss #id [...] — dismiss after Outlook confirms read",
  "/show #id — previously cached body preview", "/help — command reference",
  "Mailbox work waits until the Windows bridge connects. Scheduled sync: every four hours.",
].join("\n");

export function parseCloudCommand(text: string): Command | null {
  if (text.length > 4096) return null;
  const match = /^\/(list|sync|retry|status|tasks|read|done|dismiss|waiting|reopen|show|help|start)(?:@[A-Za-z0-9_]+)?(?:\s+(.+?))?\s*$/i.exec(text.trim());
  if (!match) return null;
  const action = match[1].toLowerCase() === "start" ? "help" : match[1].toLowerCase();
  const arg = match[2];
  const base = { action, ids: [] as number[], limit: 50 };
  if (["help", "list", "sync", "status"].includes(action)) return arg ? null : base;
  if (action === "tasks") {
    const option = (arg ?? "all").toLowerCase();
    return [...statuses, "all"].includes(option) ? { ...base, option } : null;
  }
  if (action === "retry") {
    const limit = arg ? Number(arg) : 50;
    return (!arg || /^\d+$/.test(arg)) && Number.isInteger(limit) && limit >= 1 && limit <= 100
      ? { ...base, limit } : null;
  }
  const parts = arg?.split(/\s+/) ?? [];
  if (!parts.length || parts.length > 100 || parts.some(p => !/^#?[1-9]\d*$/.test(p))) return null;
  const ids = [...new Set(parts.map(p => Number(p.replace(/^#/, ""))))];
  if (ids.some(id => !Number.isSafeInteger(id)) || (action === "show" && ids.length !== 1)) return null;
  return { ...base, ids };
}

export function validateBridge(value: unknown): BridgeSnapshot {
  const data = value as BridgeSnapshot;
  if (!data || !/^[a-f0-9-]{36}$/.test(data.dataset_id) || !Array.isArray(data.tasks)
    || !Number.isInteger(data.horizon_days) || data.horizon_days < 1 || data.horizon_days > 365
    || !data.health || !Number.isInteger(data.health.pending_or_failed) || !data.counts) {
    throw new Error("Invalid bridge snapshot");
  }
  const ids = new Set<number>();
  if (data.processed_telegram_updates !== undefined && (!Array.isArray(data.processed_telegram_updates)
    || data.processed_telegram_updates.length > 1000 || data.processed_telegram_updates.some(id => !Number.isSafeInteger(id)))) {
    throw new Error("Invalid legacy update receipts");
  }
  for (const task of data.tasks) {
    if (!Number.isSafeInteger(task.id) || task.id < 1 || ids.has(task.id) || !statuses.includes(task.status)
      || [task.description, task.urgency, task.sender_name, task.sender_address, task.subject].some(v => typeof v !== "string")
      || (task.deadline !== null && (!/^\d{4}-\d{2}-\d{2}$/.test(task.deadline) || Number.isNaN(Date.parse(task.deadline))))
      || (task.preview !== undefined && (typeof task.preview !== "string" || task.preview.length > 12000))) {
      throw new Error("Invalid bridge task");
    }
    ids.add(task.id);
  }
  return data;
}

// Escape after splitting: long bodies cannot split an HTML entity or tag.
export function textChunks(text: string, limit = 3400): string[] {
  const chunks: string[] = [];
  let chunk = "";
  for (const character of text || "(empty)") {
    const escaped = escapeHtml(character);
    if (chunk.length + escaped.length > limit) { chunks.push(chunk); chunk = ""; }
    chunk += escaped;
  }
  if (chunk) chunks.push(chunk);
  return chunks;
}
async function withLock(env: Env, work: () => Promise<Response>): Promise<Response> {
  const owner = crypto.randomUUID();
  const lock = await env.DB.prepare("UPDATE cloud_lock SET owner=?,expires_ms=? WHERE singleton=1 AND expires_ms<? RETURNING owner")
    .bind(owner, Date.now() + 120000, Date.now()).first();
  if (!lock) return Response.json({ error: "Cloud bot busy; retry shortly" }, { status: 503 });
  try { return await work(); }
  finally { await env.DB.prepare("UPDATE cloud_lock SET expires_ms=0 WHERE owner=?").bind(owner).run(); }
}

async function snapshot(env: Env): Promise<BridgeSnapshot | null> {
  const row = await env.DB.prepare("SELECT * FROM cloud_snapshot WHERE singleton=1").first();
  if (!row) return null;
  const payload = await decryptEnvelope(JSON.parse(String(row.envelope_json)), env.RELAY_ENCRYPTION_KEY);
  const data = validateBridge(payload.bridge);
  const states = await env.DB.prepare("SELECT id,status,revision FROM cloud_tasks ORDER BY id").all<{ id: number; status: Status }>();
  const lookup = new Map(states.results.map(t => [t.id, t.status]));
  return { ...data, tasks: data.tasks.map(t => ({ ...t, status: lookup.get(t.id) ?? t.status })) };
}

function packDigestBlocks(blocks: string[], limit = 3400): string[] {
  const messages: string[] = [];
  let current = "";
  for (const block of blocks) {
    const candidate = current ? `${current}\n\n${block}` : block;
    // Reserve space for pending/error labels and remaining-work totals in future edits.
    const editSize = candidate.length + [...candidate.matchAll(/^☐ <code>#/gm)].length * 72 + 160;
    if (current && editSize > limit) { messages.push(current); current = block; }
    else current = candidate;
  }
  if (current) messages.push(current);
  return messages;
}

function digestTaskParts(task: CloudTask, today: string): string[] {
  const id = `<code>#${task.id}</code>`;
  const parts = textChunks(task.description, 1800).map((description, index) =>
    `${index ? "↳" : "☐"} ${id}${index ? " <i>(continued)</i>" : ""} ${description}`);
  const metadata: string[] = [];
  if (task.deadline) {
    const label = task.deadline === today ? "today" : new Intl.DateTimeFormat("en-GB", {
      day: "numeric", month: "short", timeZone: "UTC",
    }).format(new Date(`${task.deadline}T00:00:00Z`));
    metadata.push(`Due <b>${escapeHtml(label)}</b>`);
  }
  const sender = task.sender_name || task.sender_address;
  if (sender) metadata.push(...textChunks(sender, 1000).map(piece => `<i>${piece}</i>`));
  for (const line of metadata) {
    const last = parts.length - 1;
    if (parts[last].length + line.length + 2 <= 3000) parts[last] += `\n ${line}`;
    else parts.push(`↳ ${id}\n ${line}`);
  }
  return parts;
}

function digestSection(heading: string, emoji: string, tasks: CloudTask[], today: string): string[] {
  if (!tasks.length) return [];
  const title = `${emoji} <b>${escapeHtml(heading.toUpperCase())} · ${tasks.length}</b>`;
  // Split only at complete rendered fragments. Each fragment has balanced HTML,
  // including repeated task IDs when a long description needs continuation.
  const groups = packDigestBlocks(tasks.flatMap(task => digestTaskParts(task, today)), 3400 - title.length - 60);
  return groups.map((group, index) => `${title}${groups.length > 1 ? ` — PART ${index + 1} OF ${groups.length}` : ""}\n\n${group}`);
}

export function renderDigest(data: BridgeSnapshot, now = new Date()): string[] {
  const today = singaporeClock(now).date;
  const horizon = new Date(Date.parse(today) + data.horizon_days * 86400000).toISOString().slice(0, 10);
  const groups: [string, CloudTask[]][] = [
    ["Overdue", []], ["Urgent / due today", []], [`Due within ${data.horizon_days} days`, []],
    ["Other open actions", []], ["Waiting / follow-up", []],
  ];
  for (const task of [...data.tasks].sort((a, b) => (a.deadline ?? "9999").localeCompare(b.deadline ?? "9999") || a.id - b.id)) {
    let group: number;
    if (task.status === "waiting") group = 4;
    else if (task.status !== "open") continue;
    else if (task.deadline && task.deadline < today) group = 0;
    else if (task.urgency === "urgent" || task.deadline === today) group = 1;
    else if (task.deadline && task.deadline <= horizon) group = 2;
    else group = 3;
    groups[group][1].push(task);
  }
  const emojis = ["🔴", "🟠", "🟡", "⚪", "💤"];
  const summary = [
    `📬 <b>Inbox brief — ${today}</b>`,
    ["<b>OPEN ACTIONS</b>", ...groups.map(([heading, tasks], index) =>
      `${emojis[index]} ${escapeHtml(heading)}: <b>${tasks.length}</b>`)].join("\n"),
  ];
  const sync = ["🔄 <b>SYNCHRONIZATION</b>",
    ...textChunks(data.health.last_sync ?? "never", 2000).map(value => `Last successful Outlook sync: <b>${value}</b>`),
    "<i>Mailbox data is a bridge snapshot; cloud task states are current.</i>"];
  if (data.health.last_run?.status !== "success") sync.push("⚠️ <b>Warning:</b> latest mailbox synchronization did not finish successfully.");
  if (data.health.pending_or_failed) sync.push(`⚠️ <b>${data.health.pending_or_failed}</b> pending or failed classifications.`);
  const activity = ["📊 <b>LAST 24 HOURS AT SNAPSHOT PREPARATION</b>"];
  for (const [name, key] of [["Informational", "information_count"], ["Newsletters", "newsletter_count"], ["Automated", "automated_count"], ["Explicitly skipped", "skipped_count"]]) {
    activity.push(`• ${name}: <b>${escapeHtml(String(data.counts[key] ?? 0))}</b>`);
  }
  summary.push(activity.join("\n"), ...sync);
  const messages = packDigestBlocks(summary);
  const sections = groups.map(([heading, tasks], index) => digestSection(heading, emojis[index], tasks, today));
  let compact: string[] = [];
  for (const parts of sections) {
    if (parts.length > 1) {
      messages.push(...packDigestBlocks(compact), ...parts);
      compact = [];
    } else compact.push(...parts);
  }
  return [...messages, ...packDigestBlocks(compact)];
}

async function webhook(request: Request, env: Env): Promise<Response> {
  if (!env.TELEGRAM_WEBHOOK_SECRET || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const update = await request.json() as { update_id?: number; message?: { message_id?: number; date?: number; chat?: { id?: number; type?: string }; from?: { is_bot?: boolean }; text?: string } };
  const message = update.message;
  if (!Number.isSafeInteger(update.update_id) || !message || message.chat?.type !== "private"
    || String(message.chat?.id) !== env.TELEGRAM_CHAT_ID || message.from?.is_bot || typeof message.text !== "string") return Response.json({ ok: true });
  return withLock(env, async () => {
    const key = `tg:${update.update_id}`;
    if (await env.DB.prepare("SELECT id FROM cloud_events WHERE dedupe_key=?").bind(key).first()) {
      await flush(env);
      return Response.json({ ok: true });
    }
    const cmd = parseCloudCommand(message.text!);
    const data = await snapshot(env);
    if (data?.processed_telegram_updates?.includes(update.update_id!)) return Response.json({ ok: true });
    const ids = cmd?.ids ?? [];
    const missing = ids.filter(id => !data?.tasks.some(t => t.id === id));
    let chunks: string[];
    let pending = false;
    if (!cmd) chunks = ["Unknown command or invalid arguments. Use /help."];
    else if (cmd.action === "help") chunks = [help];
    else if (!data) chunks = ["No bridge snapshot yet. Run outlook-triage bridge-poll on Windows to initialize cloud state."];
    else if (missing.length) chunks = textChunks(`Tasks not found: ${missing.map(id => `#${id}`).join(", ")}. No changes made.`);
    else if (cmd.action === "status") {
      const count = await env.DB.prepare("SELECT COUNT(*) AS count FROM cloud_events WHERE status='pending'").first<{ count: number }>();
      const bridge = await env.DB.prepare("SELECT last_seen FROM cloud_snapshot WHERE singleton=1").first<{ last_seen: string }>();
      const totals = statuses.map(s => `${s}: ${data.tasks.filter(t => t.status === s).length}`).join("\n");
      chunks = textChunks(`Cloud bot is available.\nLast bridge contact: ${bridge?.last_seen ?? "never"}\nLast successful Outlook sync: ${data.health.last_sync ?? "never"}\nLatest sync: ${data.health.last_run?.status ?? "none"}\nPending or failed classifications: ${data.health.pending_or_failed}\nQueued mailbox commands: ${count?.count ?? 0}\n${totals}`);
    } else if (cmd.action === "tasks") {
      const tasks = data.tasks.filter(t => cmd.option === "all" || t.status === cmd.option);
      chunks = textChunks(`Tasks · ${cmd.option}\n${tasks.map(t => `#${t.id} [${t.status}] [${t.urgency}] ${t.description}${t.deadline ? ` — due ${t.deadline}` : ""}`).join("\n\n") || "No matching tasks."}`);
    } else if (cmd.action === "show") {
      const task = data.tasks.find(t => t.id === ids[0])!;
      chunks = task.preview === undefined ? ["No cached body preview. Enable bridge preview caching on Windows and reconnect."]
        : textChunks(`Email preview · ${task.subject}\nCached at the last bridge publication; bounded preview.\n\n${task.preview || "(empty body)"}`);
    } else if (["waiting", "reopen"].includes(cmd.action)) {
      chunks = textChunks(`Cloud tasks ${ids.map(id => `#${id}`).join(", ")} marked ${cmd.action === "waiting" ? "waiting" : "open"}.`);
    } else {
      pending = mailboxActions.includes(cmd.action);
      chunks = [`⏳ /${cmd.action} queued. It will complete when the Windows Outlook bridge connects.`];
    }
    const event = env.DB.prepare("INSERT INTO cloud_events(dedupe_key,action,task_ids,retry_limit,status,created_at) VALUES(?,?,?,?,?,?)")
      .bind(key, cmd?.action ?? "invalid", JSON.stringify(ids), cmd?.limit ?? 50, pending ? "pending" : "done", new Date().toISOString());
    const statements = [event];
    if (cmd && data && !missing.length && ["waiting", "reopen"].includes(cmd.action)) {
      for (const id of ids) statements.push(env.DB.prepare("UPDATE cloud_tasks SET status=?,revision=(SELECT id FROM cloud_events WHERE dedupe_key=?) WHERE id=?")
        .bind(cmd.action === "waiting" ? "waiting" : "open", key, id));
    }
    if (cmd && data && !missing.length && ["read", "done", "dismiss", "waiting", "reopen"].includes(cmd.action)) {
      statements.push(await taskFeedback(env, key, ids,
        { message: message.message_id!, date: message.date! }));
    } else if (cmd?.action === "list" && data) {
      statements.push(...await digestOutbox(env, renderDigest(data), chunks));
    } else statements.push(await outbox(env, chunks));
    await env.DB.batch(statements);
    await flush(env);
    return Response.json({ ok: true });
  });
}

async function acceptSnapshot(request: Request, env: Env): Promise<Response> {
  const envelope = validateEnvelope(await request.json());
  const payload: BundlePayload = await decryptEnvelope(envelope, env.RELAY_ENCRYPTION_KEY);
  const data = validateBridge(payload.bridge);
  return withLock(env, async () => {
    const previous = await env.DB.prepare("SELECT * FROM cloud_snapshot WHERE singleton=1").first();
    if (previous && previous.dataset_id !== data.dataset_id) return Response.json({ error: "Different local database; explicit migration required" }, { status: 409 });
    if (previous && Number(previous.generated_ms) > Date.parse(envelope.generated_at)) return Response.json({ error: "Older snapshot" }, { status: 409 });
    const statements = [env.DB.prepare(`INSERT INTO cloud_snapshot(singleton,dataset_id,envelope_json,generated_ms,last_seen) VALUES(1,?,?,?,?)
      ON CONFLICT(singleton) DO UPDATE SET envelope_json=excluded.envelope_json,generated_ms=excluded.generated_ms,last_seen=excluded.last_seen`)
      .bind(data.dataset_id, JSON.stringify(envelope), Date.parse(envelope.generated_at), new Date().toISOString())];
    for (const task of data.tasks) statements.push(env.DB.prepare("INSERT OR IGNORE INTO cloud_tasks(id,status) VALUES(?,?)").bind(task.id, task.status));
    // Batch in one transaction: a task import and its encrypted details stay consistent.
    await env.DB.batch(statements);
    return Response.json({ status: "stored", bundle_id: envelope.bundle_id });
  });
}

async function bridgePoll(request: Request, env: Env): Promise<Response> {
  const dataset = request.headers.get("X-Bridge-Dataset");
  return withLock(env, async () => {
    const row = await env.DB.prepare("SELECT dataset_id FROM cloud_snapshot WHERE singleton=1").first();
    if (!row || row.dataset_id !== dataset) return Response.json({ error: "Publish matching bridge snapshot first" }, { status: 409 });
    await env.DB.prepare("UPDATE cloud_snapshot SET last_seen=? WHERE singleton=1").bind(new Date().toISOString()).run();
    const states = await env.DB.prepare("SELECT id,status,revision FROM cloud_tasks ORDER BY id").all();
    const job = await env.DB.prepare("SELECT * FROM cloud_events WHERE status='pending' ORDER BY id LIMIT 1").first<Event>();
    await flush(env);
    return Response.json({ tasks: states.results, job: job ? { ...job, task_ids: JSON.parse(job.task_ids), completed_ids: JSON.parse(job.completed_ids) } : null });
  });
}

async function bridgeAck(request: Request, env: Env): Promise<Response> {
  const body = await request.json() as { event_id: number; task_id?: number; failed?: boolean; counts?: Record<string, number> };
  if (!Number.isSafeInteger(body.event_id) || (body.task_id !== undefined && !Number.isSafeInteger(body.task_id))) return Response.json({ error: "Invalid acknowledgement" }, { status: 400 });
  return withLock(env, async () => {
    const row = await env.DB.prepare("SELECT dataset_id FROM cloud_snapshot WHERE singleton=1").first();
    if (!row || row.dataset_id !== request.headers.get("X-Bridge-Dataset")) return Response.json({ error: "Wrong bridge dataset" }, { status: 409 });
    const event = await env.DB.prepare("SELECT * FROM cloud_events WHERE id=?").bind(body.event_id).first<Event>();
    if (!event) return Response.json({ error: "Unknown command" }, { status: 404 });
    if (event.status !== "pending") return Response.json({ ok: true });
    const ids: number[] = JSON.parse(event.task_ids);
    const completed: number[] = JSON.parse(event.completed_ids);
    const statements: D1PreparedStatement[] = [];
    if (body.task_id !== undefined) {
      if (!ids.includes(body.task_id) || !["read", "done", "dismiss"].includes(event.action)) return Response.json({ error: "Task not in command" }, { status: 400 });
      if (completed.includes(body.task_id) && !body.failed) {
        await flush(env);
        return Response.json({ ok: true });
      }
      if (!completed.includes(body.task_id)) completed.push(body.task_id);
      if (event.action !== "read") statements.push(env.DB.prepare("UPDATE cloud_tasks SET status=?,revision=? WHERE id=? AND revision<=?")
        .bind(event.action === "done" ? "done" : "dismissed", event.id, body.task_id, event.id));
      statements.push(env.DB.prepare("UPDATE cloud_events SET completed_ids=? WHERE id=?").bind(JSON.stringify(completed), event.id));
    }
    const finished = body.failed || (ids.length ? completed.length === ids.length : body.task_id === undefined);
    if (finished) {
      statements.push(env.DB.prepare("UPDATE cloud_events SET status=?,error_category=? WHERE id=?")
        .bind(body.failed ? "failed" : "done", body.failed ? "bridge_operation" : null, event.id));
      let message = body.failed
        ? `/${event.action} failed on the bridge. ${completed.length}/${ids.length} mailbox items completed; earlier successes were kept. Check Windows logs and resend to retry.`
        : `✅ /${event.action} completed on the Outlook bridge.${ids.length ? ` Emails marked read: ${ids.map(id => `#${id}`).join(", ")}. Later cloud task changes take precedence.` : ""}`;
      if (body.counts) {
        const counts = body.counts;
        message += `\n${["processed", "skipped", "failed", "unchanged"].map(k => `${k}=${Number.isSafeInteger(counts[k]) ? counts[k] : 0}`).join(" ")}`;
      }
      const chunks = textChunks(message);
      if (body.failed || !["read", "done", "dismiss"].includes(event.action)) {
        if (!body.failed && event.action === "list") {
          const data = await snapshot(env);
          if (data) statements.push(...await digestOutbox(env, renderDigest(data), chunks));
          else statements.push(await outbox(env, chunks));
        } else statements.push(await outbox(env, chunks));
      }
    }
    if (["read", "done", "dismiss"].includes(event.action)) {
      statements.push(await taskFeedback(env, event.id, ids, undefined, !body.failed));
    }
    if (statements.length) await env.DB.batch(statements);
    await flush(env);
    return Response.json({ ok: true });
  });
}

export async function cloudFetch(request: Request, env: Env): Promise<Response> {
  const path = new URL(request.url).pathname;
  try {
    if (path === "/telegram/webhook" && request.method === "POST") return await webhook(request, env);
    if (request.headers.get("authorization") !== `Bearer ${env.RELAY_TOKEN}`) return Response.json({ error: "unauthorized" }, { status: 401 });
    if (path === "/v2/config" && request.method === "GET") {
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(env.TELEGRAM_WEBHOOK_SECRET ?? ""));
      return Response.json({ cloud_bot_enabled: true, webhook_secret_sha256: Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("") }, { headers: { "cache-control": "no-store" } });
    }
    if (path === "/v2/snapshot" && request.method === "POST") return await acceptSnapshot(request, env);
    if (path === "/v2/bridge/poll" && request.method === "GET") return await bridgePoll(request, env);
    if (path === "/v2/bridge/ack" && request.method === "POST") return await bridgeAck(request, env);
    return Response.json({ error: "not found" }, { status: 404 });
  } catch (error) {
    // Never return request data, decrypted content, tokens, or arbitrary service exceptions.
    return Response.json({ error: error instanceof SyntaxError ? "Invalid JSON" : "Cloud operation failed; retry or inspect service health" }, { status: error instanceof SyntaxError ? 400 : 503 });
  }
}

export async function runCloudScheduled(env: Env, now = new Date()): Promise<void> {
  await withLock(env, async () => {
    const clock = singaporeClock(now);
    const key = `digest:${clock.date}`;
    if (clock.hour >= 8 && !await env.DB.prepare("SELECT id FROM cloud_events WHERE dedupe_key=?").bind(key).first()) {
      const data = await snapshot(env);
      const statements = [env.DB.prepare("INSERT INTO cloud_events(dedupe_key,action,status,created_at) VALUES(?,'digest','done',?)")
        .bind(key, now.toISOString())];
      if (data) statements.push(...await digestOutbox(env, renderDigest(data, now)));
      else statements.push(await outbox(env, ["No bridge snapshot yet. The daily digest will become available after Windows publishes one."]));
      await env.DB.batch(statements);
    }
    await flush(env);
    // Keep update deduplication tombstones, but discard old encrypted delivered responses.
    await env.DB.prepare("DELETE FROM cloud_outbox WHERE delivered_at IS NOT NULL AND delivered_at<?")
      .bind(new Date(now.getTime() - 30 * 86400000).toISOString()).run();
    // Pending operations can still reference older layouts. Keep them until the queue drains.
    if (!await env.DB.prepare("SELECT id FROM cloud_outbox WHERE delivered_at IS NULL LIMIT 1").first()) {
      const cutoff = new Date(now.getTime() - 30 * 86400000).toISOString();
      await env.DB.batch([
        env.DB.prepare("DELETE FROM cloud_digest_messages WHERE digest_id IN (SELECT id FROM cloud_digests WHERE created_at<?)").bind(cutoff),
        env.DB.prepare("DELETE FROM cloud_digests WHERE created_at<?").bind(cutoff),
      ]);
    }
    return Response.json({ ok: true });
  });
}
