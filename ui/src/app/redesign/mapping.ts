/**
 * Adapters that translate live API shapes into the prototype's demo-data
 * shape (see ./data.ts). Keeps redesign screens purely presentational.
 */

import type {
  ApiNewsResponse,
  ApiOrderRow,
  ApiPnlResponse,
  ApiPositionsResponse,
  DashboardSnapshot,
  IntelligenceSignalsResponse,
} from '../lib/api';
import { toNumber } from '../lib/api';
import { parseAccumulatorScore } from '../lib/dashboardFallbacks';
import { displayConviction01 } from '../lib/scoreDisplay';
import {
  Approved,
  BrokerStatus,
  Conviction,
  ExecutionRejection,
  LiveEvent,
  NewsRow,
  Position,
  Rejected,
  Strategy,
  Urgency,
} from './data';
import type { SystemState as BackendSystemState } from '../lib/api';
import type { SystemState as DesignSystemState } from './tokens';

export function mapSystemState(
  backend: BackendSystemState,
  killSwitch: boolean,
): DesignSystemState {
  if (killSwitch) return 'paused';
  switch (backend) {
    case 'running':  return 'running';
    case 'starting': return 'starting';
    case 'stopping': return 'starting';
    case 'error':    return 'error';
    case 'off':
    default:         return 'off';
  }
}

