import type { ApiOrderRow, DashboardSnapshot, IntelligenceSignalsResponse } from './api';
import {
  bandFromDisplay01,
  displayConviction01,
  stanceFromDisplay01,
} from './scoreDisplay';

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

export function parseOpportunityRowScore(o: Record<string, unknown>): number {
  const v = o.opportunity_score;
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = parseFloat(v);
    if (Number.isFinite(n)) return n;
  }
  return 0;
}

export function convictionRowsFromSnapshot(snapshot: DashboardSnapshot | null): Array<Record<string, unknown>> {
  const acc = snapshot?.accumulator;
  return ((acc?.top_by_magnitude ?? []) as Array<Record<string, unknown>>).slice(0, 12);
}

export function bullishBearishFromSnapshot(snapshot: DashboardSnapshot | null): {
  bullish: Array<Record<string, unknown>>;
  bearish: Array<Record<string, unknown>>;
} {
  const acc = snapshot?.accumulator;
  const bull = ((acc?.bullish_top ?? []) as Array<Record<string, unknown>>).slice(0, 6);
  const bear = ((acc?.bearish_top ?? []) as Array<Record<string, unknown>>).slice(0, 6);
  if (bull.length > 0 || bear.length > 0) {
    return { bullish: bull, bearish: bear };
  }
  const top = convictionRowsFromSnapshot(snapshot);
  const bullish = top.filter((r) => parseAccumulatorScore(r) > 0).slice(0, 6);
  const bearish = top.filter((r) => parseAccumulatorScore(r) < 0).slice(0, 6);
  return { bullish, bearish };
}

/** When D015 opportunities[] is empty, surface accumulator or positions so the centre column is never a void. */
export function buildFallbackOpportunities(
  snapshot: DashboardSnapshot | null,
  positions: Array<{ symbol: string; change: number }>,
): Array<Record<string, unknown>> {
  const rows = convictionRowsFromSnapshot(snapshot);
  if (rows.length > 0) {
    return rows.map((r) => {
      const raw = parseAccumulatorScore(r);
      const d = displayConviction01(raw);
      return {
        symbol: r.symbol,
        opportunity_score: d.toFixed(2),
        raw_score: raw,
        display01: d,
        tags: [bandFromDisplay01(d), 'accumulator'],
        components: r.components,
        confidence: r.confidence,
      };
    });
  }
  const fromPos = positions.slice(0, 8).map((p) => {
    const raw = Math.min(1, Math.abs(p.change) / 100) * Math.sign(p.change || 1);
    const d = displayConviction01(raw);
    return {
      symbol: p.symbol,
      opportunity_score: d.toFixed(2),
      raw_score: raw,
      display01: d,
      tags: [stanceFromDisplay01(d), 'book'],
    };
  });
  if (fromPos.length) return fromPos;
  return [{ symbol: '—', opportunity_score: '—', tags: ['awaiting pipeline / loop'] }];
}

export function buildFallbackHoldPressure(
  positions: Array<{ symbol: string; change: number }>,
): Array<Record<string, unknown>> {
  if (!positions.length) return [];
  const sorted = [...positions].sort((a, b) => a.change - b.change);
  const changes = sorted.map((p) => p.change);
  const min = Math.min(...changes);
  const max = Math.max(...changes);
  const span = max - min || 1;
  const maxAbs = Math.max(...changes.map((c) => Math.abs(c)), 1e-6);
  return sorted.slice(0, 8).map((p) => {
    const t = (p.change - min) / span;
    const hold = 0.22 + t * 0.68;
    const exit = Math.min(1, Math.abs(p.change) / maxAbs);
    return {
      symbol: p.symbol,
      hold_score: hold.toFixed(2),
      exit_pressure: exit.toFixed(2),
    };
  });
}

/** Bottom strip: ranked conviction → intel signals → positions. */
export function buildWatchlistRanked(
  snapshot: DashboardSnapshot | null,
  intelligence: IntelligenceSignalsResponse | null,
  positions: Array<{ symbol: string; change: number }>,
): Array<{ symbol: string; score: number; note: string }> {
  const acc = convictionRowsFromSnapshot(snapshot);
  if (acc.length) {
    return acc.slice(0, 12).map((r) => {
      const raw = parseAccumulatorScore(r);
      const d = displayConviction01(raw);
      return {
        symbol: String(r.symbol ?? ''),
        score: d,
        note: bandFromDisplay01(d),
      };
    });
  }
  const sigs = intelligence?.signals ?? [];
  const seen = new Set<string>();
  const out: Array<{ symbol: string; score: number; note: string }> = [];
  for (const s of sigs) {
    const sym = (s.symbol ?? '').toUpperCase();
    if (!sym || seen.has(sym)) continue;
    seen.add(sym);
    const conf = typeof s.confidence === 'number' ? s.confidence : 0;
    const d = displayConviction01(conf);
    out.push({
      symbol: sym,
      score: d,
      note: bandFromDisplay01(d),
    });
    if (out.length >= 12) break;
  }
  if (out.length) return out;
  return positions.slice(0, 10).map((p) => {
    const raw = Math.min(1, Math.abs(p.change) / 100) * Math.sign(p.change || 1);
    const d = displayConviction01(raw);
    return {
      symbol: p.symbol,
      score: d,
      note: stanceFromDisplay01(d),
    };
  });
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
