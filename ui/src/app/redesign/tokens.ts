/**
 * Design tokens + accent palette for the "living instrument" redesign.
 * Ported from mytbot-design-system/project/prototypes/redesign/tokens_data.jsx.
 */

export const TOKENS = {
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

  profit: '#5eead4',
  loss: '#fda4af',
  caution: '#fcd34d',
  danger: '#f87171',
  info: '#93c5fd',

  sans: "'Geist', -apple-system, system-ui, sans-serif",
  mono: "'Geist Mono', ui-monospace, monospace",

  ease: 'cubic-bezier(0.22, 1, 0.36, 1)',
  easeSoft: 'cubic-bezier(0.4, 0, 0.2, 1)',
  fast: 160,
  med: 300,
  slow: 600,
} as const;

/**
 * Display currency symbol. Brokers in this build (Alpaca / IBKR / Binance /
 * Bybit / Kraken) settle predominantly in USD and the backend has no FX
 * conversion layer, so labelling the dashboard in £ was actively
 * misleading — every figure was the raw USD-or-other-currency total with a
 * GBP glyph stuck in front.
 *
 * Until a real FX layer lands, the dashboard uses ``$`` as a single source
 * of truth so swapping it later (or making it broker-aware) is a one-line
 * change.
 */
export const CURRENCY_SYMBOL = '$';

export type AccentName = 'cyan' | 'amber' | 'emerald' | 'violet' | 'coral' | 'white';

export interface Accent {
  main: string;
  dim: string;
  glow: string;
}

export const ACCENTS: Record<AccentName, Accent> = {
  cyan:    { main: '#67e8f9', dim: 'rgba(103,232,249,0.18)', glow: 'rgba(103,232,249,0.35)' },
  amber:   { main: '#fbbf24', dim: 'rgba(251,191,36,0.18)',  glow: 'rgba(251,191,36,0.35)' },
  emerald: { main: '#34d399', dim: 'rgba(52,211,153,0.18)',  glow: 'rgba(52,211,153,0.35)' },
  violet:  { main: '#c4b5fd', dim: 'rgba(196,181,253,0.18)', glow: 'rgba(196,181,253,0.35)' },
  coral:   { main: '#fb7185', dim: 'rgba(251,113,133,0.18)', glow: 'rgba(251,113,133,0.35)' },
  white:   { main: '#ffffff', dim: 'rgba(255,255,255,0.12)', glow: 'rgba(255,255,255,0.22)' },
};

export type SystemState = 'running' | 'starting' | 'stopping' | 'paused' | 'off' | 'error';
export type Density = 'comfort' | 'compact';
export type Theme = 'dark' | 'light';
export type Viewport = 'desktop' | 'tablet' | 'mobile';
export type Route = 'dash' | 'signals' | 'book' | 'risk' | 'strat' | 'universe' | 'log';

export interface Tweaks {
  accent: AccentName;
  density: Density;
  state: SystemState;
  theme: Theme;
  viewport: Viewport;
}

export const DEFAULT_TWEAKS: Tweaks = {
  accent: 'cyan',
  density: 'comfort',
  state: 'running',
  theme: 'dark',
  viewport: 'desktop',
};
