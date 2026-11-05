import type { Env } from "./index";
import { digestTaskGroups, renderDigest, seal, snapshot, unseal } from "./cloud";
import { singaporeClock } from "./index";
import { TelegramFailure, telegramRequest } from "./telegram-api";
import { annotation, digestGroups, effectiveStates, feedbackLabel, type DigestGroup, type TaskEvent } from "./task-feedback";
import type { BridgeSnapshot, CloudTask } from "./cloud";
import { fetchNews } from "./news";
import { renderRichDigest } from "./rich-digest";

export interface DigestRow {
  id: string; digest_date: string; message_id: number | null; view: string; page: number;
  sealed_pages: string; dirty: number; disabled: number; retry_at: number; last_payload_sha256: string | null;
  sealed_layout: string | null; task_ids_json: string; feedback_event: number | null;
  feedback_text: string;
  sealed_news?: string | null;
  format: "legacy" | "rich";
}
interface DigestLayout { tasks: CloudTask[]; groups: DigestGroup[] }
export interface DigestCallback {
  id?: string; data?: string; from?: { id?: number; is_bot?: boolean };
  message?: { message_id?: number; chat?: { id?: number; type?: string } };
}

export function digestMessage(row: Pick<DigestRow, "id" | "digest_date" | "view" | "page">, pages: string[], feedback = "", news = "News trial is disabled or no selection was saved for this edition.") {
  const count = Math.max(1, pages.length - 1);
  const page = Math.min(Math.max(0, row.page), count - 1);
  const button = (text: string, view: string, target = 0) => ({ text, callback_data: `digest:${row.id}:${view}:${target}` });
  const rows = [[button("Overview", "overview"), button("Outlook", "outlook"), button("News", "news")]];
  let content = pages[0];
  if (row.view === "news") content = news;
  if (row.view === "outlook") {
    content = pages[page + 1] ?? "No active tasks.";
    const navigation = [];
    if (page > 0) navigation.push(button("← Previous", "outlook", page - 1));
    if (page + 1 < count) navigation.push(button("Next →", "outlook", page + 1));
    if (navigation.length) rows.unshift(navigation);
  }
  const pagination = row.view === "outlook" ? ` (Page ${page + 1} / ${count})` : "";
  return { page, payload: { text: `📬 <b>Daily brief — ${row.digest_date}${pagination}</b>\n\n${content}${feedback ? `\n\n${feedback}` : ""}`, parse_mode: "HTML",
    disable_web_page_preview: true, reply_markup: { inline_keyboard: rows } } };
}

export async function queueDigest(env: Env, now = new Date()) {
  const id = crypto.randomUUID();
  const data = await snapshot(env);
  const groups = data ? digestTaskGroups(data, now).map(([heading, tasks]) => ({ heading, ids: tasks.map(task => task.id) })) : [];
  const ids = groups.flatMap(group => group.ids);
  const layout: DigestLayout | null = data ? { tasks: data.tasks.filter(task => ids.includes(task.id)).map(({ preview, ...task }) => task), groups } : null;
  let news: string | null = null;
  if (env.NEWS_TRIAL_ENABLED === "true") {
    const saved = await env.DB.prepare("SELECT id,sealed_news FROM interactive_digests WHERE digest_date=? AND sealed_news IS NOT NULL ORDER BY rowid LIMIT 1")
      .bind(singaporeClock(now).date).first<{ id: string; sealed_news: string }>();
    news = saved ? (await unseal(env, saved.id, saved.sealed_news))[0] : await fetchNews(now);
  }
  const pages = renderRichDigest(data, now, groups, new Map(), "", news ?? undefined);
  return { id, statements: [
    env.DB.prepare("INSERT INTO interactive_digests(id,digest_date,sealed_pages,sealed_layout,task_ids_json,sealed_news,dirty,format) VALUES(?,?,?,?,?,?,1,'rich')")
      .bind(id, singaporeClock(now).date, await seal(env, id, pages), layout ? await seal(env, id, [JSON.stringify(layout)]) : null, JSON.stringify(ids), news === null ? null : await seal(env, id, [news])),
    env.DB.prepare("INSERT INTO cloud_outbox(id,sealed_chunks,created_at,digest_id) VALUES(?,?,?,?)")
      .bind(id, await seal(env, id, []), now.toISOString(), id),
  ] };
}

