import type { Env } from "./index";
import { escapeHtml, singaporeClock } from "./index";
import { TelegramFailure, telegramRequest } from "./telegram-api";
import { unseal } from "./cloud_crypto";

export interface TaskState { id: number; status: string; revision: number }
export interface TaskEvent { id: number; action: string; status: string; task_ids: string; completed_ids: string; digest_id?: string | null }
export interface DigestGroup { heading: string; ids: number[] }

// Match only renderer-owned HTML; email text has already been escaped.
export function digestGroups(pages: string[]): DigestGroup[] {
  const groups = new Map<string, { heading: string; ids: Set<number> }>();
  for (const page of pages) {
    for (const summary of page.matchAll(/^[🔴🟠🟡⚪💤]+ ([^<\n]+): <b>[^<]+<\/b>$/gmu)) {
      const key = summary[1].toLowerCase();
      if (!groups.has(key)) groups.set(key, { heading: summary[1], ids: new Set() });
    }
    const headings = [...page.matchAll(/^[🔴🟠🟡⚪💤]+ <b>([^<]+) · [^<]+<\/b>.*$/gmu)];
    for (let i = 0; i < headings.length; i++) {
      const key = headings[i][1].toLowerCase();
      const group = groups.get(key) ?? { heading: headings[i][1], ids: new Set<number>() };
      const body = page.slice(headings[i].index! + headings[i][0].length, headings[i + 1]?.index);
      for (const match of body.matchAll(/^(?:☐|☑|☒|↳) <code>#(\d+)<\/code>/gm)) group.ids.add(Number(match[1]));
      groups.set(key, group);
    }
  }
  return [...groups.values()].map(group => ({ heading: group.heading, ids: [...group.ids] }));
}

export async function effectiveStates(env: Env) {
  const tasks = await env.DB.prepare("SELECT id,status,revision FROM cloud_tasks").all<TaskState>();
  const events = await env.DB.prepare("SELECT id,action,status,task_ids,completed_ids FROM cloud_events WHERE action IN ('read','done','dismiss','read_information') ORDER BY id").all<TaskEvent>();
  return { tasks: new Map(tasks.results.map(task => [task.id, task])), events: events.results };
}

// Keep PR #13's precedence: later waiting/reopen changes beat older mailbox work.
export function annotation(id: number, state: TaskState | undefined, events: TaskEvent[]): string {
  let workflow = "", read = "";
  for (const event of events) {
    if (event.action === "read_information") continue;
    if (!JSON.parse(event.task_ids).includes(id) || (event.action !== "read" && event.id < (state?.revision ?? 0))) continue;
    let note = "";
    if (JSON.parse(event.completed_ids).includes(id)) {
      if (event.action === "read") note = "Email read";
    } else if (event.status === "pending") note = `⏳ ${event.action === "read" ? "Read" : event.action === "done" ? "Done" : "Dismiss"} pending`;
    else if (event.status === "failed") note = `⚠ /${event.action} failed`;
    if (event.action === "read") read = note; else workflow = note;
  }
  const note = [state?.status === "waiting" ? "💤 Waiting" : "", workflow || (["done", "dismissed"].includes(state?.status ?? "") ? "" : read)].filter(Boolean).join(" · ");
  return note ? ` <i>· ${note}</i>` : "";
}

export function feedbackLabel(event: TaskEvent): string {
  const count = JSON.parse(event.task_ids).length;
  const progress = ["read", "done", "dismiss", "read_information"].includes(event.action)
    ? `${JSON.parse(event.completed_ids).length}/${count} confirmed` : `${count} task${count === 1 ? "" : "s"}`;
  const label = event.status === "pending" ? "⏳ Pending Outlook confirmation" : event.status === "failed" ? "⚠ Failed; earlier successes kept" : "✅ Completed";
  return `${label}: /${event.action === "read_information" ? "read" : event.action} · ${progress}${event.action === "read_information" ? " · informational emails" : ""}`;
}

export function informationAnnotation(id: number, events: TaskEvent[]): string {
  let note = "";
  for (const event of events) {
    if (event.action !== "read_information" || !JSON.parse(event.task_ids).includes(id)) continue;
    note = JSON.parse(event.completed_ids).includes(id) ? "" : event.status === "pending"
      ? "⏳ Read pending" : event.status === "failed" ? "⚠ /read failed" : "";
  }
  return note ? ` <i>· ${note}</i>` : "";
}

async function feedbackDigests(env: Env, information: boolean) {
  const rows = await env.DB.prepare("SELECT id,task_ids_json,sealed_layout,message_id,dirty FROM interactive_digests WHERE digest_date=? AND disabled=0")
    .bind(singaporeClock(new Date()).date).all<{ id: string; task_ids_json: string; sealed_layout: string | null; message_id: number | null; dirty: number }>();
  return Promise.all(rows.results.map(async digest => {
    let ids: number[] = JSON.parse(digest.task_ids_json);
    if (information) {
      const layout = digest.sealed_layout ? JSON.parse((await unseal(env, digest.id, digest.sealed_layout))[0]) : null;
      ids = (layout?.information ?? []).map((item: { id: number }) => item.id);
    }
    return { ...digest, ids };
  }));
}

export function trackCleanup(env: Env, event: number | string, cleanup?: { message?: number; date?: number }, allowFallback = false) {
  const column = typeof event === "string" ? "dedupe_key" : "id";
  const validCleanup = Number.isSafeInteger(cleanup?.message) && cleanup!.message! > 0 && Number.isFinite(cleanup?.date);
  return env.DB.prepare(`INSERT INTO cloud_task_feedback(event_id,command_message_id,expires_at,allow_fallback,created_at)
    SELECT id,?,?,?,? FROM cloud_events WHERE ${column}=?`)
    .bind(validCleanup ? cleanup!.message! : null, validCleanup ? cleanup!.date! * 1000 + 48 * 3600000 - 60000 : null,
      allowFallback ? 1 : 0, new Date().toISOString(), event);
}

export async function taskFeedback(env: Env, event: number | string, ids: number[], cleanup?: { message?: number; date?: number }, allowFallback = true, information = false) {
  const column = typeof event === "string" ? "dedupe_key" : "id";
  const relevant = information ? (await feedbackDigests(env, true)).filter(digest => digest.ids.some(id => ids.includes(id))) : [];
  const update = information
    ? env.DB.prepare(`UPDATE interactive_digests SET dirty=1,feedback_event=(SELECT id FROM cloud_events WHERE ${column}=?)
      WHERE id IN (${relevant.length ? relevant.map(() => "?").join(",") : "NULL"})`)
      .bind(event, ...relevant.map(digest => digest.id))
    : env.DB.prepare(`UPDATE interactive_digests SET dirty=1,feedback_event=(SELECT id FROM cloud_events WHERE ${column}=?)
      WHERE digest_date=? AND disabled=0 AND EXISTS (SELECT 1 FROM json_each(task_ids_json) WHERE value IN (${ids.map(() => "?").join(",")}))`)
      .bind(event, singaporeClock(new Date()).date, ...ids);
  return [
    update,
    trackCleanup(env, event, cleanup, allowFallback),
  ];
}

export async function flushFeedback(env: Env) {
  const pause = await env.DB.prepare("SELECT telegram_retry_at FROM cloud_lock WHERE singleton=1").first<{ telegram_retry_at: number }>();
  if (pause && pause.telegram_retry_at > Date.now()) return;
  const rows = await env.DB.prepare(`SELECT feedback.* FROM cloud_task_feedback feedback
    JOIN cloud_events event ON event.id=feedback.event_id
    WHERE feedback.delivered_at IS NULL AND feedback.retry_at<=?
    AND (event.action<>'show' OR feedback.expires_at IS NULL OR feedback.expires_at<=?
      OR EXISTS (SELECT 1 FROM cloud_events newer WHERE newer.id>event.id AND newer.dedupe_key LIKE 'tg:%' AND newer.action<>'invalid'))
    ORDER BY feedback.id LIMIT 3`)
    .bind(Date.now(), Date.now()).all<{ id: number; event_id: number; command_message_id: number | null; expires_at: number | null; allow_fallback: number; fallback_sent: number }>();
  if (!rows.results.length) return;
  const digestsByKind = new Map<boolean, Awaited<ReturnType<typeof feedbackDigests>>>();
  for (const row of rows.results) {
    const event = (await env.DB.prepare("SELECT * FROM cloud_events WHERE id=?").bind(row.event_id).first<TaskEvent>())!;
    if (event.action === "show" && (row.expires_at === null || row.expires_at <= Date.now())) {
      await env.DB.prepare("UPDATE cloud_task_feedback SET delivered_at=? WHERE id=?").bind(new Date().toISOString(), row.id).run();
      continue;
    }
    if (event.action === "list") {
      if (event.status === "pending") continue;
      const digest = event.digest_id ? await env.DB.prepare("SELECT message_id,dirty,disabled FROM interactive_digests WHERE id=?")
        .bind(event.digest_id).first<{ message_id: number | null; dirty: number; disabled: number }>() : null;
      if (event.status === "failed" || !digest || digest.disabled) {
        await env.DB.prepare("UPDATE cloud_task_feedback SET delivered_at=? WHERE id=?").bind(new Date().toISOString(), row.id).run();
        continue;
      }
      if (digest.message_id === null || digest.dirty) continue;
    }
    const ids: number[] = event.action === "show" ? [] : JSON.parse(event.task_ids);
    const information = event.action === "read_information";
    if (!digestsByKind.has(information)) digestsByKind.set(information, await feedbackDigests(env, information));
    const relevant = digestsByKind.get(information)!.filter(digest => digest.ids.some(id => ids.includes(id)));
    // Do not delete a command before all of today's targeted digests are current.
    if (relevant.some(digest => digest.message_id !== null && digest.dirty)) continue;
    const covered = new Set(relevant.filter(digest => digest.message_id !== null).flatMap(digest => digest.ids));
    try {
      if (row.allow_fallback && !row.fallback_sent && ids.some(id => !covered.has(id))) {
        const tasks = information ? new Map<number, TaskState>() : (await effectiveStates(env)).tasks;
        const groups = new Map<string, number[]>();
        for (const id of ids) {
          const status = information ? JSON.parse(event.completed_ids).includes(id) ? "read" : event.status
            : tasks.get(id)?.status ?? "unavailable";
          groups.set(status, [...(groups.get(status) ?? []), id]);
        }
        const text = `${feedbackLabel(event)}\n${[...groups].map(([status, values]) => `${escapeHtml(status)}: ${values.map(id => `#${information ? "i" : ""}${id}`).join(", ")}`).join("\n")}`;
        await telegramRequest(env, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text, parse_mode: "HTML" });
        await env.DB.prepare("UPDATE cloud_task_feedback SET fallback_sent=1 WHERE id=?").bind(row.id).run();
      }
      if (row.command_message_id !== null && row.expires_at! > Date.now()) {
        try { await telegramRequest(env, "deleteMessage", { chat_id: env.TELEGRAM_CHAT_ID, message_id: row.command_message_id }); }
        catch (error) { if (!(error instanceof TelegramFailure) || ![400, 403].includes(error.code)) throw error; }
      }
      await env.DB.prepare("UPDATE cloud_task_feedback SET delivered_at=? WHERE id=?").bind(new Date().toISOString(), row.id).run();
    } catch (error) {
      const delay = error instanceof TelegramFailure ? error.retryAfter : 0;
      await env.DB.prepare("UPDATE cloud_task_feedback SET retry_at=? WHERE id=?").bind(Date.now() + Math.max(5, delay) * 1000, row.id).run();
      if (delay) {
        await env.DB.prepare("UPDATE cloud_lock SET telegram_retry_at=? WHERE singleton=1").bind(Date.now() + delay * 1000).run();
        return;
      }
    }
  }
}
