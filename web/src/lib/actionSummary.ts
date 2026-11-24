// A browser step's folded row says which page it was on. The backend's sentence is about the
// action (「点击 #12」); on its own it cannot tell two tabs or two sites apart. So the row adds
// the page title and the site after it, and the whole sentence stays in the row's tooltip.
// Pure text, so the replay and the tests can use it without the chat store.

/** How much of a page title the folded row shows before it is cut with an ellipsis. */
export const PAGE_TITLE_MAX = 40;

/** The site a page belongs to, port included (`localhost:3000`), without a leading `www.`. */
export function pageHost(url: string | null | undefined): string {
  if (!url) return "";
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/**
 * The folded row for a browser step: 「<sentence> · <title> · <host>」, skipping a part the
 * sentence already says. Returns null when the detail names no page, so the row keeps the
 * backend's sentence untouched.
 */
export function browserRowSummary(
  sentence: string,
  page: { title?: string | null; url?: string | null },
  titleMax = PAGE_TITLE_MAX,
): string | null {
  const base = sentence.replace(/\s+/g, " ").trim();
  const title = (page.title ?? "").replace(/\s+/g, " ").trim();
  if (!title) return null;
  const host = pageHost(page.url);
  const said = base.toLowerCase();
  const parts = [base];
  if (!said.includes(title.toLowerCase())) {
    const chars = Array.from(title);
    parts.push(chars.length <= titleMax ? title : `${chars.slice(0, titleMax - 1).join("")}…`);
  }
  if (host && !said.includes(host.toLowerCase())) parts.push(host);
  return parts.length > 1 ? parts.join(" · ") : null;
}
