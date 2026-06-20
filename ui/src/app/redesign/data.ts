/**
 * Type definitions for the redesign's view models.
 *
 * The demo `DATA` constant that originally lived here has been removed — all
 * screens now consume live API data via `useLiveSystem`. The types are the
 * shared contract between live mappers (`mapping.ts`) and the screens.
 */

export type Side = 'long' | 'short';
export type Urgency = 'high' | 'med' | 'low';

export interface Conviction {
  sym: string;
  side: Side;
  score: number;
  urg: Urgency;
  dir: -1 | 0 | 1;
  fresh: boolean;
  strat: string;
}

export interface Position {
  sym: string;
  name?: string;
  description?: string;
  category?: string;
  logoUrl?: string;
  logoKind?: string;
  exchange?: string;
  currency?: string;
  sector?: string;
  industry?: string;
  qty: number;
  avg: number;
  last: number;
  pnl: number;
  /** Fractional portfolio weight in [0,1], ``notional / nav``. */
  w: number;
  /** Current notional exposure in account currency (``|qty × last|``).
   *  Displayed alongside qty so the operator can see size at a glance. */
  notional: number;
  /** Originating broker (``ibkr``, ``alpaca``, ``binance``, …). */
  broker?: string;
  /** Backend asset class (equity, crypto, forex, ...), used for cash-at-work math. */
  assetClass?: string;
}

export type EventKind = 'fill' | 'signal' | 'tick';

export interface LiveEvent {
  t: number;
  kind: EventKind;
  text: string;
  ok: boolean | null;
}

export interface Approved {
  sym: string;
  side: Side;
  conf: number;
  q: number;
}

export interface Rejected {
  sym: string;
  side: Side;
  reason: string;
  explain: string;
}

export interface ExecutionRejection {
  sym: string;
  side: Side;
  status: 'rejected' | 'cancelled';
  broker: string;
  t: number;
  reason: string | null;
}

export interface NewsRow {
  text: string;
  src: string;
  age: string;
  s: -1 | 0 | 1;
}

export interface NewsSourceStat {
  fresh_rows_in_window: number;
  latest_age_hours: number | null;
  stale: boolean;
  /** ISO timestamp of newest headline ``published_at`` in the scoring window (when present). */
  latest_published_at?: string | null;
  /** ISO timestamp of newest ``fetched_at`` for that source (when present). */
  latest_fetched_at?: string | null;
}

/** One row in the Book screen “News & data” card (NewsAPI, FRED, etc.). */
export interface NewsDataProviderRow {
  id: string;
  label: string;
  configured: boolean;
  state: 'live' | 'stale' | 'never' | 'off' | 'error';
  lastIngestAt: string | null;
  ageLabel: string;
  ok?: boolean;
  error?: string | null;
}

/** Canonical ids/labels; mirrors ``data.ingest_telemetry.NEWS_DATA_PROVIDERS`` (for UI when status omits rows). */
export const NEWS_DATA_PROVIDER_CATALOG: { id: string; label: string }[] = [
  { id: 'newsapi', label: 'NewsAPI' },
  { id: 'alphavantage', label: 'Alpha Vantage' },
  { id: 'finnhub', label: 'Finnhub' },
  { id: 'marketaux', label: 'Marketaux' },
  { id: 'fred', label: 'FRED' },
];

/** Placeholder rows before the first successful ``/system/status`` (or if the field is missing on old servers). */
export function defaultNewsDataProviderRows(): NewsDataProviderRow[] {
  return NEWS_DATA_PROVIDER_CATALOG.map((p) => ({
    id: p.id,
    label: p.label,
    configured: false,
    state: 'off' as const,
    lastIngestAt: null,
    ageLabel: '—',
    ok: true,
    error: null,
  }));
}

export interface Strategy {
  name: string;
  weight: number;
  sharpe: number;
  winRate: number;
  trades: number;
  /** Strategy family reported by the backend registry. Drives the "Arbitrage"
   *  vs "Signal" pill in the Strategy Mix cards and helps the operator
   *  understand that idle signal strategies are still loaded. */
  kind?: 'signal' | 'arbitrage' | string;
  /** True when the strategy is registered but hasn't produced opportunities
   *  in the current snapshot / recent signal window. */
  idle?: boolean;
  /** Whether the strategy is enabled in the trading loop registry. */
  enabled?: boolean;
  /** True only when reported by `/system/status.loaded_strategies`. */
  runtimeLoaded?: boolean;
  /** D033 — pre-execution rollups from ``/diagnostics/strategy-candidates``. */
  mix?: {
    evaluated: number;
    filtered: number;
    counts: {
      no_setup: number;
      generated: number;
      filtered_regime: number;
      filtered_signal_engine: number;
      filtered_meta: number;
      lost_to_strategy: number;
      selected_for_allocation: number;
      risk_rejected: number;
      executed: number;
      skipped: number;
      execution_incomplete?: number;
    };
    lastEvaluatedAt: string | null;
    lastGeneratedAt: string | null;
    topSkipReason: string | null;
    topFailedConditions?: Array<{ key: string; count: number; label: string }>;
    topRiskRejectionReasons?: Array<{ reason: string; count: number }>;
    topExecutionIncomplete?: Array<{ reason: string; count: number }>;
    blockerHint?: string | null;
    /** Machine key, e.g. ``scanning``, ``trading`` */
    lifecycle: string;
    /** Human label for the lifecycle pill */
    lifecycleDisplay: string;
  };
  /** Recent signal confidences (chronological) for this strategy from `/intelligence/signals`. */
  sparkValues?: number[];
  /** Latest confidence from `sparkValues`, when available. */
  lastConfidence?: number | null;
}

export type BrokerUiState = 'live' | 'paper' | 'warming' | 'offline' | 'off';

export interface BrokerStatus {
  name: string;
  state: BrokerUiState;
  /** Backend error text, shown in the row's tooltip when present. */
  error?: string | null;
  /** Whether this broker is excluded from the aggregated NAV right now. */
  excluded?: boolean;
}

/** Portfolio coverage summary mirroring the backend's /system/status.coverage. */
export interface Coverage {
  full: boolean;
  configured: string[];
  included: string[];
  excluded: Array<{
    name: string;
    connected: boolean;
    balance_ready: boolean;
    reason: string;
  }>;
}
