/**
 * Dashboard number formatting: at most two digits after the decimal, with locale grouping.
 */

function isDashSentinel(s: string): boolean {
  return s === '' || s === '—' || s === '-';
}

/**
 * Format numeric API values (strings or numbers). Non-numeric strings are returned unchanged.
 */
export function fmtDashNum(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return '—';
    return value.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  }
  const raw = String(value).trim();
  if (isDashSentinel(raw)) return '—';
  const n = Number(raw.replace(/,/g, ''));
  if (!Number.isFinite(n)) return raw;
  return n.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
}

/** Signed currency for PnL / NAV (no leading +; minus uses −). */
export function fmtDashMoneySigned(n: number, currency = '£'): string {
  if (!Number.isFinite(n)) return '—';
  const sign = n < 0 ? '−' : '';
  const abs = Math.abs(n);
  const body = abs.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  return `${sign}${currency}${body}`;
}