export function mapBrokers(
  brokers: Record<
    string,
    { configured: boolean; connected: boolean; balance_ready?: boolean }
  >,
): BrokerStatus[] {
  return Object.entries(brokers)
    .map(([name, b]): BrokerStatus => {
      if (!b.configured) return { name, state: 'off' };
      if (!b.connected) return { name, state: 'warming' };
      if (b.balance_ready === false) return { name, state: 'warming' };
      return { name, state: 'live' };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

function urgencyFromDisplay(d: number): Urgency {
  if (d >= 0.7) return 'high';
  if (d >= 0.45) return 'med';
  return 'low';
}

function strategyFromRow(r: Record<string, unknown>): string {
  const s = r.strategy_name ?? r.strategy;
  if (typeof s === 'string' && s.trim()) return s;
  return 'accumulator';
}

export function mapConviction(
  snapshot: DashboardSnapshot | null,
  positionsFallback: Array<{ symbol: string; change: number }>,
): Conviction[] {
  const acc = snapshot?.accumulator;
  const bull = ((acc?.bullish_top ?? []) as Array<Record<string, unknown>>).slice(0, 12);
  const bear = ((acc?.bearish_top ?? []) as Array<Record<string, unknown>>).slice(0, 12);
  const accRows = [...bull, ...bear];

  const rows: Conviction[] = [];
  const seen = new Set<string>();

  if (accRows.length) {
    for (const r of accRows) {
      const sym = String(r.symbol ?? '').toUpperCase();
      if (!sym || seen.has(sym)) continue;
      seen.add(sym);
      const raw = parseAccumulatorScore(r);
      const d = displayConviction01(raw);
      rows.push({
        sym,
        side: raw >= 0 ? 'long' : 'short',
        score: d,
        urg: urgencyFromDisplay(d),
        dir: raw > 0.01 ? 1 : raw < -0.01 ? -1 : 0,
        fresh: false,
        strat: strategyFromRow(r),
      });
    }
  }

  // Also merge in opportunities (priority ranking) for symbols not already present.
  const opps = (snapshot?.opportunities ?? []) as Array<Record<string, unknown>>;
  for (const o of opps.slice(0, 12)) {
    const sym = String(o.symbol ?? '').toUpperCase();
    if (!sym || seen.has(sym)) continue;
    seen.add(sym);
    const rawScore =
      typeof o.opportunity_score === 'number'
        ? o.opportunity_score
        : typeof o.priority_score === 'number'
          ? o.priority_score
          : parseFloat(String(o.opportunity_score ?? o.priority_score ?? '0'));
    const raw = Number.isFinite(rawScore) ? rawScore : 0;
    const d = displayConviction01(raw);
    const side: Conviction['side'] = normalizeSide(o.side);
    rows.push({
      sym,
      side,
      score: d,
      urg: urgencyFromDisplay(d),
      dir: side === 'short' ? -1 : 1,
      fresh: false,
      strat: strategyFromRow(o),
    });
  }

  if (rows.length) return rows.slice(0, 12);

  // Final fallback: positions as weak conviction proxies.
  return positionsFallback.slice(0, 8).map<Conviction>((p) => {
    const raw = Math.min(1, Math.abs(p.change) / 100) * Math.sign(p.change || 1);
    const d = displayConviction01(raw);
    return {
      sym: p.symbol.toUpperCase(),
      side: p.change >= 0 ? 'long' : 'short',
      score: d,
      urg: urgencyFromDisplay(d),
      dir: p.change > 0 ? 1 : p.change < 0 ? -1 : 0,
      fresh: false,
      strat: 'book',
    };
  });
}

export function mapPositions(
  pos: ApiPositionsResponse | null,
  totalNav: number,
): Position[] {
  const rows = pos?.positions ?? [];
  return rows.slice(0, 24).map<Position>((p) => {
    const avg = toNumber(p.avg_entry_price, 0);
    const last = toNumber(p.current_price, avg);
    const unreal = toNumber(p.unrealised_pnl, 0);
    // qty is not exposed on the raw positions endpoint we use, so derive it.
    const priceDelta = last - avg;
    const qtyGuess =
      Math.abs(priceDelta) > 1e-6 ? unreal / priceDelta : 0;
    const notional = Math.abs(qtyGuess * last);
    const weight = totalNav > 0 ? notional / totalNav : 0;
    return {
      sym: (p.symbol ?? '').toUpperCase(),
      qty: Number.isFinite(qtyGuess) ? Math.round(qtyGuess * 100) / 100 : 0,
      avg: Number.isFinite(avg) ? avg : 0,
      last: Number.isFinite(last) ? last : 0,
      pnl: Number.isFinite(unreal) ? unreal : 0,
      w: Math.max(0, Math.min(1, weight)),
    };
  });
}

export function normalizeSide(raw: unknown): 'long' | 'short' {
  const s = String(raw ?? '').toLowerCase();
  if (s === 'short' || s === 'sell') return 'short';
  return 'long';
}

export function mapApprovedRejected(
  sigs: IntelligenceSignalsResponse | null,
): { approved: Approved[]; rejected: Rejected[] } {
  const rows = sigs?.signals ?? [];
  const approved: Approved[] = [];
  const rejected: Rejected[] = [];
  for (const s of rows) {
    const verdict = (s.verdict ?? '').toLowerCase();
    const side = normalizeSide(s.side);
    if (verdict === 'approved') {
      approved.push({
        sym: (s.symbol ?? '').toUpperCase(),
        side,
        conf: typeof s.confidence === 'number' ? s.confidence * 100 : 0,
        q: typeof s.quality_score === 'number' ? s.quality_score : 0,
      });
    } else {
      const reason = (s.risk_reason || verdict || 'blocked')
        .replace(/^failed:\s*/i, '')
        .replace(/^rejected:\s*/i, '')
        .trim();
      rejected.push({
        sym: (s.symbol ?? '').toUpperCase(),
        side,
        reason,
        explain: explainReason(reason),
      });
    }
  }
  return { approved, rejected };
}

function explainReason(code: string): string {
  const k = code.toLowerCase().replace(/\s+/g, '_');
  if (k.includes('asset_class')) return 'Asset-class bucket at configured cap.';
  if (k.includes('max_exposure')) return 'Would exceed max exposure.';
  if (k.includes('max_position')) return 'Would exceed max position size.';
  if (k.includes('max_orders')) return 'Order rate or count limit.';
  if (k.includes('kill_switch')) return 'Kill switch active.';
  if (k.includes('news_veto')) return 'News filter veto.';
  if (k.includes('liquidity')) return 'Liquidity / spread check failed.';
  if (k.includes('correlation')) return 'Correlation / concentration limit.';
  if (k.includes('options_trading')) return 'Options trading policy.';
  return 'Risk check failed.';
}

export function mapNews(news: ApiNewsResponse | null): NewsRow[] {
  const headlines = news?.headlines ?? [];
  const scores = news?.ai_scores ?? [];
  const aiMap = new Map<string, -1 | 0 | 1>();
  for (const ai of scores) {
    if (!ai.symbol || ai.score == null) continue;
    const s = parseFloat(String(ai.score));
    if (!Number.isFinite(s)) continue;
    aiMap.set(ai.symbol.toUpperCase(), s > 0.2 ? 1 : s < -0.2 ? -1 : 0);
  }
  return headlines.slice(0, 24).map<NewsRow>((h) => {
    let s: -1 | 0 | 1 = 0;
    for (const v of aiMap.values()) { s = v; break; }
    return {
      text: h.title,
      src: h.source || '—',
      age: ageSince(h.published_at),
      s,
    };
  });
}

function ageSince(ts: string | null | undefined): string {
  if (!ts) return '—';
  const t = Date.parse(String(ts));
  if (!Number.isFinite(t)) return '—';
  const secs = Math.max(0, (Date.now() - t) / 1000);
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

export function mapPnlRollups(pnl: ApiPnlResponse | null): {
  d: number; w: number; m: number; y: number;
} {
  const today = (toNumber(pnl?.today?.realised, 0) + toNumber(pnl?.today?.unrealised, 0));
  const week = (toNumber(pnl?.week?.realised, 0) + toNumber(pnl?.week?.unrealised, 0));
  const month = (toNumber(pnl?.month?.realised, 0) + toNumber(pnl?.month?.unrealised, 0));
  // Year rollup is not exposed; use month as a sensible, non-stale proxy.
  return { d: today, w: week, m: month, y: month };
}

export function mapExposure(
  snapshot: DashboardSnapshot | null,
): { gross: number; net: number; cash: number } {
  const portfolio = (snapshot?.portfolio ?? {}) as Record<string, unknown>;
  const gross = parsePct(portfolio.gross_exposure);
  const net = parsePct(portfolio.net_exposure);
  const cash = Math.max(0, 1 - gross);
  return { gross, net, cash };
}

function parsePct(raw: unknown): number {
  if (raw == null || raw === '') return 0;
  const n = typeof raw === 'number' ? raw : parseFloat(String(raw));
  if (!Number.isFinite(n) || n < 0) return 0;
  // Backend exposure is typically a ratio [0,1]; accept percent values too.
  const v = n > 1 ? n / 100 : n;
  return Math.max(0, Math.min(1, v));
}

export function mapExecutionRejections(orders: ApiOrderRow[]): ExecutionRejection[] {
  if (!Array.isArray(orders)) return [];
  const out: ExecutionRejection[] = [];
  for (const o of orders) {
    const status = (o.status ?? '').toLowerCase();
    if (status !== 'rejected' && status !== 'cancelled') continue;
    if (!o.symbol) continue;
    const reasonRaw =
      (o as { reason?: unknown }).reason ??
      (o as { error_message?: unknown }).error_message;
    const reason = typeof reasonRaw === 'string' && reasonRaw.trim() ? reasonRaw.trim() : null;
    out.push({
      sym: o.symbol.toUpperCase(),
      side: normalizeSide(o.side),
      status: status as 'rejected' | 'cancelled',
      broker: (o.broker ?? '').toLowerCase(),
      t: o.timestamp ? Date.parse(String(o.timestamp)) : Date.now(),
      reason,
    });
  }
  return out;
}

export function mapOrderEvent(o: ApiOrderRow): LiveEvent | null {
  if (!o.symbol) return null;
  const st = (o.status ?? '').toLowerCase();
  const isFill = st === 'filled' || st === 'partially_filled';
  const kind: LiveEvent['kind'] = isFill ? 'fill' : 'signal';
  const qty = o.filled_quantity ?? '';
  const px = o.avg_fill_price ?? '';
  const txt = isFill
    ? `${o.symbol} ${String(o.side ?? '').toLowerCase()} · ${qty} @ ${px}${o.broker ? ` (${o.broker})` : ''}`
    : `${o.symbol} ${String(o.side ?? '').toLowerCase()} · ${st || 'queued'}`;
  return {
    t: o.timestamp ? Date.parse(String(o.timestamp)) : Date.now(),
    kind,
    text: txt,
    ok: isFill ? true : st === 'cancelled' || st === 'rejected' ? false : null,
  };
}

export function mapOrdersToTradeLog(orders: ApiOrderRow[]) {
  return orders.slice(0, 80).map((o) => {
    const st = (o.status ?? '').toLowerCase();
    const isFill = st === 'filled' || st === 'partially_filled';
    const isReject = st === 'rejected' || st === 'cancelled';
    const sideNorm: 'long' | 'short' = normalizeSide(o.side);
    const kind: 'fill' | 'signal' | 'reject' | 'tick' = isFill ? 'fill' : isReject ? 'reject' : 'signal';
    return {
      t: (o.timestamp ?? '').replace('T', ' ').slice(0, 19) || '—',
      kind,
      sym: o.symbol,
      side: sideNorm,
      qty: o.filled_quantity ? Number(o.filled_quantity) : undefined,
      price: o.avg_fill_price ? Number(o.avg_fill_price) : undefined,
      venue: o.broker ?? undefined,
      reason: isReject ? st : undefined,
      ok: isFill ? true : isReject ? false : null,
    };
  });
}

export function mapStrategies(snapshot: DashboardSnapshot | null): Strategy[] {
  const opps = (snapshot?.opportunities ?? []) as Array<Record<string, unknown>>;
  if (!opps.length) return [];
  const agg = new Map<string, { total: number; count: number; confSum: number }>();
  let grandTotal = 0;
  for (const o of opps) {
    const name = strategyFromRow(o);
    const score =
      typeof o.priority_score === 'number'
        ? o.priority_score
        : typeof o.opportunity_score === 'number'
          ? o.opportunity_score
          : parseFloat(String(o.priority_score ?? o.opportunity_score ?? '0'));
    const s = Number.isFinite(score) ? Math.max(0, score) : 0;
    const confRaw = o.confidence;
    const conf =
      typeof confRaw === 'number' ? confRaw : parseFloat(String(confRaw ?? '0'));
    const e = agg.get(name) ?? { total: 0, count: 0, confSum: 0 };
    e.total += s;
    e.count += 1;
    e.confSum += Number.isFinite(conf) ? conf : 0;
    agg.set(name, e);
    grandTotal += s;
  }
  const strategies: Strategy[] = [];
  for (const [name, v] of agg.entries()) {
    const weight = grandTotal > 0 ? v.total / grandTotal : 0;
    const avgConf = v.count > 0 ? v.confSum / v.count : 0;
    strategies.push({
      name,
      weight,
      // We don't have realised Sharpe/win-rate in-snapshot; surface average confidence
      // in place of sharpe so the card isn't blank, and winRate uses avg confidence.
      sharpe: avgConf,
      winRate: avgConf,
      trades: v.count,
    });
  }
  return strategies.sort((a, b) => b.weight - a.weight).slice(0, 8);
}

export function mergeStrategiesWithSignals(
  snapshotStrategies: Strategy[],
  sigs: IntelligenceSignalsResponse | null,
  loadedStrategies: Array<{ name: string; enabled: boolean; kind?: string }> = [],
): Strategy[] {
  const out = new Map<string, Strategy>();
  for (const s of snapshotStrategies) out.set(s.name, s);

  const rows = sigs?.signals ?? [];
  if (rows.length) {
    const sigAgg = new Map<string, { count: number; confSum: number }>();
    let total = 0;
    for (const r of rows) {
      const nameRaw = String(r.strategy ?? '').trim();
      if (!nameRaw) continue;
      const conf = typeof r.confidence === 'number' && Number.isFinite(r.confidence)
        ? Math.max(0, Math.min(1, r.confidence))
        : 0;
      const e = sigAgg.get(nameRaw) ?? { count: 0, confSum: 0 };
      e.count += 1;
      e.confSum += conf;
      sigAgg.set(nameRaw, e);
      total += 1;
    }
    if (total > 0) {
      for (const [name, v] of sigAgg.entries()) {
        if (out.has(name)) continue;
        const avgConf = v.count > 0 ? v.confSum / v.count : 0;
        out.set(name, {
          name,
          weight: v.count / total,
          sharpe: avgConf,
          winRate: avgConf,
          trades: v.count,
        });
      }
    }
  }

  // Seed every strategy the backend has registered — even if it produced zero
  // opportunities and zero signals in the window — so the operator can see
  // the full strategy roster. Without this the Strategy Mix card would
  // collapse to a single entry whenever the regime favours one strategy.
  for (const ls of loadedStrategies) {
    const name = (ls?.name ?? '').trim();
    if (!name) continue;
    if (out.has(name)) {
      const prev = out.get(name)!;
      out.set(name, { ...prev, kind: ls.kind ?? prev.kind, enabled: ls.enabled });
      continue;
    }
    out.set(name, {
      name,
      weight: 0,
      sharpe: 0,
      winRate: 0,
      trades: 0,
      kind: ls.kind,
      enabled: ls.enabled,
      idle: true,
    });
  }

  return [...out.values()]
    .sort((a, b) => b.weight - a.weight || (a.idle === b.idle ? 0 : a.idle ? 1 : -1))
    .slice(0, 12);
}

export function estimateNavOpen(
  navNow: number,
  pnl: ApiPnlResponse | null,
): number {
  const today = toNumber(pnl?.today?.realised, 0) + toNumber(pnl?.today?.unrealised, 0);
  const open = navNow - today;
  return Number.isFinite(open) && open > 0 ? open : navNow;
}

export function equityPeak(values: number[], navNow: number): number {
  if (!values.length) return Math.max(navNow, 0);
  return Math.max(navNow, ...values.filter((v) => Number.isFinite(v)));
}