export async function ensureDigestLayouts(env: Env, data: BridgeSnapshot) {
  const rows = await env.DB.prepare("SELECT * FROM interactive_digests WHERE digest_date=? AND sealed_layout IS NULL AND disabled=0")
    .bind(singaporeClock(new Date()).date).all<DigestRow>();
  for (const row of rows.results) {
    const pages = await unseal(env, row.id, row.sealed_pages);
    const groups = row.format === "rich" ? digestTaskGroups(data).map(([heading, tasks]) => ({ heading, ids: tasks.map(task => task.id) })) : digestGroups(pages);
    // A digest sent before the first bridge snapshot can acquire its initial layout.
    if (!groups.length && pages[0].startsWith("No bridge snapshot")) groups.push(...digestGroups(renderDigest(data)));
    const ids = groups.flatMap(group => group.ids);
    const layout: DigestLayout = { tasks: data.tasks.filter(task => ids.includes(task.id)).map(({ preview, ...task }) => task), groups };
    await env.DB.prepare("UPDATE interactive_digests SET sealed_layout=?,task_ids_json=? WHERE id=?")
      .bind(await seal(env, row.id, [JSON.stringify(layout)]), JSON.stringify(ids), row.id).run();
  }
}

export function dirtyDigests(env: Env) {
  return env.DB.prepare("UPDATE interactive_digests SET dirty=1 WHERE digest_date=? AND disabled=0")
    .bind(singaporeClock(new Date()).date);
}

