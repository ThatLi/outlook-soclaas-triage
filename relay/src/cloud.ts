import {
  decryptEnvelope, encryptEnvelope, escapeHtml, singaporeClock, telegramSend, validateEnvelope,
  type BundlePayload, type Env,
} from "./index";
import { deliverDigest, digestCallback, dirtyDigests, ensureDigestLayouts, queueDigest, refreshDigests, type DigestCallback, type DigestRow } from "./interactive-digest";
import { TelegramFailure, telegramRequest } from "./telegram-api";
import { flushFeedback, taskFeedback, trackCleanup, type DigestGroup } from "./task-feedback";
import { seal, unseal } from "./cloud_crypto";
export { seal, unseal } from "./cloud_crypto";

type Status = "open" | "waiting" | "done" | "dismissed";
export interface CloudTask {
  id: number; status: Status; description: string; deadline: string | null; urgency: string;
  sender_name: string; sender_address: string; subject: string; preview?: string;
}
export interface InformationEmail {
  id: number; summary: string; subject: string; sender_name: string; sender_address: string;
  received_at: string; is_read: boolean | null; preview?: string;
}
export interface BridgeSnapshot {
  dataset_id: string;
  tasks: CloudTask[];
  information?: InformationEmail[];
  horizon_days: number;
  health: { last_sync: string | null; last_run: { status: string } | null; pending_or_failed: number };
  counts: Record<string, number | null>;
  processed_telegram_updates?: number[];
}
interface Event {
  id: number; action: string; task_ids: string; completed_ids: string; retry_limit: number;
  status: string; created_at: string; digest_id: string | null;
}
interface Command { action: string; ids: number[]; option?: string; limit: number; information?: boolean }
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
  "/show #i42 — informational email preview",
  "/read #i42 [#i43 ...] — mark informational emails read; hide them after Outlook confirms",
  "Informational emails remain for up to 72 hours. Send task and informational IDs separately.",
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
  if (parts.length && parts.every(p => /^#?i[1-9]\d*$/i.test(p))) {
    if (!["show", "read"].includes(action) || parts.length > 100) return null;
    const ids = [...new Set(parts.map(p => Number(p.replace(/^#?i/i, ""))))];
    if (ids.some(id => !Number.isSafeInteger(id)) || (action === "show" && ids.length !== 1)) return null;
    return { ...base, ids, information: true };
  }
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
  if (data.information !== undefined) {
    if (!Array.isArray(data.information)) throw new Error("Invalid informational emails");
    const informationIds = new Set<number>();
    for (const item of data.information) {
      if (!item || !Number.isSafeInteger(item.id) || item.id < 1 || informationIds.has(item.id)
        || [item.summary, item.subject, item.sender_name, item.sender_address].some(v => typeof v !== "string")
        || !item.summary.trim() || item.summary.length > 1000
        || typeof item.received_at !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(item.received_at)
        || Number.isNaN(Date.parse(item.received_at)) || ![true, false, null].includes(item.is_read)
        || (item.preview !== undefined && (typeof item.preview !== "string" || item.preview.length > 12000))) {
        throw new Error("Invalid informational email");
      }
      informationIds.add(item.id);
    }
  }
  return data;
}

export function informationalEmails(data: BridgeSnapshot, now: Date, frozen = false): InformationEmail[] {
  return (data.information ?? []).filter(item => item.is_read === false
    && (frozen || (Date.parse(item.received_at) > now.getTime() - 72 * 3600000 && Date.parse(item.received_at) <= now.getTime())))
    .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at) || a.id - b.id);
}

function previewAvailable(item: CloudTask | InformationEmail, now: Date): boolean {
  if ("status" in item) return ["open", "waiting"].includes(item.status);
  return item.is_read === false && Date.parse(item.received_at) > now.getTime() - 72 * 3600000
    && Date.parse(item.received_at) <= now.getTime();
}

async function pruneSnapshotPreviews(env: Env, now = new Date()): Promise<void> {
  const row = await env.DB.prepare("SELECT envelope_json FROM cloud_snapshot WHERE singleton=1").first<{ envelope_json: string }>();
  if (!row) return;
  const envelope = validateEnvelope(JSON.parse(row.envelope_json));
  const payload = await decryptEnvelope(envelope, env.RELAY_ENCRYPTION_KEY);
  const data = validateBridge(payload.bridge);
  const states = await env.DB.prepare("SELECT id,status FROM cloud_tasks").all<{ id: number; status: Status }>();
  const lookup = new Map(states.results.map(task => [task.id, task.status]));
  let changed = false;
  for (const item of [...data.tasks, ...(data.information ?? [])]) {
    const effective = "status" in item ? { ...item, status: lookup.get(item.id) ?? item.status } : item;
    if (item.preview !== undefined && !previewAvailable(effective, now)) {
      delete item.preview;
      changed = true;
    }
  }
  if (changed) {
    const updated = await encryptEnvelope(envelope, payload, env.RELAY_ENCRYPTION_KEY);
    await env.DB.prepare("UPDATE cloud_snapshot SET envelope_json=? WHERE singleton=1")
      .bind(JSON.stringify(updated)).run();
  }
}

export function informationParts(item: InformationEmail, annotation = ""): string[] {
  const reference = `<code>#i${item.id}</code>`;
  return [
    ...textChunks(item.subject, 1800).map(value => `ℹ️ ${reference}${annotation} <b>${value}</b>`),
    ...textChunks(item.sender_name || item.sender_address, 1800).map(value => `<i>${value}</i>`),
    ...textChunks(item.summary, 1800),
    `<code>/show #i${item.id}</code> · <code>/read #i${item.id}</code>`,
  ];
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
async function outbox(env: Env, chunks: string[], replyEvent?: number | string): Promise<D1PreparedStatement> {
  const id = crypto.randomUUID();
  const column = typeof replyEvent === "string" ? "dedupe_key" : "id";
  return env.DB.prepare(`INSERT INTO cloud_outbox(id,sealed_chunks,created_at,list_event_id) VALUES(?,?,?,(SELECT id FROM cloud_events WHERE ${column}=?))`)
    .bind(id, await seal(env, id, chunks), new Date().toISOString(), replyEvent ?? null);
}

async function withLock(env: Env, work: () => Promise<Response>): Promise<Response> {
  const owner = crypto.randomUUID();
  const lock = await env.DB.prepare("UPDATE cloud_lock SET owner=?,expires_ms=? WHERE singleton=1 AND expires_ms<? RETURNING owner")
    .bind(owner, Date.now() + 120000, Date.now()).first();
  if (!lock) return Response.json({ error: "Cloud bot busy; retry shortly" }, { status: 503 });
  try { return await work(); }
  finally { await env.DB.prepare("UPDATE cloud_lock SET expires_ms=0 WHERE owner=?").bind(owner).run(); }
}

export async function snapshot(env: Env): Promise<BridgeSnapshot | null> {
  const row = await env.DB.prepare("SELECT * FROM cloud_snapshot WHERE singleton=1").first();
  if (!row) return null;
  const payload = await decryptEnvelope(JSON.parse(String(row.envelope_json)), env.RELAY_ENCRYPTION_KEY);
  const data = validateBridge(payload.bridge);
  const states = await env.DB.prepare("SELECT id,status,revision FROM cloud_tasks ORDER BY id").all<{ id: number; status: Status }>();
  const lookup = new Map(states.results.map(t => [t.id, t.status]));
  return { ...data, tasks: data.tasks.map(t => ({ ...t, status: lookup.get(t.id) ?? t.status })) };
}

function packDigestBlocks(blocks: string[], limit = 3000): string[] {
  const messages: string[] = [];
  let current = "";
  for (const block of blocks) {
    const candidate = current ? `${current}\n\n${block}` : block;
    if (current && candidate.length > limit) { messages.push(current); current = block; }
    else current = candidate;
  }
  if (current) messages.push(current);
  return messages;
}

export function digestTaskParts(task: CloudTask, today: string, annotation = ""): string[] {
  const id = `<code>#${task.id}</code>`;
  const parts = textChunks(task.description, 1800).map((description, index) =>
    `${index ? "↳" : task.status === "done" ? "☑" : task.status === "dismissed" ? "☒" : "☐"} ${id}${index ? " <i>(continued)</i>" : annotation} ${description}`);
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
    if (parts[last].length + line.length + 2 <= 2700) parts[last] += `\n ${line}`;
    else parts.push(`↳ ${id}\n ${line}`);
  }
  return parts;
}

export function remainingLabel(tasks: CloudTask[]): string {
  const active = tasks.filter(task => ["open", "waiting"].includes(task.status)).length;
  return active === tasks.length ? String(active) : `${active} remaining / ${tasks.length}`;
}

function digestSection(heading: string, emoji: string, tasks: CloudTask[], today: string, annotations: Map<number, string>): string[] {
  if (!tasks.length) return [];
  const title = `${emoji} <b>${escapeHtml(heading.toUpperCase())} · ${remainingLabel(tasks)}</b>`;
  // Split only at complete rendered fragments. Each fragment has balanced HTML,
  // including repeated task IDs when a long description needs continuation.
  const groups = packDigestBlocks(tasks.flatMap(task => digestTaskParts(task, today, annotations.get(task.id))), 3000 - title.length - 60);
  return groups.map((group, index) => `${title}${groups.length > 1 ? ` — PART ${index + 1} OF ${groups.length}` : ""}\n\n${group}`);
}

export function digestTaskGroups(data: BridgeSnapshot, now = new Date(), fixedGroups?: DigestGroup[]): [string, CloudTask[]][] {
  const today = singaporeClock(now).date;
  const horizon = new Date(Date.parse(today) + data.horizon_days * 86400000).toISOString().slice(0, 10);
  let groups: [string, CloudTask[]][] = [
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
  if (fixedGroups) {
    const lookup = new Map(data.tasks.map(task => [task.id, task]));
    groups = fixedGroups.map(group => [group.heading, group.ids.map(id => lookup.get(id)).filter((task): task is CloudTask => Boolean(task))]);
  }
  return groups;
}

export function renderDigest(data: BridgeSnapshot, now = new Date(), fixedGroups?: DigestGroup[], annotations = new Map<number, string>(), frozenInformation = false, informationAnnotations = new Map<number, string>()): string[] {
  const today = singaporeClock(now).date;
  const groups = digestTaskGroups(data, now, fixedGroups);
  const emojis = ["🔴", "🟠", "🟡", "⚪", "💤"];
  const summary = [
    `📬 <b>Inbox brief — ${today}</b>`,
    ["<b>OPEN ACTIONS</b>", ...groups.map(([heading, tasks], index) =>
      `${emojis[index]} ${escapeHtml(heading)}: <b>${remainingLabel(tasks)}</b>`)].join("\n"),
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
  const sections = groups.map(([heading, tasks], index) => digestSection(heading, emojis[index], tasks, today, annotations));
  messages.push(...sections.slice(0, 3).flat());
  let compact: string[] = [];
  // Only other actions and waiting/follow-up may share a page.
  for (const parts of sections.slice(3)) {
    if (parts.length > 1) {
      messages.push(...packDigestBlocks(compact), ...parts);
      compact = [];
    } else compact.push(...parts);
  }
  const information = informationalEmails(data, now, frozenInformation);
  const infoTitle = "ℹ️ <b>INFORMATIONAL EMAILS · LAST 72 HOURS</b>";
  const infoPages = packDigestBlocks(information.flatMap(item => informationParts(item, informationAnnotations.get(item.id))), 2800);
  const pages = [...messages, ...packDigestBlocks(compact),
    ...(infoPages.length ? infoPages.map(page => `${infoTitle}\n\n${page}`) : [
      `${groups.every(([, tasks]) => tasks.every(task => !["open", "waiting"].includes(task.status))) ? "No active tasks.\n\n" : ""}${infoTitle}\n\nNo unread informational emails.`])];
  return pages;
}

async function flush(env: Env, now = new Date()): Promise<void> {
  const pause = await env.DB.prepare("SELECT telegram_retry_at FROM cloud_lock WHERE singleton=1").first<{ telegram_retry_at: number }>();
  if (pause && pause.telegram_retry_at > Date.now()) return;
  await refreshDigests(env, now);
  await flushFeedback(env);
  const afterRefresh = await env.DB.prepare("SELECT telegram_retry_at FROM cloud_lock WHERE singleton=1").first<{ telegram_retry_at: number }>();
  if (afterRefresh && afterRefresh.telegram_retry_at > Date.now()) return;
  const rows = await env.DB.prepare("SELECT * FROM cloud_outbox WHERE delivered_at IS NULL ORDER BY created_at,id LIMIT 2").all<{
    id: string; sealed_chunks: string; next_chunk: number; digest_id: string | null; list_event_id: number | null;
  }>();
  let budget = 3;
  let trackedRepliesSent = false;
  for (const row of rows.results) {
    if (row.digest_id) {
      const digest = await env.DB.prepare("SELECT * FROM interactive_digests WHERE id=?").bind(row.digest_id).first<DigestRow>();
      if (!digest || !await deliverDigest(env, digest, now)) return;
      await env.DB.prepare("UPDATE cloud_outbox SET delivered_at=? WHERE id=?").bind(now.toISOString(), row.id).run();
      continue;
    }
    const chunks = await unseal(env, row.id, row.sealed_chunks);
    for (let i = row.next_chunk; i < chunks.length; i++) {
      if (--budget < 0) return;
      const statements = [env.DB.prepare("UPDATE cloud_outbox SET next_chunk=?,delivered_at=? WHERE id=?")
        .bind(i + 1, i + 1 === chunks.length ? new Date().toISOString() : null, row.id)];
      try {
        if (row.list_event_id !== null) {
          const result = await telegramRequest(env, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: chunks[i], parse_mode: "HTML", disable_web_page_preview: true });
          if (!Number.isSafeInteger(result.message_id) || result.message_id! <= 0) throw new Error("Telegram did not return a reply message ID");
          statements.push(trackCleanup(env, row.list_event_id, { message: result.message_id!, date: Date.now() / 1000 }));
          trackedRepliesSent = true;
        } else await telegramSend(env, chunks[i]);
      } catch (error) {
        if (error instanceof TelegramFailure && error.retryAfter) await env.DB.prepare("UPDATE cloud_lock SET telegram_retry_at=? WHERE singleton=1")
          .bind(Date.now() + error.retryAfter * 1000).run();
        return;
      }
      await env.DB.batch(statements);
    }
  }
  if (trackedRepliesSent) await flushFeedback(env);
}

async function webhook(request: Request, env: Env): Promise<Response> {
  if (!env.TELEGRAM_WEBHOOK_SECRET || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const update = await request.json() as { update_id?: number; callback_query?: DigestCallback; message?: { message_id?: number; date?: number; chat?: { id?: number; type?: string }; from?: { is_bot?: boolean }; text?: string } };
  if (Number.isSafeInteger(update.update_id) && update.callback_query) return withLock(env, () => digestCallback(update.callback_query!, env));
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
    const missing = ids.filter(id => !(cmd?.information ? data?.information : data?.tasks)?.some(t => t.id === id));
    const quiet = Boolean(cmd && data && !missing.length && ["read", "done", "dismiss", "waiting", "reopen"].includes(cmd.action));
    if (quiet) await ensureDigestLayouts(env, data!);
    let chunks: string[];
    let pending = false;
    let preview = false;
    if (!cmd) chunks = ["Unknown command or invalid arguments. Send task and informational IDs in separate commands. Use /help."];
    else if (cmd.action === "help") chunks = [help];
    else if (!data) chunks = ["No bridge snapshot yet. Run outlook-triage bridge-poll on Windows to initialize cloud state."];
    else if (missing.length) chunks = textChunks(`${cmd.information ? "Informational emails" : "Tasks"} not found: ${missing.map(id => `#${cmd.information ? "i" : ""}${id}`).join(", ")}. No changes made.`);
    else if (cmd.action === "status") {
      const count = await env.DB.prepare("SELECT COUNT(*) AS count FROM cloud_events WHERE status='pending'").first<{ count: number }>();
      const bridge = await env.DB.prepare("SELECT last_seen FROM cloud_snapshot WHERE singleton=1").first<{ last_seen: string }>();
      const totals = statuses.map(s => `${s}: ${data.tasks.filter(t => t.status === s).length}`).join("\n");
      chunks = textChunks(`Cloud bot is available.\nLast bridge contact: ${bridge?.last_seen ?? "never"}\nLast successful Outlook sync: ${data.health.last_sync ?? "never"}\nLatest sync: ${data.health.last_run?.status ?? "none"}\nPending or failed classifications: ${data.health.pending_or_failed}\nQueued mailbox commands: ${count?.count ?? 0}\n${totals}`);
    } else if (cmd.action === "tasks") {
      const tasks = data.tasks.filter(t => cmd.option === "all" || t.status === cmd.option);
      chunks = textChunks(`Tasks · ${cmd.option}\n${tasks.map(t => `#${t.id} [${t.status}] [${t.urgency}] ${t.description}${t.deadline ? ` — due ${t.deadline}` : ""}`).join("\n\n") || "No matching tasks."}`);
    } else if (cmd.action === "show") {
      const task = (cmd.information ? data.information : data.tasks)!.find(t => t.id === ids[0])!;
      preview = previewAvailable(task, new Date()) && task.preview !== undefined;
      chunks = !previewAvailable(task, new Date()) ? ["This email is no longer eligible for a cached preview."]
        : task.preview === undefined ? ["No cached body preview. Enable bridge preview caching on Windows and reconnect."]
        : textChunks(`Email preview · ${task.subject}\nCached at the last bridge publication; bounded preview.\n\n${task.preview || "(empty body)"}`);
    } else if (["waiting", "reopen"].includes(cmd.action)) {
      chunks = textChunks(`Cloud tasks ${ids.map(id => `#${id}`).join(", ")} marked ${cmd.action === "waiting" ? "waiting" : "open"}.`);
    } else {
      pending = mailboxActions.includes(cmd.action);
      chunks = [`⏳ /${cmd.action} queued. It will complete when the Windows Outlook bridge connects.`];
    }
    const event = env.DB.prepare("INSERT INTO cloud_events(dedupe_key,action,task_ids,retry_limit,status,created_at) VALUES(?,?,?,?,?,?)")
      .bind(key, missing.length ? "invalid" : cmd?.information && cmd.action === "read" ? "read_information" : cmd?.action ?? "invalid", JSON.stringify(ids), cmd?.limit ?? 50, pending ? "pending" : "done", new Date().toISOString());
    const statements = [event];
    if (cmd && !missing.length) statements.push(env.DB.prepare(`DELETE FROM cloud_outbox WHERE list_event_id IN (
      SELECT id FROM cloud_events WHERE action='show' AND id<(SELECT id FROM cloud_events WHERE dedupe_key=?))`).bind(key));
    if (cmd && data && !missing.length && ["waiting", "reopen"].includes(cmd.action)) {
      for (const id of ids) statements.push(env.DB.prepare("UPDATE cloud_tasks SET status=?,revision=(SELECT id FROM cloud_events WHERE dedupe_key=?) WHERE id=?")
        .bind(cmd.action === "waiting" ? "waiting" : "open", key, id));
      statements.push(dirtyDigests(env));
    }
    if (cmd?.action === "list") {
      const digest = await queueDigest(env);
      statements.push(...digest.statements, env.DB.prepare("UPDATE cloud_events SET digest_id=? WHERE dedupe_key=?").bind(digest.id, key));
    }
    if (quiet) statements.push(...await taskFeedback(env, key, ids, { message: message.message_id, date: message.date }, true, cmd!.information));
    else {
      if (preview) statements.push(trackCleanup(env, key, { message: message.message_id, date: message.date }));
      statements.push(await outbox(env, chunks, (cmd?.action === "list" && pending) || preview ? key : undefined));
    }
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
    statements.push(dirtyDigests(env));
    // Batch in one transaction: a task import and its encrypted details stay consistent.
    await env.DB.batch(statements);
    await pruneSnapshotPreviews(env);
    await flush(env);
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
    if (event.status !== "pending") { await flush(env); return Response.json({ ok: true }); }
    const ids: number[] = JSON.parse(event.task_ids);
    const completed: number[] = JSON.parse(event.completed_ids);
    const statements: D1PreparedStatement[] = [];
    if (body.task_id !== undefined) {
      if (!ids.includes(body.task_id) || !["read", "done", "dismiss", "read_information"].includes(event.action)) return Response.json({ error: "Item not in command" }, { status: 400 });
      if (completed.includes(body.task_id) && !body.failed) { await flush(env); return Response.json({ ok: true }); }
      if (!completed.includes(body.task_id)) completed.push(body.task_id);
      if (["done", "dismiss"].includes(event.action)) statements.push(env.DB.prepare("UPDATE cloud_tasks SET status=?,revision=? WHERE id=? AND revision<=?")
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
      if (event.action === "read_information") message = body.failed
        ? `Informational read failed: ${completed.length}/${ids.length} completed. Earlier successes were kept; resend to retry.`
        : `✅ Informational emails marked read: ${ids.map(id => `#i${id}`).join(", ")}.`;
      if (body.counts) {
        const counts = body.counts;
        message += `\n${["processed", "skipped", "failed", "unchanged"].map(k => `${k}=${Number.isSafeInteger(counts[k]) ? counts[k] : 0}`).join(" ")}`;
      }
      const chunks = textChunks(message);
      if (event.action === "list" && event.digest_id) statements.push(env.DB.prepare("UPDATE interactive_digests SET dirty=1 WHERE id=? AND digest_date=? AND disabled=0")
        .bind(event.digest_id, singaporeClock(new Date()).date));
      if (body.failed || !["read", "done", "dismiss", "read_information"].includes(event.action)) statements.push(await outbox(env, chunks,
        event.action === "list" && !body.failed ? event.id : undefined));
    }
    if (["read", "done", "dismiss"].includes(event.action)) statements.push(...await taskFeedback(env, event.id, ids, undefined, !body.failed));
    if (event.action === "read_information") statements.push(...await taskFeedback(env, event.id, ids, undefined, !body.failed, true));
    if (statements.length) await env.DB.batch(statements);
    if (["done", "dismiss", "read_information"].includes(event.action)) await pruneSnapshotPreviews(env);
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
    await pruneSnapshotPreviews(env, now);
    const clock = singaporeClock(now);
    const key = `digest:${clock.date}`;
    if (clock.hour >= 8 && !await env.DB.prepare("SELECT id FROM cloud_events WHERE dedupe_key=?").bind(key).first()) {
      const digest = await queueDigest(env, now);
      await env.DB.batch([
        env.DB.prepare("INSERT INTO cloud_events(dedupe_key,action,status,created_at) VALUES(?,'digest','done',?)").bind(key, now.toISOString()),
        ...digest.statements,
      ]);
    }
    await flush(env, now);
    // Keep update deduplication tombstones, but discard old encrypted delivered responses.
    await env.DB.prepare("DELETE FROM cloud_outbox WHERE delivered_at IS NOT NULL AND delivered_at<?")
      .bind(new Date(now.getTime() - 30 * 86400000).toISOString()).run();
    await env.DB.prepare("DELETE FROM cloud_task_feedback WHERE delivered_at IS NOT NULL AND created_at<?")
      .bind(new Date(now.getTime() - 30 * 86400000).toISOString()).run();
    if (!await env.DB.prepare("SELECT id FROM cloud_outbox WHERE delivered_at IS NULL LIMIT 1").first()
      && !await env.DB.prepare("SELECT id FROM cloud_task_feedback WHERE delivered_at IS NULL LIMIT 1").first()) {
      const cutoff = singaporeClock(new Date(now.getTime() - 30 * 86400000)).date;
      await env.DB.batch([
        env.DB.prepare("UPDATE cloud_events SET digest_id=NULL WHERE digest_id IN (SELECT id FROM interactive_digests WHERE digest_date<?)").bind(cutoff),
        env.DB.prepare("DELETE FROM cloud_outbox WHERE digest_id IN (SELECT id FROM interactive_digests WHERE digest_date<?)").bind(cutoff),
        env.DB.prepare("DELETE FROM interactive_digests WHERE digest_date<?").bind(cutoff),
        env.DB.prepare("DELETE FROM cloud_digest_messages WHERE digest_id IN (SELECT id FROM cloud_digests WHERE created_at<?)")
          .bind(new Date(now.getTime() - 30 * 86400000).toISOString()),
        env.DB.prepare("DELETE FROM cloud_digests WHERE created_at<?").bind(new Date(now.getTime() - 30 * 86400000).toISOString()),
      ]);
    }
    return Response.json({ ok: true });
  });
}
