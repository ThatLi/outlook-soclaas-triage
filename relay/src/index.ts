export interface Env {
  DB: D1Database;
  RELAY_TOKEN: string;
  RELAY_ENCRYPTION_KEY: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID: string;
}

export interface BundleEnvelope {
  schema_version: number;
  bundle_id: string;
  intended_date: string;
  generated_at: string;
  last_sync_at: string | null;
  nonce: string;
  ciphertext: string;
}

export interface BundlePayload {
  schema_version: number;
  digest_chunks: string[];
  cached_commands: {
    list: string[];
    status: string[];
    tasks: Record<string, string[]>;
  };
}

interface DeliveryRow {
  delivery_date: string;
  bundle_id: string | null;
  next_chunk: number;
  total_chunks: number;
  status: "pending" | "delivered" | "no_data";
  created_at: string;
  updated_at: string;
  delivered_at: string | null;
  last_error: string | null;
}

const SCHEMA_VERSION = 1;
const MAX_CHUNKS_PER_RUN = 20;
const TASK_FILTERS = ["all", "open", "waiting", "done", "dismissed"];

function json(data: unknown, status = 200): Response {
  return Response.json(data, { status, headers: { "cache-control": "no-store" } });
}

function decodeBase64Url(value: string): Uint8Array<ArrayBuffer> {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4);
  const binary = atob(base64);
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function canonicalMetadata(envelope: BundleEnvelope): Uint8Array<ArrayBuffer> {
  const metadata = {
    bundle_id: envelope.bundle_id,
    generated_at: envelope.generated_at,
    intended_date: envelope.intended_date,
    last_sync_at: envelope.last_sync_at,
    nonce: envelope.nonce,
    schema_version: envelope.schema_version,
  };
  return new TextEncoder().encode(JSON.stringify(metadata));
}

export function validateEnvelope(value: unknown): BundleEnvelope {
  if (!value || typeof value !== "object") throw new Error("body must be an object");
  const item = value as Record<string, unknown>;
  if (item.schema_version !== SCHEMA_VERSION) throw new Error("unsupported schema version");
  for (const field of ["bundle_id", "intended_date", "generated_at", "nonce", "ciphertext"] as const) {
    if (typeof item[field] !== "string" || !item[field]) throw new Error(`${field} is required`);
  }
  if (item.last_sync_at !== null && typeof item.last_sync_at !== "string") {
    throw new Error("last_sync_at must be a string or null");
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(item.intended_date as string)) throw new Error("invalid intended_date");
  if (Number.isNaN(Date.parse(item.generated_at as string))) throw new Error("invalid generated_at");
  if (decodeBase64Url(item.nonce as string).byteLength !== 12) throw new Error("invalid nonce");
  if ((item.ciphertext as string).length > 1_900_000) throw new Error("ciphertext is too large");
  return item as unknown as BundleEnvelope;
}

function validatePayload(value: unknown): BundlePayload {
  if (!value || typeof value !== "object") throw new Error("invalid encrypted payload");
  const payload = value as BundlePayload;
  if (payload.schema_version !== SCHEMA_VERSION || !Array.isArray(payload.digest_chunks)) {
    throw new Error("invalid encrypted payload");
  }
  if (!payload.cached_commands || !Array.isArray(payload.cached_commands.list)
      || !Array.isArray(payload.cached_commands.status) || !payload.cached_commands.tasks) {
    throw new Error("invalid encrypted payload");
  }
  const groups = [payload.digest_chunks, payload.cached_commands.list, payload.cached_commands.status];
  for (const filter of TASK_FILTERS) groups.push(payload.cached_commands.tasks[filter]);
  for (const chunks of groups) {
    if (!Array.isArray(chunks) || chunks.some((chunk) => typeof chunk !== "string" || !chunk || chunk.length > 4096)) {
      throw new Error("invalid encrypted payload chunks");
    }
  }
  if (payload.digest_chunks.length > 500) throw new Error("too many digest chunks");
  return payload;
}

