// ─── Tokens, accent, mock data shaped honestly ───────────────────────
// Every count has a freshness state. Universe is read-only by default.

window.UNI = window.UNI || {};

UNI.TOKENS = {
  bg0:'#050505', bg1:'#0a0a0b', bg2:'#111113', bg3:'#17171a',
  line:'rgba(255,255,255,0.06)', lineStrong:'rgba(255,255,255,0.12)',
  ink0:'#ffffff', ink1:'rgba(255,255,255,0.92)', ink2:'rgba(255,255,255,0.62)',
  ink3:'rgba(255,255,255,0.42)', ink4:'rgba(255,255,255,0.22)',
  profit:'#5eead4', loss:'#fda4af', caution:'#fcd34d', danger:'#f87171', info:'#93c5fd',
  // accent for Universe — slightly different to Dashboard so the section reads distinct
  accent:'#a5b4fc',  // indigo-300
  accentDim:'rgba(165,180,252,0.18)',
  accentGlow:'rgba(165,180,252,0.4)',
  sans:"'Geist', -apple-system, system-ui, sans-serif",
  mono:"'Geist Mono', ui-monospace, monospace",
  ease:'cubic-bezier(0.22, 1, 0.36, 1)',
};

// Stage colours — tuned to read as a temperature ramp
// All-hex so `${c}HH` alpha-suffix concatenation produces valid CSS uniformly.
UNI.STAGE_COLORS = {
  source:    '#9ca3af',   // neutral grey
  eligible:  '#93c5fd',   // info
  watching:  '#a5b4fc',   // accent
  promoted:  '#fcd34d',   // caution / hot
  active:    '#5eead4',   // profit
  banned:    '#f87171',   // danger
};
UNI.STAGE_LABELS = {
  source:'source pool',
  eligible:'eligible',
  watching:'watching',
  promoted:'promoted',
  active:'active',
  banned:'banned',
};
UNI.STAGE_DESC = {
  source:'Full venue catalogue — every symbol the broker exposes.',
  eligible:'Passes hard filters: liquidity, asset class, region, data freshness, broker availability, spread.',
  watching:'Continuously evaluated by the factor pipeline. Up to 300 symbols (50 core + 250 scan).',
  promoted:'Recently surfaced as candidates for the engine. Scored against active strategies.',
  active:'Currently held in the book.',
  banned:'Excluded — manually banned, auto-blacklisted, or correlation-rejected.',
};

// Build status — drives the global pill + local detail card
UNI.BUILD = {
  state: 'fresh',          // 'fresh' | 'building' | 'stale' | 'error' | 'pending'
  lastBuildAt: Date.now() - 1000 * 47,         // 47s ago
  nextBuildAt: Date.now() + 1000 * 73,         // in 73s
  loopId: 4218,
  durationMs: 6420,
};

// Funnel: every count has a freshness flag.
UNI.FUNNEL = [
  { stage:'source',   count: 8742, fresh:true,  drops: null },
  { stage:'eligible', count: 1284, fresh:true,  drops: [
      { reason:'Low liquidity (<£50k ADV)', count: 4118 },
      { reason:'Wrong asset class for region',  count: 2104 },
      { reason:'Data freshness > 5min',          count: 642 },
      { reason:'Broker unavailable',             count: 320 },
      { reason:'Spread too wide (> 20bps)',      count: 274 },
  ]},
  { stage:'watching', count: 300,  fresh:true,  drops: [
      { reason:'Capacity cap (300 max)',         count: 821 },
      { reason:'Correlation overlap',            count: 98 },
      { reason:'Low opportunity score',          count: 65 },
  ]},
  { stage:'promoted', count: 27,   fresh:true,  drops: [
      { reason:'No strategy fit',                count: 198 },
      { reason:'Conviction below threshold',     count: 62 },
      { reason:'Macro regime mismatch',          count: 13 },
  ]},
  { stage:'active',   count: 7,    fresh:true,  drops: null },
  { stage:'banned',   count: 14,   fresh:true,  drops: null },
];

// 50 core + a slice of scan + recently promoted/active
const CLASSES = ['equity','crypto','etf','fx','bond'];
const SECTORS = {
  equity:   ['tech','consumer','health','financial','energy','industrial'],
  crypto:   ['layer1','defi','infra'],
  etf:      ['broad','sector','factor'],
  fx:       ['major','minor'],
  bond:     ['sovereign','corp']
};
const NEWS = ['earnings beat','rate decision','geopolitics','regulator filing','sector rotation','liquidity event'];

function rng(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 0xffffffff; };
}
const r = rng(20260427);

