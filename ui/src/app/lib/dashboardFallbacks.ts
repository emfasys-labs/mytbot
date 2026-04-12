import type { ApiOrderRow, DashboardSnapshot, IntelligenceSignalsResponse } from './api';

/** Shown in copy when allocator returns no ranked rows but accumulator still has scores. */
export const OPPORTUNITY_THRESHOLD_HINT = 0.6;

export function parseAccumulatorScore(row: Record<string, unknown>): number {
  const s = row.score;
  if (typeof s === 'number' && Number.isFinite(s)) return s;
  if (typeof s === 'string') {
    const n = parseFloat(s);
    if (Number.isFinite(n)) return n;
  }
  return 0;
}

export function convictionRowsFromSnapshot(snapshot: DashboardSnapshot | null): Array<Record<string, unknown>> {
  const acc = snapshot?.accumulator;
  return ((acc?.top_by_magnitude ?? []) as Array<Record<string, unknown>>).slice(0, 12);
}

/** When D015 opportunities[] is empty, surface accumulator or positions so the centre column is never a void. */
export function buildFallbackOpportunities(
  snapshot: DashboardSnapshot | null,
  positions: Array<{ symbol: string; change: number }>,
): Array<Record<string, unknown>> {
  const rows = convictionRowsFromSnapshot(snapshot);
  if (rows.length > 0) {
    return rows.map((r) => ({
      symbol: r.symbol,
      opportunity_score: parseAccumulatorScore(r).toFixed(4),
      tags: ['accumulator', String(r.direction ?? '')].filter(Boolean),
    }));
  }
  const fromPos = positions.slice(0, 8).map((p) => ({
    symbol: p.symbol,
    opportunity_score: (Math.min(1, Math.abs(p.change) / 100) * 0.5).toFixed(4),
    tags: ['position'],
  }));
  if (fromPos.length) return fromPos;
  return [{ symbol: '—', opportunity_score: '—', tags: ['awaiting pipeline / loop'] }];
}

export function buildFallbackHoldPressure(
  positions: Array<{ symbol: string; change: number }>,
): Array<Record<string, unknown>> {
  return [...positions]
    .sort((a, b) => a.change - b.change)
    .slice(0, 8)
    .map((p) => ({
      symbol: p.symbol,
      hold_score: (0.5 + p.change / 200).toFixed(2),
      exit_pressure: p.change < 0 ? (Math.min(1, Math.abs(p.change) / 100)).toFixed(2) : '0.00',
    }));
}

/** Bottom strip: ranked conviction → intel signals → positions. */
export function buildWatchlistRanked(
  snapshot: DashboardSnapshot | null,
  intelligence: IntelligenceSignalsResponse | null,
  positions: Array<{ symbol: string; change: number }>,
): Array<{ symbol: string; score: number; note: string }> {
  const acc = convictionRowsFromSnapshot(snapshot);
  if (acc.length) {
    return acc.slice(0, 12).map((r) => ({
      symbol: String(r.symbol ?? ''),
      score: parseAccumulatorScore(r),
      note: 'conviction',
    }));
  }
  const sigs = intelligence?.signals ?? [];
  const seen = new Set<string>();
  const out: Array<{ symbol: string; score: number; note: string }> = [];
  for (const s of sigs) {
    const sym = (s.symbol ?? '').toUpperCase();
    if (!sym || seen.has(sym)) continue;
    seen.add(sym);
    out.push({
      symbol: sym,
      score: typeof s.confidence === 'number' ? s.confidence : 0,
      note: (s.strategy ?? 'signal').replace(/_/g, ' '),
    });
    if (out.length >= 12) break;
  }
  if (out.length) return out;
  return positions.slice(0, 10).map((p) => ({
    symbol: p.symbol,
    score: Math.min(1, Math.abs(p.change) / 100),
    note: 'position',
  }));
}

/** Map filled orders to equity-curve indices; colour = day-over-day portfolio change on that date. */
export function buildEquityTradeMarkers(
  historyDates: Array<{ date: string; value: number }>,
  orders: ApiOrderRow[],
): Array<{ index: number; positive: boolean }> {
  if (historyDates.length < 2 || !orders.length) return [];
  const filled = orders.filter((o) => {
    const st = (o.status ?? '').toLowerCase();
    return o.timestamp && (st === 'filled' || st === 'partially_filled');
  });
  const out: Array<{ index: number; positive: boolean }> = [];
  const seen = new Set<number>();
  for (const o of filled.slice(0, 40)) {
    const day = (o.timestamp ?? '').slice(0, 10);
    if (!day) continue;
    const idx = historyDates.findIndex((h) => (h.date || '').slice(0, 10) === day);
    if (idx < 0) continue;
    if (seen.has(idx)) continue;
    seen.add(idx);
    const prev = historyDates[idx - 1]?.value;
    const cur = historyDates[idx]?.value ?? 0;
    const positive = prev == null ? true : cur >= prev;
    out.push({ index: idx, positive });
  }
  return out;
}
