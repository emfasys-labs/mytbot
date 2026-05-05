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
    case 'stopping': return 'stopping';
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
 *
 * When ``orchestratorIdle`` is true (orchestrator ``off`` or ``stopping``),
 * any ``warming`` row is shown as ``off`` instead: the trading stack is not
 * actively bringing brokers up, so "warming" would read like a hung connect.
 * ``live`` and ``offline`` are preserved (still useful if a venue stayed up or
 * failed with a concrete error).
 */
export function mapBrokers(
  brokers: Record<
    string,
    { configured: boolean; connected: boolean; balance_ready?: boolean; error?: string | null }
  >,
  excludedNames?: Set<string>,
  orchestratorIdle = false,
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
    .map((row): BrokerStatus => {
      if (orchestratorIdle && row.state === 'warming') {
        return { ...row, state: 'off', error: null };
      }
      return row;
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

const FOREX_PAIRS = new Set([
  'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'NZDUSD', 'USDCAD',
  'EURJPY', 'GBPJPY', 'EURGBP', 'EURCHF', 'AUDJPY', 'EURAUD', 'EURCAD',
  'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPNZD', 'AUDCAD', 'AUDCHF', 'AUDNZD',
  'CADJPY', 'CHFJPY', 'NZDJPY', 'USDSEK', 'USDNOK', 'USDDKK', 'USDZAR',
  'USDMXN', 'USDTRY', 'USDHKD', 'USDSGD', 'USDCNH',
]);

function inferAssetClass(assetClass: string | null | undefined, symbol: string | null | undefined): string {
  let key = String(assetClass ?? '').trim().toLowerCase() || 'equity';
  const sym = String(symbol ?? '').trim().toUpperCase();
  if (key === 'equity' || key === 'stock' || key === '') {
    if (sym.endsWith('=X') || (sym.length === 6 && FOREX_PAIRS.has(sym))) key = 'forex';
    else if (sym.endsWith('=F')) key = 'future';
    else if (sym.endsWith('-USD') || sym.endsWith('-USDT') || sym.endsWith('USDT')) key = 'crypto';
  }
  return key;
}

function cashFactor(assetClass: string | null | undefined, symbol: string | null | undefined): number {
  switch (inferAssetClass(assetClass, symbol)) {
    case 'forex':
    case 'fx':
      return 0.05;
    case 'future':
      return 0.15;
    case 'bond':
      return 0.20;
    default:
      return 1.0;
  }
}

function cashNotional(notional: number, assetClass: string | null | undefined, symbol: string | null | undefined): number {
  if (!Number.isFinite(notional) || notional <= 0) return 0;
  return notional * cashFactor(assetClass, symbol);
}

/**
 * Notional reserved by unfilled exposure-increasing orders.
 *
 * Closing/reduce orders do not consume new capital: a sell against an existing
 * long position releases exposure, so counting it here makes capital-at-work
 * look roughly doubled while trims are waiting for the broker session.
 */
export function pendingOrderNotional(orders: ApiOrderRow[], positions: Position[] = []): number {
  if (!Array.isArray(orders)) return 0;
  const positionBySymbol = new Map(
    (positions ?? []).map((p) => [String(p.sym ?? '').toUpperCase(), p]),
  );
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
    const side = String(o.side ?? '').toLowerCase();
    const sym = String(o.symbol ?? '').toUpperCase();
    const pos = positionBySymbol.get(sym);
    const posQty = pos?.qty ?? 0;
    if (side === 'sell' && posQty > 0) continue;
    if (side === 'buy' && posQty < 0) continue;
    sum += cashNotional(qty * px, pos?.assetClass, sym);
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
 * ``pending``  — notional of still-open exposure-increasing orders.
 * ``working``  — ``deployed + pending`` (the figure the ceiling gates).
 */
export function capitalAtWork(
  positions: Position[],
  orders: ApiOrderRow[],
): { deployed: number; pending: number; working: number } {
  const deployed = (positions ?? []).reduce(
    (s, p) => s + cashNotional(p.notional || 0, p.assetClass, p.sym),
    0,
  );
  const pending = pendingOrderNotional(orders, positions);
  return { deployed, pending, working: deployed + pending };
}

export function mapPositions(
  pos: ApiPositionsResponse | null,
  totalNav: number,
): Position[] {
  const mapped = (pos?.positions ?? []).map<Position | null>((p) => {
    const avg = toNumber(p.avg_entry_price, 0);
    const last = toNumber(p.current_price, avg);
    const unreal = toNumber(p.unrealised_pnl, 0);
    const qtyRaw = toNumber(p.quantity, NaN);
    let qty: number;
    if (Number.isFinite(qtyRaw) && qtyRaw !== 0) {
      qty = qtyRaw;
    } else {
      const priceDelta = last - avg;
      qty = Math.abs(priceDelta) > 1e-6 ? unreal / priceDelta : 0;
    }
    const notional = Math.abs(qty * last);
    if (
      !Number.isFinite(qty)
      || !Number.isFinite(notional)
      || Math.abs(qty) <= 1e-8
      || notional <= 0.005
    ) {
      return null;
    }
    return {
      sym: (p.symbol ?? '').toUpperCase(),
      qty: Number.isFinite(qty) ? Math.round(qty * 100) / 100 : 0,
      avg: Number.isFinite(avg) ? avg : 0,
      last: Number.isFinite(last) ? last : 0,
      pnl: Number.isFinite(unreal) ? unreal : 0,
      w: 0,
      notional: Number.isFinite(notional) ? notional : 0,
      broker: typeof p.broker === 'string' && p.broker.trim() ? p.broker.trim() : undefined,
      assetClass: typeof p.asset_class === 'string' && p.asset_class.trim()
        ? p.asset_class.trim().toLowerCase()
        : undefined,
    };
  }).filter((p): p is Position => p !== null);
  const sumNot = mapped.reduce((a, p) => a + (Number.isFinite(p.notional) && p.notional > 0 ? p.notional : 0), 0);
  const wDenom = sumNot > 0 ? sumNot : totalNav;
  return mapped.map((p) => ({
    ...p,
    w: wDenom > 0 ? Math.max(0, Math.min(1, p.notional / wDenom)) : 0,
  }));
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

/**
 * Local calendar YYYY-MM-DD in the browser timezone.
 */
function ymdLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/** Monday (local) of the current week, as YYYY-MM-DD. */
function mondayLocalYmd(d: Date): string {
  const c = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const dow = c.getDay();
  const delta = dow === 0 ? -6 : 1 - dow;
  c.setDate(c.getDate() + delta);
  return ymdLocal(c);
}

function firstOfMonthLocalYmd(d: Date): string {
  return ymdLocal(new Date(d.getFullYear(), d.getMonth(), 1));
}

function janFirstLocalYmd(d: Date): string {
  return ymdLocal(new Date(d.getFullYear(), 0, 1));
}

function hasPartialBrokerCoverage(pnl: ApiPnlResponse | null): boolean {
  const status = pnl?.today?.nav_status;
  if (!status) return false;
  if (status.coverage_full === false) return true;
  const excluded = status.excluded;
  return Array.isArray(excluded) && excluded.length > 0;
}

/**
 * Intraday baseline NAV: last persisted end-of-day ``portfolio_value`` before **today**; otherwise a coarse fallback.
 * Used for the hero day-change and ``mapPnlRollups`` **d** (unused in the shell but kept consistent).
 */
export function navOpenFromHistory(
  navNow: number,
  pnl: ApiPnlResponse | null,
  history: Array<{ date: string; value: number }>,
): number {
  const today = ymdLocal(new Date());
  const sorted = [...(history ?? [])]
    .filter((h): h is { date: string; value: number } =>
      !!h && typeof h === 'object' && typeof h.date === 'string' && Number.isFinite(h.value) && h.value > 0,
    )
    .sort((a, b) => a.date.localeCompare(b.date));
  const before = sorted.filter((h) => h.date < today);
  const todayPnl = toNumber(pnl?.today?.realised, 0) + toNumber(pnl?.today?.unrealised, 0);
  if (hasPartialBrokerCoverage(pnl)) {
    const open = navNow - todayPnl;
    return Number.isFinite(open) && open > 0 ? open : navNow;
  }
  if (before.length) {
    const last = before[before.length - 1];
    if (last && Number.isFinite(last.value)) {
      const delta = navNow - last.value;
      // Broker/history NAV can be flat across a restart even while live MTM
      // moved. In that case use the API live P&L as the intraday baseline.
      if (Math.abs(delta) < 0.01 && Math.abs(todayPnl) > 0.01) {
        const open = navNow - todayPnl;
        return Number.isFinite(open) && open > 0 ? open : last.value;
      }
      return last.value;
    }
  }
  const open = navNow - todayPnl;
  return Number.isFinite(open) && open > 0 ? open : navNow;
}

/**
 * Change in ``navNow`` from the last persisted ``portfolio_value`` in ``history`` **before** ``periodStart`` (inclusive
 * of prior close). Sums of daily ``unrealised`` from the API are not a reliable P&L (levels were aggregated).
 * Falls back to API period sums if there is no usable history.
 */
function navChangeSincePeriodStart(
  history: Array<{ date: string; value: number }>,
  navNow: number,
  periodStartYmd: string,
  apiFallback: number,
): number {
  const sorted = [...(history ?? [])]
    .filter((h): h is { date: string; value: number } =>
      !!h && typeof h === 'object' && typeof h.date === 'string' && Number.isFinite(h.value) && h.value > 0,
    )
    .sort((a, b) => a.date.localeCompare(b.date));
  if (!Number.isFinite(navNow) || navNow <= 0) return 0;
  if (!sorted.length) return apiFallback;
  const before = sorted.filter((h) => h.date < periodStartYmd);
  let anchor: number;
  if (before.length > 0) {
    // True period-open: previous close immediately before the period start.
    anchor = before[before.length - 1].value;
  } else {
    // History does not reach back to the period start. Use the first row
    // on/after the period start as a best-effort period-open NAV (we joined
    // mid-period). Without that, defer to the API-side rollup so each
    // period does not silently collapse onto the same earliest history row.
    const onOrAfter = sorted.find((h) => h.date >= periodStartYmd);
    if (!onOrAfter || !Number.isFinite(onOrAfter.value) || onOrAfter.value <= 0) {
      return apiFallback;
    }
    anchor = onOrAfter.value;
  }
  if (!Number.isFinite(anchor) || anchor <= 0) return apiFallback;
  const delta = navNow - anchor;
  if (Math.abs(delta) < 0.01 && Math.abs(apiFallback) > 0.01) return apiFallback;
  return delta;
}

export function mapPnlRollups(
  pnl: ApiPnlResponse | null,
  navNow: number,
  history: Array<{ date: string; value: number }>,
): {
  d: number; w: number; m: number; y: number;
} {
  const wApi = toNumber(pnl?.week?.realised, 0) + toNumber(pnl?.week?.unrealised, 0);
  const mApi = toNumber(pnl?.month?.realised, 0) + toNumber(pnl?.month?.unrealised, 0);
  const yApi = toNumber(pnl?.all_time?.realised, 0) + toNumber(pnl?.all_time?.unrealised, 0);
  const todayApi = toNumber(pnl?.today?.realised, 0) + toNumber(pnl?.today?.unrealised, 0);
  if (hasPartialBrokerCoverage(pnl)) {
    return { d: todayApi, w: 0, m: 0, y: 0 };
  }
  const now = new Date();
  const weekStart = mondayLocalYmd(now);
  const monthStart = firstOfMonthLocalYmd(now);
  const ytdStart = janFirstLocalYmd(now);
  const open = navOpenFromHistory(navNow, pnl, history);
  return {
    d: navNow - open,
    w: navChangeSincePeriodStart(history, navNow, weekStart, wApi),
    m: navChangeSincePeriodStart(history, navNow, monthStart, mApi),
    y: navChangeSincePeriodStart(history, navNow, ytdStart, yApi),
  };
}

export function mapExposure(
  snapshot: DashboardSnapshot | null,
  pnl: ApiPnlResponse | null = null,
): {
  gross: number;
  net: number;
  cash: number;
  navBasis: 'snapshot' | 'pnl_today_portfolio_value' | 'none';
  navDivergencePct: number | null;
} {
  const portfolio = (snapshot?.portfolio ?? {}) as Record<string, unknown>;
  const navSnapshot = toNumber(portfolio.nav, 0);
  const navPnl = toNumber(pnl?.today?.portfolio_value, 0);
  const hasSnapshot = Number.isFinite(navSnapshot) && navSnapshot > 0;
  const hasPnl = Number.isFinite(navPnl) && navPnl > 0;
  const navDivergencePct =
    hasSnapshot && hasPnl
      ? Math.abs(navSnapshot - navPnl) / Math.max(navSnapshot, navPnl)
      : null;

  // If snapshot NAV diverges heavily from `/pnl` NAV, exposure percentages
  // can spike to absurd values (e.g. 2500%+) despite a normal live equity.
  // Prefer `/pnl` as denominator in that case while still surfacing a warning
  // in the UI that the two feeds disagree.
  const usePnlFallback = hasPnl && (!hasSnapshot || (navDivergencePct ?? 0) > 0.5);
  const nav = usePnlFallback ? navPnl : navSnapshot;
  const navBasis: 'snapshot' | 'pnl_today_portfolio_value' | 'none' =
    nav > 0 ? (usePnlFallback ? 'pnl_today_portfolio_value' : 'snapshot') : 'none';

  const grossRaw = normalizeExposure(portfolio.gross_exposure, nav);
  const sampleGross = sampleGrossExposure(portfolio.positions_sample, nav);
  const gross = Math.max(grossRaw, sampleGross);
  // Net can legitimately be negative (short bias); clamp to [0,1] for display
  // by taking absolute value — the sign is conveyed via P&L + position sides.
  const netRaw = normalizeExposure(portfolio.net_exposure, nav);
  const net = Math.max(netRaw, sampleGross);
  const cash = Math.max(0, 1 - gross);
  return { gross, net, cash, navBasis, navDivergencePct };
}

function sampleGrossExposure(raw: unknown, nav: number): number {
  if (nav <= 0 || !Array.isArray(raw)) return 0;
  let total = 0;
  for (const row of raw) {
    if (!row || typeof row !== 'object') continue;
    const mv = toNumber((row as Record<string, unknown>).market_value, 0);
    if (Number.isFinite(mv)) total += Math.abs(mv);
  }
  return total > 0 ? total / nav : 0;
}

/** Exposure figures from the backend arrive in three shapes depending on the
 *  snapshot writer:
 *   1. Ratio in [0,1]  (e.g. ``0.54``)
 *   2. Percent 0–100   (e.g. ``54``)
 *   3. Absolute notional when ``PortfolioState`` serializes market value
 *      directly (e.g. ``2440782`` with ``nav=1102438``).
 *  Auto-detect by magnitude. The result is **not clamped** at 1.0 — a
 *  margined paper account legitimately runs 2–3× gross/NAV, and clamping
 *  to 1.0 hid that fact behind a friendly "100%" lie. The ring caps the
 *  visual arc at one full revolution but the displayed number is the
 *  true ratio. */
function normalizeExposure(raw: unknown, nav: number): number {
  if (raw == null || raw === '') return 0;
  const n = typeof raw === 'number' ? raw : parseFloat(String(raw));
  if (!Number.isFinite(n)) return 0;
  const a = Math.abs(n);
  if (a <= 1) return Math.max(0, a);
  if (a <= 100) return Math.max(0, a / 100);
  if (nav > 0) return Math.max(0, a / nav);
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
  kind: 'signal' | 'arbitrage' | 'factor' | 'relative_value' | 'options';
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
  { name: 'factor_sleeve', kind: 'factor', enabled: true },
  { name: 'stat_arb_pairs', kind: 'relative_value', enabled: true },
  { name: 'options_long_call', kind: 'options', enabled: true },
  { name: 'options_long_put', kind: 'options', enabled: true },
  { name: 'options_protective_put', kind: 'options', enabled: true },
  { name: 'options_covered_call', kind: 'options', enabled: true },
];

const INTERNAL_ALLOCATION_ACTIONS = new Set([
  'global_edge_trim',
  'trim_symbol',
]);

function isStrategyScreenEligible(name: string | null | undefined): boolean {
  const key = String(name ?? '').trim();
  return key.length > 0 && !INTERNAL_ALLOCATION_ACTIONS.has(key);
}

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
    if (!isStrategyScreenEligible(name)) continue;
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
      if (!isStrategyScreenEligible(nameRaw)) continue;
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
    if (!isStrategyScreenEligible(name)) continue;
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
      if (isStrategyScreenEligible(n)) mixByName.set(n, row);
    }
  }
  const mixApiOk = !!(mixResponse && !mixResponse.error && Array.isArray(mixResponse.strategies));

  return [...out.values()]
    .filter((s) => isStrategyScreenEligible(s.name))
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
  history: Array<{ date: string; value: number }>,
): number {
  return navOpenFromHistory(navNow, pnl, history);
}

export function equityPeak(values: number[], navNow: number): number {
  if (!values.length) return Math.max(navNow, 0);
  return Math.max(navNow, ...values.filter((v) => Number.isFinite(v)));
}

/** Drop bad ``0`` / negative daily ``portfolio_value`` samples that cause V-shaped chart glitches. */
export function forwardFillNavSeries(values: number[]): number[] {
  let last = 0;
  return values.map((v) => {
    if (Number.isFinite(v) && v > 0) {
      last = v;
      return v;
    }
    return last;
  });
}