// Caller holds the cloud lock: navigation and background edits cannot overwrite each other.
export async function deliverDigest(env: Env, row: DigestRow, now = new Date()): Promise<boolean> {
  if (row.disabled) return true;
  if (row.retry_at > Date.now()) return false;
  let pages = await unseal(env, row.id, row.sealed_pages);
  let richData: BridgeSnapshot | null = null;
  let richGroups: DigestGroup[] | undefined;
  let richNotes = new Map<number, string>();
  if (row.dirty && row.digest_date === singaporeClock(now).date) {
    const data = await snapshot(env);
    if (data) {
      if (row.sealed_layout === null) {
        await ensureDigestLayouts(env, data);
        const saved = await env.DB.prepare("SELECT sealed_layout FROM interactive_digests WHERE id=?").bind(row.id).first<{ sealed_layout: string }>();
        row.sealed_layout = saved!.sealed_layout;
      }
      const layout: DigestLayout = JSON.parse((await unseal(env, row.id, row.sealed_layout!))[0]);
      const latest = new Map(data.tasks.map(task => [task.id, task]));
      const { tasks, events } = await effectiveStates(env);
      const notes = new Map(layout.tasks.map(task => [task.id, annotation(task.id, tasks.get(task.id), events)]));
      const updated = layout.tasks.map(task => ({ ...task, ...latest.get(task.id), status: (tasks.get(task.id)?.status ?? task.status) as CloudTask["status"] }));
      if (row.format === "rich") {
        richData = { ...data, tasks: updated }; richGroups = layout.groups; richNotes = notes;
      } else pages = renderDigest({ ...data, tasks: updated }, now, layout.groups, notes);
    }
  }
  const event = row.feedback_event === null || row.digest_date !== singaporeClock(now).date ? null
    : await env.DB.prepare("SELECT * FROM cloud_events WHERE id=?").bind(row.feedback_event).first<TaskEvent>();
  const feedback = event ? feedbackLabel(event) : row.feedback_text;
  const news = row.sealed_news ? (await unseal(env, row.id, row.sealed_news))[0] : undefined;
  if (row.format === "rich") {
    // Older editions retry their encrypted, frozen rendering instead of using today's snapshot.
    if (richData) pages = renderRichDigest(richData, new Date(`${row.digest_date}T00:00:00+08:00`), richGroups, richNotes, feedback, news);
    await env.DB.prepare("UPDATE interactive_digests SET sealed_pages=?,feedback_text=? WHERE id=?")
      .bind(await seal(env, row.id, pages), feedback, row.id).run();
    return deliverRichDigest(env, row, pages);
  }
  const { page, payload } = digestMessage(row, pages, feedback, news);
  // Save the last generated edition even when Telegram is temporarily unavailable.
  await env.DB.prepare("UPDATE interactive_digests SET sealed_pages=?,page=?,feedback_text=? WHERE id=?")
    .bind(await seal(env, row.id, pages), page, feedback, row.id).run();
  const fingerprint = await payloadHash(payload);
  if (row.message_id !== null && row.last_payload_sha256 === fingerprint) {
    await env.DB.prepare("UPDATE interactive_digests SET dirty=0,retry_at=0 WHERE id=?").bind(row.id).run();
    return true;
  }
  try {
    const result = await telegramRequest(env, row.message_id === null ? "sendMessage" : "editMessageText", {
      chat_id: env.TELEGRAM_CHAT_ID, ...(row.message_id === null ? {} : { message_id: row.message_id }), ...payload,
    });
    const messageId = row.message_id ?? result.message_id;
    if (!Number.isSafeInteger(messageId)) throw new Error("Telegram did not return a message ID");
    await env.DB.prepare("UPDATE interactive_digests SET message_id=?,dirty=0,retry_at=0,last_payload_sha256=? WHERE id=?")
      .bind(messageId!, fingerprint, row.id).run();
    return true;
  } catch (error) {
    const failure = error instanceof TelegramFailure ? error : null;
    await env.DB.prepare("UPDATE interactive_digests SET dirty=1,disabled=?,retry_at=? WHERE id=?")
      .bind(failure?.missing ? 1 : 0, Date.now() + Math.max(5, failure?.retryAfter ?? 0) * 1000, row.id).run();
    if (failure?.retryAfter) await env.DB.prepare("UPDATE cloud_lock SET telegram_retry_at=? WHERE singleton=1")
      .bind(Date.now() + failure.retryAfter * 1000).run();
    return failure?.missing ?? false;
  }
}

async function payloadHash(payload: object): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(payload)));
  return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
}

