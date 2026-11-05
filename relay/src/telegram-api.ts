import type { Env } from "./index";

export class TelegramFailure extends Error {
  constructor(public readonly code: number, public readonly retryAfter = 0, public readonly missing = false) {
    super(`Telegram delivery failed (HTTP ${code})`);
  }
}

export async function telegramRequest(env: Env, method: string, body: object): Promise<{ message_id?: number }> {
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", signal: AbortSignal.timeout(10000), headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json().catch(() => null) as {
    ok?: boolean; result?: { message_id?: number }; error_code?: number; description?: string; parameters?: { retry_after?: number };
  } | null;
  const description = result?.description ?? "";
  const code = result?.error_code ?? response.status;
  if (method === "editMessageText" && code === 400 && /message is not modified/i.test(description)) return {};
  if (method === "deleteMessage" && code === 400 && /message to delete not found/i.test(description)) return {};
  if (!response.ok || !result?.ok) throw new TelegramFailure(code,
    Math.max(0, Number(result?.parameters?.retry_after) || 0),
    code === 403 || (code === 400 && /message to edit not found|message can't be edited|chat not found/i.test(description)));
  return result.result ?? {};
}
