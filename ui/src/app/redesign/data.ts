/**
 * Demo data for the "living instrument" redesign prototype.
 * Ported from mytbot-design-system/project/prototypes/redesign/tokens_data.jsx.
 * Production wiring to real APIs lives in ui/src/app/App.tsx (legacy shell).
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

export interface DemoData {
  nav: number;
  navOpen: number;
  navPeak: number;
  pnl: { d: number; w: number; m: number; y: number };
  exposure: { gross: number; net: number; cash: number };
  loop: number;
  path: string;
  broker: BrokerStatus[];
  conviction: Conviction[];
  positions: Position[];
  events: LiveEvent[];
  approved: Approved[];
  rejected: Rejected[];
  news: NewsRow[];
  equity: number[];
  strategies: Strategy[];
}

export const DATA: DemoData = {
  nav: 84231.42,
  navOpen: 82991.0,
  navPeak: 84562.0,
  pnl: { d: 1240.42, w: 3810.12, m: 11240.0, y: 12831.45 },
  exposure: { gross: 0.72, net: 0.61, cash: 0.39 },
  loop: 47,
  path: 'D015',
  broker: [
    { name: 'ibkr',    state: 'live' },
    { name: 'kraken',  state: 'live' },
    { name: 'binance', state: 'warming' },
    { name: 'alpaca',  state: 'off' },
  ],
  conviction: [
    { sym: 'NVDA', side: 'long',  score: 0.84, urg: 'high', dir: 1,  fresh: true,  strat: 'momentum_breakout' },
    { sym: 'AAPL', side: 'long',  score: 0.71, urg: 'med',  dir: 1,  fresh: false, strat: 'momentum_breakout' },
    { sym: 'MSFT', side: 'long',  score: 0.63, urg: 'med',  dir: 0,  fresh: false, strat: 'mean_reversion' },
    { sym: 'BTC',  side: 'long',  score: 0.58, urg: 'med',  dir: 1,  fresh: false, strat: 'trend_follow' },
    { sym: 'AMZN', side: 'long',  score: 0.48, urg: 'low',  dir: 1,  fresh: false, strat: 'momentum_breakout' },
    { sym: 'SPY',  side: 'long',  score: 0.42, urg: 'low',  dir: 0,  fresh: false, strat: 'regime_filter' },
    { sym: 'NFLX', side: 'short', score: 0.28, urg: 'low',  dir: -1, fresh: false, strat: 'mean_reversion' },
    { sym: 'META', side: 'short', score: 0.18, urg: 'med',  dir: -1, fresh: false, strat: 'momentum_breakout' },
    { sym: 'TSLA', side: 'short', score: 0.09, urg: 'high', dir: -1, fresh: false, strat: 'mean_reversion' },
  ],
  positions: [
    { sym: 'NVDA', qty: 48,    avg: 871.4,  last: 882.44, pnl: 529.92,  w: 0.24 },
    { sym: 'AAPL', qty: 120,   avg: 178.2,  last: 182.44, pnl: 508.8,   w: 0.18 },
    { sym: 'MSFT', qty: 42,    avg: 408.1,  last: 411.6,  pnl: 147.0,   w: 0.15 },
    { sym: 'BTC',  qty: 0.12,  avg: 42100,  last: 43211,  pnl: 133.32,  w: 0.11 },
    { sym: 'ETH',  qty: 1.8,   avg: 3150,   last: 3142,   pnl: -14.4,   w: 0.07 },
    { sym: 'META', qty: -20,   avg: 512.0,  last: 521.4,  pnl: -188.0,  w: 0.05 },
  ],
  events: [
    { t: 10,  kind: 'fill',   text: 'NVDA long · 12 @ 882.40',           ok: true },
    { t: 64,  kind: 'signal', text: 'MSFT long 0.63 · approved',         ok: true },
    { t: 180, kind: 'signal', text: 'META short 0.18 · asset_class_limit', ok: false },
    { t: 244, kind: 'signal', text: 'AMZN long 0.48 · approved',         ok: true },
    { t: 360, kind: 'tick',   text: 'loop #47 · path D015',              ok: null },
    { t: 420, kind: 'fill',   text: 'AAPL long · 20 @ 182.44',           ok: true },
    { t: 605, kind: 'signal', text: 'SPY long 0.42 · approved',          ok: true },
    { t: 680, kind: 'tick',   text: 'loop #46 · path D015',              ok: null },
  ],
  approved: [
    { sym: 'NVDA', side: 'long', conf: 84.2, q: 0.81 },
    { sym: 'AAPL', side: 'long', conf: 71.3, q: 0.72 },
    { sym: 'MSFT', side: 'long', conf: 63.8, q: 0.68 },
  ],
  rejected: [
    { sym: 'META', side: 'short', reason: 'asset_class_limit', explain: 'Asset-class bucket at configured cap.' },
    { sym: 'TSLA', side: 'short', reason: 'max_position',      explain: 'Would exceed max position size.' },
  ],
  news: [
    { text: 'Fed holds rates · dot plot signals 2 cuts by year-end', src: 'reuters',  age: '4m',  s: 0 },
    { text: 'NVDA beats Q4 estimates · revenue up 122% YoY',         src: 'bbg',      age: '12m', s: 1 },
    { text: 'Crypto market slides 3.2% on regulatory concern',       src: 'coindesk', age: '18m', s: -1 },
    { text: 'AAPL supply chain disruption reported in Taiwan',       src: 'ft',       age: '31m', s: -1 },
    { text: 'MSFT Azure growth accelerates to 31% in Q4',            src: 'cnbc',     age: '42m', s: 1 },
    { text: 'UK GDP beats expectations at 0.4% monthly growth',      src: 'ons',      age: '1h',  s: 0 },
  ],
  equity: [80000, 80400, 81200, 80800, 82100, 83000, 82600, 83800, 82900, 83400, 84100, 84562, 84231],
  strategies: [
    { name: 'momentum_breakout', weight: 0.42, sharpe: 1.82, winRate: 0.61, trades: 12 },
    { name: 'mean_reversion',    weight: 0.28, sharpe: 1.14, winRate: 0.54, trades: 8 },
    { name: 'trend_follow',      weight: 0.18, sharpe: 0.92, winRate: 0.49, trades: 4 },
    { name: 'regime_filter',     weight: 0.12, sharpe: 0.41, winRate: 0.42, trades: 2 },
  ],
};