function makeSymbol(i, stage, klass, override) {
  const sec = SECTORS[klass][Math.floor(r() * SECTORS[klass].length)];
  const baseSym = (klass === 'crypto') ? ['BTC','ETH','SOL','LINK','MATIC','AVAX','ATOM','DOT','UNI','AAVE','LDO','OP','ARB','RPL'][i % 14] + (i > 13 ? `-${i}` : '')
                : (klass === 'fx') ? ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','EURJPY','GBPJPY','EURGBP'][i % 8]
                : (klass === 'bond') ? ['UST10','UST2','BUND10','GILT10','JGB10','OAT10'][i % 6]
                : (klass === 'etf') ? ['SPY','QQQ','IWM','XLK','XLE','XLF','XLV','VTI','SMH','EFA','EEM'][i % 11]
                : ['NVDA','AAPL','MSFT','TSLA','META','AMZN','GOOG','AMD','NFLX','CRM','SHOP','UBER','SNOW','PLTR','ASML','TSM','SAP','ORCL','ADBE','LIN','BRK','JNJ','UNH','XOM','CVX','PG','KO','PEP','MCD','WMT','HD','LOW','NKE','V','MA','AXP','GS','MS','BAC','C','JPM','WFC','BLK','SCHW','HON','CAT','DE','BA','GE','MMM'][i % 50];
  const factors = {
    momentum:    Math.round(r() * 100),
    meanRev:     Math.round(r() * 100),
    volRegime:   Math.round(r() * 100),
    liquidity:   Math.round(40 + r() * 60),
    news:        Math.round(r() * 100),
    correlation: Math.round(r() * 100),
    macro:       Math.round(r() * 100),
    strategyFit: Math.round(r() * 100),
  };
  const conviction = stage === 'promoted' ? Math.round(60 + r() * 40)
                   : stage === 'active' ? Math.round(70 + r() * 30)
                   : stage === 'watching' ? Math.round(20 + r() * 50)
                   : Math.round(r() * 30);
  const trend = r() > 0.5 ? 'rising' : r() > 0.5 ? 'steady' : 'falling';
  // promotion sparkline — last 12 hourly samples
  const spark = [];
  let v = conviction - Math.round(r() * 20);
  for (let k = 0; k < 12; k++) { v += Math.round((r() - 0.45) * 12); spark.push(Math.max(0, Math.min(100, v))); }
  spark[11] = conviction;
  return {
    sym: baseSym, klass, sector: sec,
    stage, conviction, trend, factors,
    avgVol:     Math.round(50 + r() * 9950) * 1000,
    spread:     +(0.5 + r() * 30).toFixed(1),
    listedAt:   Date.now() - Math.round(r() * 86400000 * 30),
    promotedAt: stage === 'promoted' || stage === 'active' ? Date.now() - Math.round(r() * 3600000 * 4) : null,
    why: stage === 'promoted' || stage === 'active' ? NEWS[Math.floor(r() * NEWS.length)] : null,
    spark,
    bookCorr:   +((r() - 0.5) * 1.2).toFixed(2),
    override:   override ?? null,  // {kind, by, at, reason, expiresAt}
    tierReason: stage === 'watching' ? (i < 50 ? 'core' : 'scan') : null,
  };
}

UNI.SYMBOLS = (() => {
  const out = [];
  // 7 active
  for (let i = 0; i < 7; i++) out.push(makeSymbol(i, 'active', CLASSES[i % CLASSES.length]));
  // 27 promoted (overlap with active is fine — list just shows current stage)
  for (let i = 7; i < 27; i++) out.push(makeSymbol(i, 'promoted', CLASSES[i % CLASSES.length]));
  // 50 core watching
  for (let i = 27; i < 77; i++) out.push(makeSymbol(i, 'watching', CLASSES[i % CLASSES.length]));
  // 100 scan (rest of watching, sample)
  for (let i = 77; i < 177; i++) out.push(makeSymbol(i, 'watching', CLASSES[i % CLASSES.length]));
  // 14 banned
  for (let i = 177; i < 191; i++) out.push(makeSymbol(i, 'banned', CLASSES[i % CLASSES.length], {
    kind: i % 3 === 0 ? 'manual-exclude' : 'auto-blacklist',
    by: i % 3 === 0 ? 'kvcom' : 'system',
    at: Date.now() - Math.round(r() * 86400000 * 12),
    reason: i % 3 === 0 ? 'Manual exclusion — fundamental mistrust' : 'Repeated stop-out (>3 in 7d)',
    expiresAt: i % 5 === 0 ? Date.now() + 86400000 * 30 : null,
  }));
  // 4 manually pinned (also in active/watching, so we tag the existing ones)
  ['NVDA','BTC','SPY','EURUSD'].forEach((sym, k) => {
    const e = out.find(s => s.sym === sym);
    if (e) e.override = { kind: 'pin-core', by:'kvcom', at: Date.now() - 86400000 * (k+1), reason: ['Long-term thesis','Core macro hedge','Index pillar','Reserve currency exposure'][k], expiresAt: null };
  });
  return out;
})();

