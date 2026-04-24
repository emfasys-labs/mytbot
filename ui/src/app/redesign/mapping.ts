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
  StrategyCandidateMixResponse,
  StrategyMixRow,
} from '../lib/api';
import { toNumber } from '../lib/api';
import { parseAccumulatorScore } from '../lib/dashboardFallbacks';
import { displayConviction01 } from '../lib/scoreDisplay';
import {
  Approved,
  BrokerStatus,
  Conviction,
  Coverage,
  ExecutionRejection,
  LiveEvent,
  NewsRow,
  Position,
  Rejected,
  Strategy,
  Urgency,
} from './data';
import type { SystemState as BackendSystemState } from '../lib/api';
import { prettySymbol } from '../lib/symbol';
import type { SystemState as DesignSystemState } from './tokens';

export { prettySymbol };

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

/**
 * Map raw ``/system/status.brokers`` rows into dashboard badges.
 *
 * The backend distinguishes four meaningful broker states — ``live``,
 * ``warming`` (connected but account snapshot not yet ready), ``offline``
 * (configured but unreachable, with a concrete error), and ``off`` (no
 * credentials). Collapsing the last two into a single "warming" pill — as
 * the original mapper did — lies to the operator: a zombie IB Gateway or a
 * broker with revoked keys looks indistinguishable from one that's going to
 * come up in the next two seconds. The explicit ``offline`` state plus the
 * backend's error text in ``error`` let the BROKERS card render a red pill
 * with a tooltip so the operator immediately sees *why* NAV is partial.
 *
 * ``excluded`` is set when the broker is not contributing to aggregated NAV,
 * which is used by the NAV card footnote to name the missing wallets.
 */
