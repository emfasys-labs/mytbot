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
  w: number;
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

export interface NewsRow {
  text: string;
  src: string;
  age: string;
  s: -1 | 0 | 1;
}

export interface Strategy {
  name: string;
  weight: number;
  sharpe: number;
  winRate: number;
  trades: number;
}

export interface BrokerStatus {
  name: string;
  state: 'live' | 'warming' | 'off';
}