// Promotion stream — most recent first
UNI.STREAM = (() => {
  const promoted = UNI.SYMBOLS.filter(s => s.stage === 'promoted' || s.stage === 'active');
  return promoted.slice().sort((a, b) => (b.promotedAt || 0) - (a.promotedAt || 0)).slice(0, 12).map(s => ({
    sym: s.sym, klass: s.klass, why: s.why, conviction: s.conviction, trend: s.trend,
    promotedAt: s.promotedAt, spark: s.spark, bookCorr: s.bookCorr,
    topFactors: Object.entries(s.factors).sort((a,b) => b[1] - a[1]).slice(0, 3),
    relatedNews: r() > 0.4 ? [
      { source:'Reuters', text: `${s.sym} — ${NEWS[Math.floor(r() * NEWS.length)]}`, at: Date.now() - Math.round(r() * 7200000) },
    ] : [],
  }));
})();

// Config (read-only mirror of backend)
UNI.CONFIG = {
  capacity: { source: null, watching: 300, core: 50, scan: 250, candidates: 400 },
  filters: {
    minLiquidityADV: 50000,
    maxSpreadBps: 20,
    maxDataAgeSec: 300,
    allowedClasses: ['equity','crypto','etf','fx','bond'],
    allowedRegions: ['US','EU','UK','JP'],
  },
  factorWeights: {
    momentum: 0.20, meanRev: 0.10, volRegime: 0.10, liquidity: 0.10,
    news: 0.15, correlation: 0.15, macro: 0.10, strategyFit: 0.10,
  },
  promotion: { convictionThreshold: 65, holdTimeMin: 15 },
  rebuild: { intervalSec: 120, lastDurationMs: 6420 },
};

// Audit trail for Overrides view
UNI.OVERRIDES_LOG = [
  { kind:'pin-core',       sym:'NVDA',   by:'kvcom', at: Date.now() - 86400000 * 1,  reason:'Long-term thesis',  expiresAt:null },
  { kind:'manual-exclude', sym:'GME',    by:'kvcom', at: Date.now() - 86400000 * 4,  reason:'Fundamental mistrust', expiresAt:null },
  { kind:'temp-promote',   sym:'COIN',   by:'kvcom', at: Date.now() - 3600000 * 3,   reason:'Earnings catalyst',  expiresAt: Date.now() + 86400000 * 1 },
  { kind:'force-scan',     sym:'TLT',    by:'kvcom', at: Date.now() - 3600000 * 9,   reason:'Macro pivot watch',  expiresAt:null },
  { kind:'manual-exclude', sym:'AMC',    by:'kvcom', at: Date.now() - 86400000 * 11, reason:'Volatility extreme', expiresAt: Date.now() + 86400000 * 30 },
  { kind:'pin-core',       sym:'BTC',    by:'kvcom', at: Date.now() - 86400000 * 2,  reason:'Core macro hedge',   expiresAt:null },
  { kind:'clear-override', sym:'NFLX',   by:'kvcom', at: Date.now() - 86400000 * 2.5, reason:'Thesis invalidated', expiresAt:null },
];

UNI.OVERRIDE_KINDS = {
  'pin-core':       { label: 'Pin to core',        tone: '#a5b4fc', desc:'Always include in core 50.' },
  'manual-exclude': { label: 'Manual exclude',     tone: '#f87171', desc:'Block from universe entirely.' },
  'force-scan':     { label: 'Force scan',         tone: '#93c5fd', desc:'Include in scan tier even if filters fail.' },
  'temp-promote':   { label: 'Temporary promote',  tone: '#fcd34d', desc:'Promote to candidate set with expiry.' },
  'clear-override': { label: 'Clear override',     tone: 'rgba(255,255,255,0.62)', desc:'Removed an existing override.' },
  'auto-blacklist': { label: 'Auto-blacklist',     tone: '#fb7185', desc:'System-rejected (e.g., repeated stop-out).' },
};

// Helpers
UNI.fmt = {
  num: (n) => n == null ? '—' : n.toLocaleString(),
  ago: (ts) => {
    if (!ts) return '—';
    const s = Math.round((Date.now() - ts) / 1000);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s/60)}m ago`;
    if (s < 86400) return `${Math.round(s/3600)}h ago`;
    return `${Math.round(s/86400)}d ago`;
  },
  in: (ts) => {
    if (!ts) return '—';
    const s = Math.round((ts - Date.now()) / 1000);
    if (s < 60) return `in ${s}s`;
    if (s < 3600) return `in ${Math.round(s/60)}m`;
    return `in ${Math.round(s/3600)}h`;
  },
};