export async function decryptEnvelope(envelope: BundleEnvelope, encodedKey: string): Promise<BundlePayload> {
  const keyBytes = decodeBase64Url(encodedKey);
  if (keyBytes.byteLength !== 32) throw new Error("invalid relay encryption key");
  const key = await crypto.subtle.importKey("raw", keyBytes, "AES-GCM", false, ["decrypt"]);
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: decodeBase64Url(envelope.nonce), additionalData: canonicalMetadata(envelope) },
    key,
    decodeBase64Url(envelope.ciphertext),
  );
  return validatePayload(JSON.parse(new TextDecoder().decode(plaintext)));
}

function authorized(request: Request, env: Env): boolean {
  return request.headers.get("authorization") === `Bearer ${env.RELAY_TOKEN}`;
}

export function singaporeClock(now: Date): { date: string; hour: number } {
  const shifted = new Date(now.getTime() + 8 * 60 * 60 * 1000);
  return { date: shifted.toISOString().slice(0, 10), hour: shifted.getUTCHours() };
}

function envelopeFromRow(row: Record<string, unknown>): BundleEnvelope {
  return {
    schema_version: Number(row.schema_version),
    bundle_id: String(row.bundle_id),
    intended_date: String(row.intended_date),
    generated_at: String(row.generated_at),
    last_sync_at: row.last_sync_at === null ? null : String(row.last_sync_at),
    nonce: String(row.nonce),
    ciphertext: String(row.ciphertext),
  };
}

