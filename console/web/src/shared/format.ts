// Display formatters shared across the console's stat surfaces. Pure, allocation-
// light wrappers over Intl so every page renders numbers, money, percentages, and
// dates the same way. Null/NaN collapse to a placeholder dash so a missing metric
// never prints "NaN" or "undefined".

const numFmt = new Intl.NumberFormat('en-US');
const usdFmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
const pctFmt = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 1 });

export const fmtNum = (n: number | null | undefined) => (n == null ? '-' : numFmt.format(n));

export function fmtCompact(n: number | null | undefined): string {
  if (n == null) return '-';
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return numFmt.format(n);
}

/** USD from a dollar amount (the metrics API already reports dollars, not cents). */
export const fmtUsd = (dollars: number | null | undefined) => (dollars == null ? '-' : usdFmt.format(dollars));

export const fmtPct = (x: number | null | undefined) =>
  x == null || Number.isNaN(x) ? '-' : pctFmt.format(x);

/** A latency in milliseconds rendered as seconds with one decimal (governance KPIs). */
export const fmtSeconds = (ms: number | null | undefined) =>
  ms == null ? '-' : `${(ms / 1000).toFixed(1)}s`;

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/**
 * Privacy mask for an identifier that looks like an email: keep the first two
 * characters of the local part, star the rest, keep the domain. A plain handle
 * (an OS username with no '@') is returned untouched, since there is nothing to
 * hide and masking it would only obscure who owns the runs.
 *   alice.kim@acme.com -> al*******@acme.com
 *   ubuntu             -> ubuntu
 */
export function maskHandle(handle: string | null | undefined): string {
  if (!handle) return '';
  const at = handle.lastIndexOf('@');
  if (at < 1) return handle;
  const local = handle.slice(0, at);
  const domain = handle.slice(at);
  if (local.length <= 2) return handle;
  return local.slice(0, 2) + '*'.repeat(Math.max(3, local.length - 2)) + domain;
}
