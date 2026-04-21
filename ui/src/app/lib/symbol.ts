/**
 * Display-side helpers for ticker symbols.
 *
 * Our data pipeline internally carries yfinance-style suffixes so the
 * universe resolver can disambiguate asset classes (``=X`` for forex,
 * ``=F`` for futures). Those suffixes are purely internal — operators on
 * a broker terminal would type ``EURUSD`` / ``ES``, not ``EURUSD=X`` /
 * ``ES=F``. Use ``prettySymbol`` at every render site so the UI matches
 * broker conventions while the in-memory state keeps the raw symbol.
 */
export function prettySymbol(raw: string | null | undefined): string {
  if (!raw) return '';
  const s = String(raw).trim().toUpperCase();
  if (s.endsWith('=X') || s.endsWith('=F')) return s.slice(0, -2);
  return s;
}