async function deliverRichDigest(env: Env, row: DigestRow, parts: string[]): Promise<boolean> {
  const saved = await env.DB.prepare("SELECT part_index,message_id,payload_sha256 FROM interactive_digest_parts WHERE digest_id=? ORDER BY part_index")
    .bind(row.id).all<{ part_index: number; message_id: number; payload_sha256: string }>();
  const byIndex = new Map(saved.results.map(part => [part.part_index, part]));
  let primaryHash = "";
  try {
    for (const [index, html] of parts.entries()) {
      const payload = { rich_message: { html } };
      const hash = await payloadHash(payload);
      if (index === 0) primaryHash = hash;
      const previous = byIndex.get(index);
      if (previous?.payload_sha256 === hash && (index !== 0 || row.last_payload_sha256 !== null)) continue;
      const result = await telegramRequest(env, previous ? "editMessageText" : "sendRichMessage", {
        chat_id: env.TELEGRAM_CHAT_ID, ...(previous ? { message_id: previous.message_id } : {}), ...payload,
      });
      const messageId = previous?.message_id ?? result.message_id;
      if (!Number.isSafeInteger(messageId) || messageId! <= 0) throw new Error("Telegram did not return a message ID");
      const statements = [env.DB.prepare("INSERT INTO interactive_digest_parts(digest_id,part_index,message_id,payload_sha256) VALUES(?,?,?,?) ON CONFLICT(digest_id,part_index) DO UPDATE SET message_id=excluded.message_id,payload_sha256=excluded.payload_sha256")
        .bind(row.id, index, messageId!, hash)];
      if (index === 0) statements.push(env.DB.prepare("UPDATE interactive_digests SET message_id=?,last_payload_sha256=? WHERE id=?")
        .bind(messageId!, hash, row.id));
      await env.DB.batch(statements);
    }
    for (const part of saved.results.filter(part => part.part_index >= parts.length)) {
      await telegramRequest(env, "deleteMessage", { chat_id: env.TELEGRAM_CHAT_ID, message_id: part.message_id });
      await env.DB.prepare("DELETE FROM interactive_digest_parts WHERE digest_id=? AND part_index=?").bind(row.id, part.part_index).run();
    }
    await env.DB.prepare("UPDATE interactive_digests SET dirty=0,retry_at=0,last_payload_sha256=? WHERE id=?").bind(primaryHash, row.id).run();
    return true;
  } catch (error) {
    const failure = error instanceof TelegramFailure ? error : null;
    await env.DB.prepare("UPDATE interactive_digests SET dirty=1,disabled=?,retry_at=? WHERE id=?")
      .bind(failure?.missing ? 1 : 0, Date.now() + Math.max(5, failure?.retryAfter ?? 0) * 1000, row.id).run();
    if (failure?.retryAfter) await env.DB.prepare("UPDATE cloud_lock SET telegram_retry_at=? WHERE singleton=1")
      .bind(Date.now() + failure.retryAfter * 1000).run();
    return failure?.missing ?? false;
  }
}

export async function refreshDigests(env: Env, now = new Date()) {
  const rows = await env.DB.prepare("SELECT * FROM interactive_digests WHERE dirty=1 AND disabled=0 AND message_id IS NOT NULL AND retry_at<=? ORDER BY retry_at,id LIMIT 3")
    .bind(Date.now()).all<DigestRow>();
  for (const row of rows.results) {
    await deliverDigest(env, row, now);
    const paused = await env.DB.prepare("SELECT telegram_retry_at FROM cloud_lock WHERE singleton=1").first<{ telegram_retry_at: number }>();
    if (paused && paused.telegram_retry_at > Date.now()) break;
  }
}

export async function digestCallback(query: DigestCallback, env: Env): Promise<Response> {
  const match = typeof query.data === "string" ? /^digest:([a-f0-9-]{36}):(overview|outlook|news):(\d{1,6})$/.exec(query.data) : null;
  if (!match || typeof query.id !== "string" || !query.id || query.from?.is_bot
    || String(query.from?.id) !== env.TELEGRAM_CHAT_ID || query.message?.chat?.type !== "private"
    || String(query.message.chat.id) !== env.TELEGRAM_CHAT_ID || !Number.isSafeInteger(query.message.message_id)) return Response.json({ ok: true });
  const row = await env.DB.prepare("SELECT * FROM interactive_digests WHERE id=? AND message_id=? AND disabled=0")
    .bind(match[1], query.message.message_id!).first<DigestRow>();
  if (!row || row.format === "rich") return Response.json({ ok: true });
  await telegramRequest(env, "answerCallbackQuery", { callback_query_id: query.id });
  row.view = match[2]; row.page = Number(match[3]); row.dirty = 1;
  await env.DB.prepare("UPDATE interactive_digests SET view=?,page=?,dirty=1 WHERE id=?")
    .bind(row.view, row.page, row.id).run();
  const paused = await env.DB.prepare("SELECT telegram_retry_at FROM cloud_lock WHERE singleton=1").first<{ telegram_retry_at: number }>();
  if (!paused || paused.telegram_retry_at <= Date.now()) await deliverDigest(env, row);
  return Response.json({ ok: true });
}
