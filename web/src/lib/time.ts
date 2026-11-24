const time = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
const day = new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" });
const full = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });

/** List stamp: time today, date this year, full date before. */
export function listTimestamp(iso: string, now = new Date()): string {
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  if (value.toDateString() === now.toDateString()) return time.format(value);
  if (value.getFullYear() === now.getFullYear()) return day.format(value);
  return full.format(value);
}

/** Sidebar stamp: the clock today, the month and day before that. */
export function shortTime(iso: string, now = new Date()): string {
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  if (value.toDateString() === now.toDateString()) return time.format(value);
  return day.format(value);
}

/** A moment told apart from its neighbours: the clock today, else the date and the clock. */
export function momentStamp(iso: string, now = new Date()): string {
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  if (value.toDateString() === now.toDateString()) return time.format(value);
  const date = value.getFullYear() === now.getFullYear() ? day.format(value) : full.format(value);
  return `${date} ${time.format(value)}`;
}
