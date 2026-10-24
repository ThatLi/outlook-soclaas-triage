import { seal, unseal } from "./cloud_crypto";
import { escapeHtml, telegramRequest, telegramSend, TelegramApiError, type Env } from "./index";

type Group = { heading: string; ids: number[] };
type EventRef = number | string;
type Item = string
  | { kind: "digest"; digest: string; chunk: number }
  | { kind: "edit"; digest: string; chunk: number; event: EventRef }
  | { kind: "feedback"; event: EventRef }
  | { kind: "delete"; message: number; expires: number };
interface Digest { id: string; sealed_chunks: string; groups_json: string }
interface Event {
  id: number; action: string; status: string; task_ids: string; completed_ids: string;
}
interface TaskState { id: number; status: string; revision: number }

export async function outbox(env: Env, items: Item[]): Promise<D1PreparedStatement> {
  const id = crypto.randomUUID();
  return env.DB.prepare("INSERT INTO cloud_outbox(id,sealed_chunks,created_at) VALUES(?,?,?)")
    .bind(id, await seal(env, id, items), new Date().toISOString());
}

// Only renderer-owned HTML is recognized; email text is already escaped.
function groupsIn(chunks: string[]): Group[] {
  const groups = new Map<string, Set<number>>();
  for (const chunk of chunks) {
    const headings = [...chunk.matchAll(/^[🔴🟠🟡⚪💤]+ <b>([^<]+) · \d+<\/b>.*$/gmu)];
    for (let i = 0; i < headings.length; i++) {
      const heading = headings[i][1];
      const ids = groups.get(heading) ?? new Set<number>();
      const body = chunk.slice(headings[i].index! + headings[i][0].length, headings[i + 1]?.index);
      for (const match of body.matchAll(/^(?:☐|↳) <code>#(\d+)<\/code>/gm)) ids.add(Number(match[1]));
      groups.set(heading, ids);
    }
  }
  return [...groups].map(([heading, ids]) => ({ heading, ids: [...ids] }));
}

export async function digestOutbox(env: Env, chunks: string[], prefix: string[] = []): Promise<D1PreparedStatement[]> {
  const id = crypto.randomUUID();
  const statements = [env.DB.prepare("INSERT INTO cloud_digests(id,sealed_chunks,groups_json,created_at) VALUES(?,?,?,?)")
    .bind(id, await seal(env, id, chunks), JSON.stringify(groupsIn(chunks)), new Date().toISOString())];
  for (let i = 0; i < chunks.length; i++) statements.push(env.DB.prepare(
    "INSERT INTO cloud_digest_messages(digest_id,chunk_index) VALUES(?,?)",
  ).bind(id, i));
  statements.push(await outbox(env, [...prefix, ...chunks.map((_, chunk): Item => ({ kind: "digest", digest: id, chunk }))]));
  return statements;
}

async function effectiveStates(env: Env) {
  const tasks = await env.DB.prepare("SELECT id,status,revision FROM cloud_tasks").all<TaskState>();
  const events = await env.DB.prepare(
    "SELECT id,action,status,task_ids,completed_ids FROM cloud_events WHERE action IN ('read','done','dismiss') ORDER BY id",
  ).all<Event>();
  return { tasks: new Map(tasks.results.map(t => [t.id, t])), events: events.results };
}

function annotation(id: number, state: TaskState | undefined, events: Event[]): string {
  let workflow = "";
  let read = "";
  for (const event of events) {
    if (!JSON.parse(event.task_ids).includes(id)) continue;
    if (event.action !== "read" && event.id < (state?.revision ?? 0)) continue;
    let note = "";
    if (JSON.parse(event.completed_ids).includes(id)) {
      if (event.action === "read") note = "Email read";
    } else if (event.status === "pending") note = `⏳ ${event.action === "read" ? "Read" : event.action === "done" ? "Done" : "Dismiss"} pending`;
    else if (event.status === "failed") note = `⚠ /${event.action} failed`;
    if (event.action === "read") read = note;
    else workflow = note;
  }
  const note = [state?.status === "waiting" ? "💤 Waiting" : "",
    workflow || (["done", "dismissed"].includes(state?.status ?? "") ? "" : read)].filter(Boolean).join(" · ");
  return note ? ` <i>· ${note}</i>` : "";
}

async function renderChunk(env: Env, digest: Digest, index: number): Promise<string> {
  const originals = await unseal<string[]>(env, digest.id, digest.sealed_chunks);
  const { tasks, events } = await effectiveStates(env);
  const groups: Group[] = JSON.parse(digest.groups_json);
  let html = originals[index];
  html = html.replace(/^(☐|↳) <code>#(\d+)<\/code>/gm, (_, marker: string, value: string) => {
    const id = Number(value);
    const task = tasks.get(id);
    const symbol = marker === "↳" ? marker : task?.status === "done" ? "☑" : task?.status === "dismissed" ? "☒" : "☐";
    return `${symbol} <code>#${id}</code>${marker === "↳" ? "" : annotation(id, task, events)}`;
  });
  // Membership stays fixed in a sent digest. Totals count its remaining open/waiting rows.
  for (const group of groups) {
    const remaining = group.ids.filter(id => ["open", "waiting"].includes(tasks.get(id)?.status ?? "")).length;
    const label = remaining === group.ids.length ? String(remaining) : `${remaining} remaining / ${group.ids.length}`;
    html = html.replace(`<b>${group.heading} · ${group.ids.length}</b>`, `<b>${group.heading} · ${label}</b>`);
    const summaryLabel = group.heading.toLowerCase();
    html = html.replace(/^([🔴🟠🟡⚪💤]+ )([^<\n]+): <b>\d+<\/b>$/gmu,
      (line, emoji: string, heading: string) => heading.toLowerCase() === summaryLabel
        ? `${emoji}${heading}: <b>${label}</b>` : line);
  }
  return html;
}

// One command targets the most recently sent, still-editable digest containing each task.
// Summary and continuation chunks are included so the visible layout stays consistent.
export async function taskFeedback(
  env: Env, event: EventRef, ids: number[], cleanup?: { message: number; date: number },
  allowFallback = true,
): Promise<D1PreparedStatement> {
  const rows = await env.DB.prepare("SELECT * FROM cloud_digests ORDER BY rowid DESC").all<Digest>();
  const uncovered = new Set(ids);
  const selected = new Set<string>();
  const items: Item[] = [];
  for (const digest of rows.results) {
    const groups: Group[] = JSON.parse(digest.groups_json);
    if (!groups.some(group => group.ids.some(id => uncovered.has(id)))) continue;
    const chunks = await unseal<string[]>(env, digest.id, digest.sealed_chunks);
    const messages = await env.DB.prepare(
      "SELECT chunk_index,message_id,unavailable FROM cloud_digest_messages WHERE digest_id=? ORDER BY chunk_index",
    ).bind(digest.id).all<{ chunk_index: number; message_id: number | null; unavailable: number }>();
    let found = false;
    for (const message of messages.results) {
      if (!message.message_id || message.unavailable) continue;
      for (const match of chunks[message.chunk_index].matchAll(/^☐ <code>#(\d+)<\/code>/gm)) {
        if (uncovered.delete(Number(match[1]))) found = true;
      }
    }
    if (found) selected.add(digest.id);
  }
  for (const digest of selected) {
    const messages = await env.DB.prepare(
      "SELECT chunk_index FROM cloud_digest_messages WHERE digest_id=? AND message_id IS NOT NULL AND unavailable=0 ORDER BY chunk_index",
    ).bind(digest).all<{ chunk_index: number }>();
    for (const message of messages.results) items.push({ kind: "edit", digest, chunk: message.chunk_index, event });
  }
  if (uncovered.size && allowFallback) items.push({ kind: "feedback", event });
  if (cleanup && Number.isSafeInteger(cleanup.message) && cleanup.message > 0 && Number.isFinite(cleanup.date)) {
    items.push({ kind: "delete", message: cleanup.message, expires: cleanup.date * 1000 + 48 * 3600000 - 60000 });
  }
  return outbox(env, items);
}

async function feedbackText(env: Env, id: EventRef): Promise<string> {
  const event = (await env.DB.prepare(`SELECT id,action,status,task_ids,completed_ids FROM cloud_events WHERE ${typeof id === "string" ? "dedupe_key" : "id"}=?`)
    .bind(id).first<Event>())!;
  const ids: number[] = JSON.parse(event.task_ids);
  const { tasks } = await effectiveStates(env);
  const label = event.status === "pending" ? "⏳ Pending Outlook confirmation" : event.status === "failed" ? "⚠ Failed; earlier successes kept" : "✅ Completed";
  const progress = ["read", "done", "dismiss"].includes(event.action)
    ? `\n${JSON.parse(event.completed_ids).length}/${ids.length} mailbox items completed.` : "";
  const groups = new Map<string, number[]>();
  for (const id of ids) {
    const status = tasks.get(id)?.status ?? "unavailable";
    groups.set(status, [...(groups.get(status) ?? []), id]);
  }
  // Every ID appears once; even a 100-item command with maximum safe IDs fits.
  return `${label}: /${event.action}${progress}\n${[...groups].map(([status, values]) =>
    `${escapeHtml(status)}: ${values.map(id => `#${id}`).join(", ")}`,
  ).join("\n")}`;
}

export async function flush(env: Env): Promise<void> {
  const rows = await env.DB.prepare(
    "SELECT * FROM cloud_outbox WHERE delivered_at IS NULL ORDER BY rowid LIMIT 2",
  ).all<{ id: string; sealed_chunks: string; next_chunk: number; fallback_sent: number; retry_after_ms: number }>();
  let budget = 3;
  for (const row of rows.results) {
    if (row.retry_after_ms > Date.now()) return;
    const items = await unseal<Item[]>(env, row.id, row.sealed_chunks);
    const fallback = async (event: EventRef) => {
      if (row.fallback_sent) return;
      await telegramSend(env, await feedbackText(env, event));
      await env.DB.prepare("UPDATE cloud_outbox SET fallback_sent=1 WHERE id=?").bind(row.id).run();
      row.fallback_sent = 1;
    };
    for (let i = row.next_chunk; i < items.length; i++) {
      if (--budget < 0) return;
      const item = items[i];
      try {
        if (typeof item === "string") await telegramSend(env, item);
        else if (item.kind === "feedback") await fallback(item.event);
        else if (item.kind === "delete") {
          if (Date.now() < item.expires) {
            try { await telegramRequest(env, "deleteMessage", { message_id: item.message }); }
            catch (error) {
              // Cleanup cannot turn a successful command into an operation failure.
              if (!(error instanceof TelegramApiError) || ![400, 403].includes(error.code)) throw error;
            }
          }
        } else {
          const digest = (await env.DB.prepare("SELECT * FROM cloud_digests WHERE id=?").bind(item.digest).first<Digest>())!;
          const message = (await env.DB.prepare("SELECT message_id,unavailable FROM cloud_digest_messages WHERE digest_id=? AND chunk_index=?")
            .bind(item.digest, item.chunk).first<{ message_id: number | null; unavailable: number }>())!;
          const html = await renderChunk(env, digest, item.chunk);
          if (item.kind === "digest") {
            const messageId = await telegramSend(env, html);
            // Advancing the outbox and saving its Telegram ID happen in one transaction.
            await env.DB.batch([
              env.DB.prepare("UPDATE cloud_digest_messages SET message_id=? WHERE digest_id=? AND chunk_index=?")
                .bind(messageId, item.digest, item.chunk),
              env.DB.prepare("UPDATE cloud_outbox SET next_chunk=?,delivered_at=? WHERE id=?")
                .bind(i + 1, i + 1 === items.length ? new Date().toISOString() : null, row.id),
            ]);
            continue;
          }
          if (message.unavailable || !message.message_id) await fallback(item.event);
          else {
            try {
              await telegramRequest(env, "editMessageText", { message_id: message.message_id, text: html,
                parse_mode: "HTML", disable_web_page_preview: true });
            } catch (error) {
              if (!(error instanceof TelegramApiError)) throw error;
              if (error.code === 400 && /message is not modified/i.test(error.description)) { /* Already current. */ }
              else if (error.code === 400) {
                await env.DB.prepare("UPDATE cloud_digest_messages SET unavailable=1 WHERE digest_id=? AND chunk_index=?")
                  .bind(item.digest, item.chunk).run();
                await fallback(item.event);
              } else throw error;
            }
          }
        }
      } catch (error) {
        const delay = error instanceof TelegramApiError ? error.retryAfter : 0;
        if (delay) await env.DB.prepare("UPDATE cloud_outbox SET retry_after_ms=? WHERE id=?")
          .bind(Date.now() + delay * 1000, row.id).run();
        return;
      }
      await env.DB.prepare("UPDATE cloud_outbox SET next_chunk=?,delivered_at=? WHERE id=?")
        .bind(i + 1, i + 1 === items.length ? new Date().toISOString() : null, row.id).run();
    }
    if (!items.length) await env.DB.prepare("UPDATE cloud_outbox SET delivered_at=? WHERE id=?")
      .bind(new Date().toISOString(), row.id).run();
  }
}