async function acceptBundle(request: Request, env: Env): Promise<Response> {
  let envelope: BundleEnvelope;
  let payload: BundlePayload;
  try {
    envelope = validateEnvelope(await request.json());
    payload = await decryptEnvelope(envelope, env.RELAY_ENCRYPTION_KEY);
  } catch (error) {
    return json({ error: error instanceof Error ? error.message : "invalid request" }, 400);
  }
  if (payload.schema_version !== envelope.schema_version) return json({ error: "schema mismatch" }, 400);

  const existing = await env.DB.prepare("SELECT bundle_id FROM bundles WHERE bundle_id=?")
    .bind(envelope.bundle_id).first();
  if (existing) return json({ bundle_id: envelope.bundle_id, status: "already_stored" });

  const latest = await env.DB.prepare("SELECT generated_at FROM bundles ORDER BY generated_at DESC LIMIT 1").first();
  if (latest && String(latest.generated_at) > envelope.generated_at) {
    return json({ error: "bundle is older than the newest stored snapshot" }, 409);
  }
  const receivedAt = new Date().toISOString();
  await env.DB.prepare(
    `INSERT INTO bundles(bundle_id, schema_version, intended_date, generated_at, last_sync_at, nonce, ciphertext, received_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(
    envelope.bundle_id, envelope.schema_version, envelope.intended_date, envelope.generated_at,
    envelope.last_sync_at, envelope.nonce, envelope.ciphertext, receivedAt,
  ).run();
  return json({ bundle_id: envelope.bundle_id, status: "stored" }, 201);
}

async function relayStatus(env: Env, now = new Date()): Promise<Response> {
  const latest = await env.DB.prepare(
    "SELECT bundle_id, intended_date, generated_at, last_sync_at, received_at FROM bundles ORDER BY generated_at DESC LIMIT 1",
  ).first();
  const deliveryDate = singaporeClock(now).date;
  const delivery = await env.DB.prepare(
    "SELECT delivery_date, bundle_id, next_chunk, total_chunks, status, updated_at, delivered_at FROM deliveries WHERE delivery_date=?",
  ).bind(deliveryDate).first();
  return json({ latest_bundle: latest ?? null, today_delivery: delivery ?? null });
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function provenance(envelope: BundleEnvelope): string {
  return [
    "☁️ <b>CLOUD RELAY SNAPSHOT</b>",
    "",
    `Prepared: <b>${escapeHtml(envelope.generated_at)}</b>`,
    `Last Outlook sync: <b>${escapeHtml(envelope.last_sync_at ?? "never")}</b>`,
  ].join("\n");
}

async function telegramSend(env: Env, text: string): Promise<void> {
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
  const result = await response.json().catch(() => null) as { ok?: boolean } | null;
  if (!response.ok || !result?.ok) throw new Error(`Telegram delivery failed (HTTP ${response.status})`);
}

async function ensureDelivery(env: Env, deliveryDate: string, nowText: string): Promise<DeliveryRow> {
  const current = await env.DB.prepare("SELECT * FROM deliveries WHERE delivery_date=?")
    .bind(deliveryDate).first<DeliveryRow>();
  if (current) return current;
  const latest = await env.DB.prepare("SELECT * FROM bundles ORDER BY generated_at DESC, received_at DESC LIMIT 1").first();
  const bundleId = latest ? String(latest.bundle_id) : null;
  let totalChunks = 1;
  if (latest) {
    const payload = await decryptEnvelope(envelopeFromRow(latest), env.RELAY_ENCRYPTION_KEY);
    totalChunks = payload.digest_chunks.length + 1;
  }
  await env.DB.prepare(
    `INSERT INTO deliveries(delivery_date, bundle_id, next_chunk, total_chunks, status, created_at, updated_at)
     VALUES (?, ?, 0, ?, 'pending', ?, ?)`,
  ).bind(deliveryDate, bundleId, totalChunks, nowText, nowText).run();
  return (await env.DB.prepare("SELECT * FROM deliveries WHERE delivery_date=?")
    .bind(deliveryDate).first<DeliveryRow>())!;
}

async function deliveryChunks(env: Env, delivery: DeliveryRow): Promise<string[]> {
  if (!delivery.bundle_id) {
    return ["⚠️ <b>OUTLOOK TRIAGE</b>\n\nNo local digest snapshot has been uploaded yet."];
  }
  const row = await env.DB.prepare("SELECT * FROM bundles WHERE bundle_id=?").bind(delivery.bundle_id).first();
  if (!row) throw new Error("Frozen relay bundle is missing");
  const envelope = envelopeFromRow(row);
  const payload = await decryptEnvelope(envelope, env.RELAY_ENCRYPTION_KEY);
  return [provenance(envelope), ...payload.digest_chunks];
}

export async function runScheduled(env: Env, now = new Date()): Promise<void> {
  const clock = singaporeClock(now);
  if (clock.hour < 8) return;
  const nowText = now.toISOString();
  const previousPending = await env.DB.prepare(
    "SELECT * FROM deliveries WHERE status='pending' ORDER BY delivery_date ASC LIMIT 1",
  ).first<DeliveryRow>();
  const delivery = previousPending ?? await ensureDelivery(env, clock.date, nowText);
  if (delivery.status !== "pending") return;
  const chunks = await deliveryChunks(env, delivery);
  const stop = Math.min(chunks.length, delivery.next_chunk + MAX_CHUNKS_PER_RUN);
  try {
    for (let index = delivery.next_chunk; index < stop; index += 1) {
      await telegramSend(env, chunks[index]);
      await env.DB.prepare(
        "UPDATE deliveries SET next_chunk=?, updated_at=?, last_error=NULL WHERE delivery_date=?",
      ).bind(index + 1, nowText, delivery.delivery_date).run();
    }
    if (stop >= chunks.length) {
      await env.DB.prepare(
        `UPDATE deliveries SET status=?, delivered_at=?, updated_at=?, last_error=NULL WHERE delivery_date=?`,
      ).bind(delivery.bundle_id ? "delivered" : "no_data", nowText, nowText, delivery.delivery_date).run();
    }
  } catch (error) {
    await env.DB.prepare("UPDATE deliveries SET updated_at=?, last_error=? WHERE delivery_date=?")
      .bind(nowText, error instanceof Error ? error.message.slice(0, 300) : "delivery failed", delivery.delivery_date).run();
    throw error;
  }

  const sevenDaysAgo = new Date(now.getTime() - 7 * 86400000).toISOString();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 86400000).toISOString().slice(0, 10);
  await env.DB.batch([
    env.DB.prepare(
      `DELETE FROM bundles WHERE received_at < ?
       AND bundle_id <> (SELECT bundle_id FROM bundles ORDER BY generated_at DESC LIMIT 1)
       AND bundle_id NOT IN (SELECT bundle_id FROM deliveries WHERE status='pending' AND bundle_id IS NOT NULL)`,
    ).bind(sevenDaysAgo),
    env.DB.prepare("DELETE FROM deliveries WHERE delivery_date < ? AND status IN ('delivered','no_data')")
      .bind(thirtyDaysAgo),
  ]);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/v1/bundles") return acceptBundle(request, env);
    if (request.method === "GET" && url.pathname === "/v1/status") return relayStatus(env);
    return json({ error: "not found" }, 404);
  },
  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runScheduled(env));
  },
};
