import { afterEach, expect, it, vi } from "vitest";
import { fetchNews, NEWS_UNAVAILABLE } from "../src/news";

const now = new Date("2026-11-02T00:00:00Z");
const story = (id: number, changes = {}) => ({ id, type: "story", title: `Story ${id}`, time: now.getTime() / 1000 - 60, ...changes });
function mockNews(items: Record<number, unknown>, ids = Object.keys(items).map(Number)) {
  return vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (url.endsWith("topstories.json")) return Response.json(ids);
    const id = Number(/item\/(\d+)/.exec(url)?.[1]);
    if (items[id] instanceof Error) throw items[id];
    return Response.json(items[id] ?? null);
  }));
}
afterEach(() => vi.unstubAllGlobals());

it("keeps source rank, deduplicates IDs and excludes deleted, stale, future and non-story items", async () => {
  mockNews({ 1: story(1), 2: story(2, { dead: true }), 3: story(3, { deleted: true }),
    4: story(4, { time: now.getTime() / 1000 - 86401 }), 5: story(5, { time: now.getTime() / 1000 + 1 }),
    6: story(6, { type: "job" }), 7: story(7), 8: story(8, { id: 9 }), 9: null }, [7, 1, 1, 2, 3, 4, 5, 6, 8, 9]);
  const text = await fetchNews(now);
  expect(text).toContain("Story 7");
  expect(text.indexOf("Story 7")).toBeLessThan(text.indexOf("Story 1"));
  expect(text.match(/• /g)).toHaveLength(2);
  expect(fetch).toHaveBeenCalledTimes(10);
});

it("bounds requests and output, escapes headlines and never uses article URLs", async () => {
  mockNews(Object.fromEntries(Array.from({ length: 20 }, (_, i) => [i + 1,
    story(i + 1, { title: '<b>" & '.repeat(100), url: 'javascript:alert(1)' })])));
  const text = await fetchNews(now);
  expect(fetch).toHaveBeenCalledTimes(11);
  expect(text.length).toBeLessThan(3000);
  expect(text).toContain("&lt;b&gt;");
  expect(text).not.toContain("javascript:");
  expect(text).not.toContain('<b>"');
});

it("uses partial results and explains failures without exposing upstream errors", async () => {
  mockNews({ 1: new Error("secret upstream detail"), 2: story(2) });
  const text = await fetchNews(now);
  expect(text).toContain("Story 2");
  expect(text).toContain("Some stories could not be loaded");
  expect(text).not.toContain("secret");
  mockNews({ 1: new Error("secret") });
  expect(await fetchNews(now)).toBe(NEWS_UNAVAILABLE);
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({}, { status: 503 })));
  expect(await fetchNews(now)).toBe(NEWS_UNAVAILABLE);
});

it("handles malformed source data and empty selections", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "no data" })));
  expect(await fetchNews(now)).toBe(NEWS_UNAVAILABLE);
  mockNews({ 1: story(1, { title: " " }), 2: story(2, { time: "yesterday" }) });
  expect(await fetchNews(now)).toContain("No eligible stories");
});

it("decodes source entities before escaping HTML and caps the selection at five", async () => {
  mockNews(Object.fromEntries(Array.from({ length: 10 }, (_, i) => [i + 1,
    story(i + 1, { title: "A &amp; B &#x3c;safe&#62; &#39;quote&#39;" })])));
  const text = await fetchNews(now);
  expect(text.match(/• /g)).toHaveLength(5);
  expect(text).toContain("A &amp; B &lt;safe&gt; 'quote'");
  expect(text).not.toContain("&amp;amp;");
});

it("rejects oversized source responses and sets a deadline without following redirects", async () => {
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
    expect(init.redirect).toBe("error");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    return new Response("x".repeat(65537));
  }));
  expect(await fetchNews(now)).toBe(NEWS_UNAVAILABLE);
});
