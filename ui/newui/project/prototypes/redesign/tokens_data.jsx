// ─── Design tokens + live fake data ──────────────────────────
// The "living instrument" design language for mytbot.

window.TOKENS = {
  // Neutrals — warm near-black
  bg0: '#050505',
  bg1: '#0a0a0b',
  bg2: '#111113',
  bg3: '#17171a',
  line: 'rgba(255,255,255,0.06)',
  lineStrong: 'rgba(255,255,255,0.12)',
  ink0: '#ffffff',
  ink1: 'rgba(255,255,255,0.92)',
  ink2: 'rgba(255,255,255,0.62)',
  ink3: 'rgba(255,255,255,0.42)',
  ink4: 'rgba(255,255,255,0.22)',

  // Semantic — calibrated for this accent system
  profit: '#5eead4',          // teal-300 — calm green
  loss: '#fda4af',             // rose-300
  caution: '#fcd34d',           // amber-300
  danger: '#f87171',            // red-400
  info: '#93c5fd',              // blue-300

  // Fonts
  sans: "'Geist', -apple-system, system-ui, sans-serif",
  mono: "'Geist Mono', ui-monospace, monospace",

  // Motion
  ease: 'cubic-bezier(0.22, 1, 0.36, 1)',   // snappy settle
  easeSoft: 'cubic-bezier(0.4, 0, 0.2, 1)',
  fast: 160,
  med: 300,
  slow: 600,
};

// Tweakable accent palette
window.ACCENTS = {
  cyan:    { main: '#67e8f9', dim: 'rgba(103,232,249,0.18)', glow: 'rgba(103,232,249,0.35)' },
  amber:   { main: '#fbbf24', dim: 'rgba(251,191,36,0.18)',  glow: 'rgba(251,191,36,0.35)' },
  emerald: { main: '#34d399', dim: 'rgba(52,211,153,0.18)',  glow: 'rgba(52,211,153,0.35)' },
  violet:  { main: '#c4b5fd', dim: 'rgba(196,181,253,0.18)', glow: 'rgba(196,181,253,0.35)' },
  coral:   { main: '#fb7185', dim: 'rgba(251,113,133,0.18)', glow: 'rgba(251,113,133,0.35)' },
  white:   { main: '#ffffff', dim: 'rgba(255,255,255,0.12)', glow: 'rgba(255,255,255,0.22)' },
};

// ─── Fake data ────────────────────────────────────────────────
window.DATA = {
  nav: 84231.42,
  navOpen: 82991.00,
  navPeak: 84562.00,
  pnl: { d: 1240.42, w: 3810.12, m: 11240.00, y: 12831.45 },
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
    { sym: 'MSFT', side: 'long',  score: 0.63, urg: 'med',  dir: 0,  fresh: false, strat: 'mean_reversion'    },
    { sym: 'BTC',  side: 'long',  score: 0.58, urg: 'med',  dir: 1,  fresh: false, strat: 'trend_follow'      },
    { sym: 'AMZN', side: 'long',  score: 0.48, urg: 'low',  dir: 1,  fresh: false, strat: 'momentum_breakout' },
    { sym: 'SPY',  side: 'long',  score: 0.42, urg: 'low',  dir: 0,  fresh: false, strat: 'regime_filter'     },
    { sym: 'NFLX', side: 'short', score: 0.28, urg: 'low',  dir: -1, fresh: false, strat: 'mean_reversion'    },
    { sym: 'META', side: 'short', score: 0.18, urg: 'med',  dir: -1, fresh: false, strat: 'momentum_breakout' },
    { sym: 'TSLA', side: 'short', score: 0.09, urg: 'high', dir: -1, fresh: false, strat: 'mean_reversion'    },
  ],
  positions: [
    { sym: 'NVDA', qty: 48,    avg: 871.40, last: 882.44, pnl: 529.92,  w: 0.24 },
    { sym: 'AAPL', qty: 120,   avg: 178.20, last: 182.44, pnl: 508.80,  w: 0.18 },
    { sym: 'MSFT', qty: 42,    avg: 408.10, last: 411.60, pnl: 147.00,  w: 0.15 },
    { sym: 'BTC',  qty: 0.12,  avg: 42100,  last: 43211,  pnl: 133.32,  w: 0.11 },
    { sym: 'ETH',  qty: 1.8,   avg: 3150,   last: 3142,   pnl: -14.40,  w: 0.07 },
    { sym: 'META', qty: -20,   avg: 512.00, last: 521.40, pnl: -188.00, w: 0.05 },
  ],
  events: [
    { t: 10,    kind: 'fill',    text: 'NVDA long · 12 @ 882.40',     ok: true },
    { t: 64,    kind: 'signal',  text: 'MSFT long 0.63 · approved',   ok: true },
    { t: 180,   kind: 'signal',  text: 'META short 0.18 · asset_class_limit', ok: false },
    { t: 244,   kind: 'signal',  text: 'AMZN long 0.48 · approved',   ok: true },
    { t: 360,   kind: 'tick',    text: 'loop #47 · path D015',        ok: null },
    { t: 420,   kind: 'fill',    text: 'AAPL long · 20 @ 182.44',     ok: true },
    { t: 605,   kind: 'signal',  text: 'SPY long 0.42 · approved',    ok: true },
    { t: 680,   kind: 'tick',    text: 'loop #46 · path D015',        ok: null },
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
    { text: 'Fed holds rates · dot plot signals 2 cuts by year-end', src: 'reuters', age: '4m', s: 0 },
    { text: 'NVDA beats Q4 estimates · revenue up 122% YoY',         src: 'bbg',     age: '12m', s: 1 },
    { text: 'Crypto market slides 3.2% on regulatory concern',       src: 'coindesk',age: '18m', s: -1 },
    { text: 'AAPL supply chain disruption reported in Taiwan',       src: 'ft',      age: '31m', s: -1 },
    { text: 'MSFT Azure growth accelerates to 31% in Q4',            src: 'cnbc',    age: '42m', s: 1 },
    { text: 'UK GDP beats expectations at 0.4% monthly growth',      src: 'ons',     age: '1h',  s: 0 },
  ],
  equity: [80000,80400,81200,80800,82100,83000,82600,83800,82900,83400,84100,84562,84231],
  strategies: [
    { name: 'momentum_breakout', weight: 0.42, sharpe: 1.82, winRate: 0.61, trades: 12 },
    { name: 'mean_reversion',     weight: 0.28, sharpe: 1.14, winRate: 0.54, trades: 8  },
    { name: 'trend_follow',       weight: 0.18, sharpe: 0.92, winRate: 0.49, trades: 4  },
    { name: 'regime_filter',      weight: 0.12, sharpe: 0.41, winRate: 0.42, trades: 2  },
  ],
};
