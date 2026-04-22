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
}

export type BrokerUiState = 'live' | 'warming' | 'offline' | 'off';

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