export function mapBrokers(
  brokers: Record<
    string,
    { configured: boolean; connected: boolean; balance_ready?: boolean; error?: string | null }
  >,
  excludedNames?: Set<string>,
): BrokerStatus[] {
  return Object.entries(brokers)
    .map(([name, b]): BrokerStatus => {
      const err = typeof b.error === 'string' && b.error.trim() ? b.error.trim() : null;
      const excluded = excludedNames ? excludedNames.has(name) : undefined;
      if (!b.configured) return { name, state: 'off', error: err, excluded };
      if (!b.connected) {
        // Distinguish "genuinely still connecting" from "broker is down".
        // The only signal we have pre-coverage is the presence of an error
        // from the last connect attempt: if there's a concrete reason, it's
        // not warming — it's a user-actionable failure.
        const state: BrokerStatus['state'] = err ? 'offline' : 'warming';
        return { name, state, error: err, excluded };
      }
      if (b.balance_ready === false) return { name, state: 'warming', error: err, excluded };
      return { name, state: 'live', error: err, excluded };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Coerce the backend's coverage block into the strict shape consumed by the
 * dashboard. Defensive against missing/partial payloads (old backends or
 * in-flight upgrades) so the UI never crashes when the coverage field is
 * absent — it simply falls back to "assume full" and the BROKERS card stays
 * authoritative.
 */
export function mapCoverage(raw: unknown): Coverage | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const asStringList = (v: unknown): string[] => {
    if (!Array.isArray(v)) return [];
    return v.filter((x): x is string => typeof x === 'string' && !!x.trim()).map((s) => s.trim());
  };
  const excluded = Array.isArray(r.excluded)
    ? (r.excluded as Array<Record<string, unknown>>)
        .map((e) => ({
          name: String(e.name ?? '').trim(),
          connected: !!e.connected,
          balance_ready: !!e.balance_ready,
          reason: String(e.reason ?? '').trim(),
        }))
        .filter((e) => !!e.name)
    : [];
  return {
    full: !!r.full,
    configured: asStringList(r.configured),
    included: asStringList(r.included),
    excluded,
  };
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

/**
 * Order statuses that still reserve capital from the allocator's point of
 * view — i.e. unfilled but live orders that haven't been rejected/cancelled.
 * Anything in this set contributes to the ``capital at work`` gauge
 * alongside filled positions.
 */
export function isPendingOrderStatus(status: string | null | undefined): boolean {
  const s = String(status ?? '').toLowerCase();
  return s === 'pending' || s === 'open' || s === 'submitted' || s === 'partially_filled';
}

function _orderFinite(v: unknown): number {
  const n = typeof v === 'number' ? v : Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Notional reserved by unfilled orders: ``|qty × (limit_price or avg_fill)|``
 * across every order whose status is still "open-ish". Shared by the Book
 * screen's Capital-at-work card and the dashboard's capital-allocation
 * slider so the two surfaces can never drift apart.
 */
export function pendingOrderNotional(orders: ApiOrderRow[]): number {
  if (!Array.isArray(orders)) return 0;
  let sum = 0;
  for (const o of orders) {
    if (!isPendingOrderStatus(o.status)) continue;
    // ApiOrderRow only types the canonical fields; ``quantity`` and
    // ``limit_price`` come from the backend's wider OrdersResponse and are
    // accessed through a defensive cast.
    const extras = o as unknown as { quantity?: unknown; limit_price?: unknown };
    const qty = _orderFinite(extras.quantity);
    const px = _orderFinite(extras.limit_price ?? o.avg_fill_price);
    if (qty <= 0 || px <= 0) continue;
    sum += qty * px;
  }
  return sum;
}

/**
 * Combined capital-at-work view used by every headroom-vs-ceiling surface.
 *
 * The backend's ``cap_slider`` gates ``deploy = NAV × ge × cap_slider`` in
 * ``portfolio/allocation_engine.py`` — and "deploy" there covers both new
 * positions AND the buy orders feeding them, because a pending order has
 * already consumed allocator budget. Showing positions-only in the UI
 * slider under-reports the real commitment by the pending-order notional,
 * so the ceiling gauge lies about headroom and the snap landmark sits in
 * the wrong place. This helper is the single source of truth.
 *
 * ``deployed`` — filled-position notional (canonical ``p.notional``).
 * ``pending``  — notional of still-open orders.
 * ``working``  — ``deployed + pending`` (the figure the ceiling gates).
 */
export function capitalAtWork(
  positions: Position[],
  orders: ApiOrderRow[],
): { deployed: number; pending: number; working: number } {
  const deployed = (positions ?? []).reduce((s, p) => s + (p.notional || 0), 0);
  const pending = pendingOrderNotional(orders);
  return { deployed, pending, working: deployed + pending };
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
    // Prefer the authoritative quantity from the backend. The legacy
    // ``unreal / priceDelta`` heuristic silently collapses to zero when the
    // price hasn't moved since entry (freshly opened positions) and mis-signs
    // shorts — using PositionLog.quantity removes both failure modes.
    const qtyRaw = toNumber(p.quantity, NaN);
    let qty: number;
    if (Number.isFinite(qtyRaw) && qtyRaw !== 0) {
      qty = qtyRaw;
    } else {
      const priceDelta = last - avg;
      qty = Math.abs(priceDelta) > 1e-6 ? unreal / priceDelta : 0;
    }
    const notional = Math.abs(qty * last);
    const weight = totalNav > 0 ? notional / totalNav : 0;
    return {
      sym: (p.symbol ?? '').toUpperCase(),
      qty: Number.isFinite(qty) ? Math.round(qty * 100) / 100 : 0,
      avg: Number.isFinite(avg) ? avg : 0,
      last: Number.isFinite(last) ? last : 0,
      pnl: Number.isFinite(unreal) ? unreal : 0,
      w: Math.max(0, Math.min(1, weight)),
      notional: Number.isFinite(notional) ? notional : 0,
      broker: typeof p.broker === 'string' && p.broker.trim() ? p.broker.trim() : undefined,
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
  const nav = toNumber(portfolio.nav, 0);
  const gross = normalizeExposure(portfolio.gross_exposure, nav);
  // Net can legitimately be negative (short bias); clamp to [0,1] for display
  // by taking absolute value — the sign is conveyed via P&L + position sides.
  const net = normalizeExposure(portfolio.net_exposure, nav);
  const cash = Math.max(0, 1 - gross);
  return { gross, net, cash };
}

/** Exposure figures from the backend arrive in three shapes depending on the
 *  snapshot writer:
 *   1. Ratio in [0,1]  (e.g. ``0.54``)
 *   2. Percent 0–100   (e.g. ``54``)
 *   3. Absolute £ notional when ``PortfolioState`` serializes market value
 *      directly (e.g. ``57919.88`` with ``nav=1055095.72``).
 *  Auto-detect by magnitude so the Exposure / Capital-at-work panels never
 *  collapse to ``100%`` just because the writer shipped absolutes. */
function normalizeExposure(raw: unknown, nav: number): number {
  if (raw == null || raw === '') return 0;
  const n = typeof raw === 'number' ? raw : parseFloat(String(raw));
  if (!Number.isFinite(n)) return 0;
  const a = Math.abs(n);
  if (a <= 1) return Math.max(0, Math.min(1, a));
  if (a <= 100) return Math.max(0, Math.min(1, a / 100));
  if (nav > 0) return Math.max(0, Math.min(1, a / nav));
  return 0;
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
  const sym = prettySymbol(o.symbol);
  const txt = isFill
    ? `${sym} ${String(o.side ?? '').toLowerCase()} · ${qty} @ ${px}${o.broker ? ` (${o.broker})` : ''}`
    : `${sym} ${String(o.side ?? '').toLowerCase()} · ${st || 'queued'}`;
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

/** Canonical roster matching `TradingLoop` + arbitrage stack (`config/strategies.yaml`). Used when the system is off or before the loop exposes `loaded_strategies`. */
export const DEFAULT_STRATEGY_MIX_ROSTER: Array<{
  name: string;
  kind: 'signal' | 'arbitrage';
  enabled: boolean;
}> = [
  { name: 'momentum_breakout', kind: 'signal', enabled: true },
  { name: 'mean_reversion', kind: 'signal', enabled: true },
  { name: 'volume_flow', kind: 'signal', enabled: true },
  { name: 'event_driven_news', kind: 'signal', enabled: true },
  { name: 'pairs_trading', kind: 'signal', enabled: true },
  { name: 'volatility_regime', kind: 'signal', enabled: true },
  { name: 'regime_rotation', kind: 'signal', enabled: true },
  { name: 'funding_rate_arbitrage', kind: 'arbitrage', enabled: true },
  { name: 'cross_exchange_arbitrage', kind: 'arbitrage', enabled: true },
];

function _parseSigTs(iso: string | null | undefined): number {
  if (!iso) return 0;
  const t = Date.parse(String(iso));
  return Number.isFinite(t) ? t : 0;
}

/** Pull recent confidences for one strategy from intelligence API (newest window, ascending for sparkline). */
export function intelligenceSparkForStrategy(
  strategyName: string,
  sigs: IntelligenceSignalsResponse | null,
  maxPoints = 24,
): { values: number[]; last: number | null } {
  const key = (strategyName || '').trim();
  if (!key) return { values: [], last: null };
  const rows = sigs?.signals ?? [];
  const matched = rows
    .filter((r) => String(r.strategy ?? '').trim() === key)
    .map((r) => ({
      t: _parseSigTs(r.timestamp),
      c: typeof r.confidence === 'number' && Number.isFinite(r.confidence)
        ? Math.max(0, Math.min(1, r.confidence))
        : NaN,
    }))
    .filter((x) => Number.isFinite(x.c));
  matched.sort((a, b) => a.t - b.t);
  const slice = matched.slice(-maxPoints);
  const values = slice.map((x) => x.c);
  const last = values.length ? values[values.length - 1]! : null;
  if (values.length === 1) {
    return { values: [values[0]!, values[0]!], last };
  }
  return { values, last };
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
  return strategies.sort((a, b) => b.weight - a.weight);
}

function formatLifecycleDisplay(k: string): string {
  switch (k) {
    case 'scanning':
      return 'Scanning';
    case 'finding_setups':
      return 'Finding setups';
    case 'competing':
      return 'Competing';
    case 'selected':
      return 'Selected';
    case 'trading':
      return 'Trading';
    case 'blocked_by_risk':
      return 'Blocked by risk';
    case 'idle':
      return 'Idle';
    default:
      return k.replace(/_/g, ' ');
  }
}

function buildStrategyMixView(m: StrategyMixRow): NonNullable<Strategy['mix']> {
  const c = m.counts;
  const filtered = c.filtered_regime + c.filtered_signal_engine + c.filtered_meta;
  return {
    evaluated: m.evaluated,
    filtered,
    counts: { ...c, execution_incomplete: c.execution_incomplete ?? 0 },
    lastEvaluatedAt: m.last_evaluated_at,
    lastGeneratedAt: m.last_generated_at,
    topSkipReason: m.top_skip_reason?.reason ?? null,
    topFailedConditions: m.top_failed_conditions,
    topRiskRejectionReasons: m.top_risk_rejection_reasons,
    topExecutionIncomplete: m.top_execution_incomplete,
    blockerHint: m.blocker_hint ?? null,
    lifecycle: m.lifecycle,
    lifecycleDisplay: formatLifecycleDisplay(m.lifecycle),
  };
}

function emptyStrategyMixView(): NonNullable<Strategy['mix']> {
  return {
    evaluated: 0,
    filtered: 0,
    counts: {
      no_setup: 0,
      generated: 0,
      filtered_regime: 0,
      filtered_signal_engine: 0,
      filtered_meta: 0,
      lost_to_strategy: 0,
      selected_for_allocation: 0,
      risk_rejected: 0,
      executed: 0,
      skipped: 0,
      execution_incomplete: 0,
    },
    lastEvaluatedAt: null,
    lastGeneratedAt: null,
    topSkipReason: null,
    topFailedConditions: undefined,
    topRiskRejectionReasons: undefined,
    topExecutionIncomplete: undefined,
    blockerHint: null,
    lifecycle: 'idle',
    lifecycleDisplay: 'Idle',
  };
}

export function mergeStrategiesWithSignals(
  snapshotStrategies: Strategy[],
  sigs: IntelligenceSignalsResponse | null,
  loadedStrategies: Array<{ name: string; enabled: boolean; kind?: string }> = [],
  mixResponse: StrategyCandidateMixResponse | null = null,
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

  for (const d of DEFAULT_STRATEGY_MIX_ROSTER) {
    if (out.has(d.name)) continue;
    out.set(d.name, {
      name: d.name,
      weight: 0,
      sharpe: 0,
      winRate: 0,
      trades: 0,
      kind: d.kind,
      enabled: d.enabled,
      idle: true,
    });
  }

  const kindRank = (k: string | undefined) => (String(k).toLowerCase() === 'arbitrage' ? 1 : 0);

  const mixByName = new Map<string, StrategyMixRow>();
  if (mixResponse?.strategies?.length) {
    for (const row of mixResponse.strategies) {
      const n = (row.name ?? '').trim();
      if (n) mixByName.set(n, row);
    }
  }
  const mixApiOk = !!(mixResponse && !mixResponse.error && Array.isArray(mixResponse.strategies));

  return [...out.values()]
    .map((s) => {
      const sp = intelligenceSparkForStrategy(s.name, sigs);
      const hasTrace = sp.values.length >= 2;
      const row = mixByName.get(s.name);
      let mix: Strategy['mix'] | undefined;
      if (mixApiOk) {
        mix = row ? buildStrategyMixView(row) : emptyStrategyMixView();
      } else if (row) {
        mix = buildStrategyMixView(row);
      } else {
        mix = undefined;
      }
      const idle =
        mix != null ? mix.evaluated === 0 : !!s.idle;
      return {
        ...s,
        sparkValues: hasTrace ? sp.values : undefined,
        lastConfidence: hasTrace ? sp.last : null,
        sharpe: hasTrace && sp.last != null ? sp.last : s.sharpe,
        mix,
        idle,
      };
    })
    .sort((a, b) => {
      if (b.weight !== a.weight) return b.weight - a.weight;
      if (!!a.idle !== !!b.idle) return a.idle ? 1 : -1;
      const kd = kindRank(a.kind as string | undefined) - kindRank(b.kind as string | undefined);
      if (kd !== 0) return kd;
      return a.name.localeCompare(b.name);
    });
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
