import { digestTaskGroups, digestTaskParts, remainingLabel, textChunks, informationalEmails, informationParts, type BridgeSnapshot } from "./cloud";
import { escapeHtml, singaporeClock } from "./index";
import type { DigestGroup } from "./task-feedback";

interface Fragment { summary?: string; html: string }

// Only renderer-owned HTML enters this packer; mailbox strings are escaped first.
export function renderRichDigest(data: BridgeSnapshot | null, now = new Date(), fixedGroups?: DigestGroup[],
  annotations = new Map<number, string>(), feedback = "", news?: string, frozenInformation = false, informationAnnotations = new Map<number, string>()): string[] {
  const today = singaporeClock(now).date;
  const fragments: Fragment[] = [];
  const paragraph = (html: string, summary?: string) => fragments.push({ summary, html: `<p>${html.replaceAll("\n", "<br>")}</p>` });
  if (data) {
    const groups = digestTaskGroups(data, now, fixedGroups);
    const active = groups.flatMap(([, tasks]) => tasks).filter(task => ["open", "waiting"].includes(task.status)).length;
    paragraph(`<b>${active} open tasks</b>`);
    for (const value of textChunks(data.health.last_sync ?? "never", 2000)) paragraph(`Last successful Outlook sync: <b>${value}</b>`);
    paragraph("<i>Mailbox data is a bridge snapshot; cloud task states are current.</i>");
    if (data.health.last_run?.status !== "success") paragraph("⚠️ <b>Warning:</b> latest mailbox synchronization did not finish successfully.");
    if (data.health.pending_or_failed) paragraph(`⚠️ <b>${data.health.pending_or_failed}</b> pending or failed classifications.`);
    if (feedback) paragraph(feedback);
    const emojis = ["🔴", "🟠", "🟡", "⚪", "💤"];
    if (!groups.some(([, tasks]) => tasks.length)) paragraph("No active tasks.");
    groups.forEach(([heading, tasks], index) => {
      const summary = `${emojis[index]} <b>${escapeHtml(heading.toUpperCase())} · ${remainingLabel(tasks)}</b>`;
      if (!tasks.length) paragraph(summary);
      else for (const task of tasks) {
        for (const part of digestTaskParts(task, today, annotations.get(task.id))) paragraph(part, summary);
      }
    });
    const information = informationalEmails(data, now, frozenInformation);
    const informationHeading = "ℹ️ <b>INFORMATIONAL EMAILS · LAST 72 HOURS</b>";
    if (!information.length) paragraph("No unread informational emails.", informationHeading);
    for (const item of information) {
      for (const part of informationParts(item, informationAnnotations.get(item.id))) paragraph(part, informationHeading);
    }
    for (const [label, key] of [["Informational", "information_count"], ["Newsletters", "newsletter_count"],
      ["Automated", "automated_count"], ["Explicitly skipped", "skipped_count"]]) {
      paragraph(`• ${label}: <b>${escapeHtml(String(data.counts[key] ?? 0))}</b>`, "📊 Recent activity · last 24 hours at snapshot preparation");
    }
  } else {
    paragraph("No bridge snapshot yet. The digest will become available after Windows publishes one.");
    if (feedback) paragraph(feedback);
  }
  if (news) paragraph(news, "📰 News");

  const render = (items: Fragment[]) => {
    let html = "", current: string | undefined;
    for (const item of items) {
      if (item.summary !== current) {
        if (current !== undefined) html += "</details>";
        if (item.summary !== undefined) html += `<details><summary>${item.summary}</summary>`;
        current = item.summary;
      }
      html += item.html;
    }
    return html + (current !== undefined ? "</details>" : "");
  };
  const parts: string[] = [];
  let current: Fragment[] = [];
  for (const fragment of fragments) {
    const candidate = render([...current, fragment]);
    // Reserve room for the numbered heading; count summary and paragraph blocks conservatively.
    if (current.length && (new TextEncoder().encode(candidate).length > 29500
      || (candidate.match(/<(?:p|details|summary)>/g)?.length ?? 0) > 390)) {
      parts.push(render(current)); current = [];
    }
    current.push(fragment);
  }
  if (current.length) parts.push(render(current));
  return parts.map((part, index) => `<h2>📬 Daily brief — ${today}${parts.length > 1 ? ` · ${index + 1} / ${parts.length}` : ""}</h2>${part}`);
}
