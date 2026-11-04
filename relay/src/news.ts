import { escapeHtml } from "./index";

const API = "https://hacker-news.firebaseio.com/v0";
export const NEWS_UNAVAILABLE = "📰 <b>News trial · Hacker News</b>\n\nNews could not be loaded for this edition. The next daily edition will try again.";

async function readJson(path: string): Promise<unknown> {
  const response = await fetch(`${API}/${path}.json`, { signal: AbortSignal.timeout(4000), redirect: "error" });
  if (!response.ok) throw new Error("News source unavailable");
  if (!response.body) throw new Error("Empty news response");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 65536) throw new Error("News response too large");
      text += decoder.decode(value, { stream: true });
    }
  } finally { await reader.cancel(); }
  return JSON.parse(text + decoder.decode());
}

function headline(value: string): string {
  const entities: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };
  const decoded = value.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (match, entity: string) => {
    if (!entity.startsWith("#")) return entities[entity.toLowerCase()];
    const code = entity[1].toLowerCase() === "x" ? parseInt(entity.slice(2), 16) : Number(entity.slice(1));
    return code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : match;
  });
  return escapeHtml(Array.from(decoded.replace(/[\u0000-\u001f\u007f]/g, " ").trim()).slice(0, 160).join(""));
}

export async function fetchNews(now = new Date()): Promise<string> {
  try {
    const ids = await readJson("topstories");
    if (!Array.isArray(ids)) return NEWS_UNAVAILABLE;
    const candidates = [...new Set(ids.filter(id => Number.isSafeInteger(id) && id > 0))].slice(0, 10);
    const results = await Promise.allSettled(candidates.map(id => readJson(`item/${id}`)));
    const lines: string[] = [];
    let failed = false;
    for (const [index, result] of results.entries()) {
      if (result.status === "rejected") { failed = true; continue; }
      const item = result.value as Record<string, unknown> | null;
      if (!item || item.id !== candidates[index] || item.type !== "story" || item.dead || item.deleted
        || typeof item.title !== "string" || !item.title.trim() || typeof item.time !== "number"
        || !Number.isFinite(item.time)) continue;
      const age = now.getTime() / 1000 - item.time;
      if (age < 0 || age > 86400) continue;
      // Link only to the fixed discussion host; never trust a source-provided URL or HTML.
      const title = headline(item.title);
      const line = `• <a href="https://news.ycombinator.com/item?id=${item.id}">${title}</a>`;
      if ([...lines, line].join("\n\n").length > 2600) continue;
      lines.push(line);
      if (lines.length === 5) break;
    }
    if (!lines.length && failed) return NEWS_UNAVAILABLE;
    return ["📰 <b>News trial · Hacker News</b>",
      `Selected: ${now.toISOString().slice(0, 16).replace("T", " ")} UTC`,
      // shortcut: one technology source and ten candidates, add sources after the trial is evaluated.
      "Up to 5 top-ranked stories posted in the past 24 hours. Links open HN discussions; headlines are not verified reporting.",
      lines.join("\n\n") || "No eligible stories in the current top ten.",
      failed ? "Some stories could not be loaded." : "",
    ].filter(Boolean).join("\n\n");
  } catch { return NEWS_UNAVAILABLE; }
}
