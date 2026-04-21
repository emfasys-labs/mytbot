/**
 * Primitives: Glyph, Wordmark, NavNumber, Card, Label, Pill, Signed, Spark, Icons.
 * Ported from mytbot-design-system/project/prototypes/redesign/primitives.jsx.
 */

import { CSSProperties, ReactElement, ReactNode, SVGProps, useEffect, useState } from 'react';
import { TOKENS, SystemState } from './tokens';

export type PillTone = 'neutral' | 'accent' | 'profit' | 'loss' | 'caution' | 'danger' | 'info';
export type PillSize = 'sm' | 'md' | 'lg';

// ─── Glyph — the living brand mark ─────────────────────────────
export function Glyph({
  state = 'running',
  size = 14,
  accent = '#67e8f9',
}: {
  state?: SystemState;
  size?: number;
  accent?: string;
}) {
  const danger = TOKENS.danger;
  const off = TOKENS.ink3;
  const color =
    state === 'running' ? accent :
    state === 'paused' ? TOKENS.caution :
    state === 'error' ? danger : off;
  const glow = state === 'running' || state === 'error' ? color : 'transparent';
  const pulse =
    state === 'running' ? 'ds-glyph-pulse 2.4s ease-in-out infinite' :
    state === 'error' ? 'ds-glyph-err 0.9s ease-in-out infinite' : 'none';

  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
      width: size + 6, height: size + 6, position: 'relative',
    }}>
      <span style={{
        position: 'absolute', width: size, height: size, borderRadius: '50%',
        background: glow, filter: `blur(${Math.round(size * 0.6)}px)`, opacity: 0.5,
        animation: pulse,
      }} />
      {state === 'paused' ? (
        <svg width={size} height={size} viewBox="0 0 10 10" style={{ position: 'relative' }}>
          <circle cx="5" cy="5" r="4" fill="none" stroke={color} strokeWidth="1" />
          <path d="M5 1 A4 4 0 0 1 5 9 Z" fill={color} />
        </svg>
      ) : state === 'off' ? (
        <svg width={size} height={size} viewBox="0 0 10 10" style={{ position: 'relative' }}>
          <circle cx="5" cy="5" r="4" fill="none" stroke={color} strokeWidth="1" />
        </svg>
      ) : (
        <span style={{
          position: 'relative', width: size * 0.7, height: size * 0.7, borderRadius: '50%',
          background: color,
        }} />
      )}
    </span>
  );
}

// ─── Wordmark — identity as typography ─────────────────────────
export function Wordmark({
  state = 'running', accent = '#67e8f9', size = 18, showGlyph = true,
}: { state?: SystemState; accent?: string; size?: number; showGlyph?: boolean }) {
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 8,
      fontFamily: TOKENS.sans, fontSize: size, fontWeight: 500,
      letterSpacing: '-0.02em', color: TOKENS.ink0,
    }}>
      {showGlyph && <Glyph state={state} accent={accent} size={size - 4} />}
      <span>mytbot</span>
    </div>
  );
}

// ─── NavNumber — hero NAV with digit color-flash on change ─────
export function NavNumber({
  value, size = 80, currency = '£',
}: { value: number; accent?: string; size?: number; currency?: string }) {
  const [display, setDisplay] = useState(value);
  const [flash, setFlash] = useState<'up' | 'down' | null>(null);

  useEffect(() => {
    if (value === display) return;
    setFlash(value > display ? 'up' : 'down');
    setDisplay(value);
    const t = setTimeout(() => setFlash(null), 600);
    return () => clearTimeout(t);
  }, [value, display]);

  const formatted = Math.round(display).toLocaleString();
  const decimals = ((display % 1).toFixed(2)).slice(1);

  return (
    <div style={{
      fontFamily: TOKENS.sans, color: TOKENS.ink0,
      fontVariantNumeric: 'tabular-nums', lineHeight: 1,
      fontWeight: 300, letterSpacing: '-0.04em',
      fontSize: size, position: 'relative',
      transition: `color ${TOKENS.med}ms ${TOKENS.ease}`,
    }}>
      <span style={{ color: TOKENS.ink3, fontWeight: 200, fontSize: size * 0.55, marginRight: 2, verticalAlign: 'top' }}>{currency}</span>
      <span style={{
        color: flash === 'up' ? TOKENS.profit : flash === 'down' ? TOKENS.loss : TOKENS.ink0,
        transition: `color 600ms ${TOKENS.ease}`,
      }}>
        {formatted}
      </span>
      <span style={{ color: TOKENS.ink3, fontSize: size * 0.45, fontWeight: 300 }}>{decimals}</span>
    </div>
  );
}

// ─── Card — base container ─────────────────────────────────────
export function Card({
  children, style, noPad, accent, glow,
}: {
  children?: ReactNode;
  style?: CSSProperties;
  noPad?: boolean;
  accent?: string;
  glow?: boolean;
}) {
  return (
    <div style={{
      borderRadius: 14,
      background: TOKENS.bg1,
      border: `1px solid ${TOKENS.line}`,
      padding: noPad ? 0 : 16,
      position: 'relative',
      boxShadow: glow && accent ? `0 0 0 1px ${accent}33, 0 0 24px ${accent}22` : 'none',
      transition: `box-shadow ${TOKENS.med}ms ${TOKENS.ease}, border-color ${TOKENS.med}ms ${TOKENS.ease}`,
      ...style,
    }}>
      {children}
    </div>
  );
}

// ─── Label — UPPERCASE micro label ─────────────────────────────
export function Label({
  children, accent, style,
}: { children?: ReactNode; accent?: string; style?: CSSProperties }) {
  return (
    <div style={{
      fontFamily: TOKENS.sans, fontSize: 10, fontWeight: 500,
      textTransform: 'uppercase', letterSpacing: '0.14em',
      color: accent || TOKENS.ink3,
      ...style,
    }}>
      {children}
    </div>
  );
}

// ─── Pill — small state/data tag ───────────────────────────────
const PILL_TONES: Record<PillTone, { bg: string; fg: string }> = {
  neutral: { bg: 'rgba(255,255,255,0.04)', fg: TOKENS.ink2 },
  accent:  { bg: 'rgba(255,255,255,0.06)', fg: TOKENS.ink0 },
  profit:  { bg: 'rgba(94,234,212,0.10)',  fg: TOKENS.profit },
  loss:    { bg: 'rgba(253,164,175,0.10)', fg: TOKENS.loss },
  caution: { bg: 'rgba(252,211,77,0.10)',  fg: TOKENS.caution },
  danger:  { bg: 'rgba(248,113,113,0.10)', fg: TOKENS.danger },
  info:    { bg: 'rgba(147,197,253,0.10)', fg: TOKENS.info },
};
const PILL_SIZES: Record<PillSize, { px: number; py: number; fs: number }> = {
  sm: { px: 6,  py: 1, fs: 9  },
  md: { px: 8,  py: 2, fs: 10 },
  lg: { px: 10, py: 3, fs: 11 },
};

export function Pill({
  children, tone = 'neutral', size = 'md', style,
}: { children?: ReactNode; tone?: PillTone; size?: PillSize; style?: CSSProperties }) {
  const t = PILL_TONES[tone];
  const s = PILL_SIZES[size];
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: `${s.py}px ${s.px}px`, borderRadius: 999,
      background: t.bg, color: t.fg,
      fontFamily: TOKENS.sans, fontSize: s.fs, fontWeight: 500,
      letterSpacing: '0.02em',
      ...style,
    }}>
      {children}
    </span>
  );
}

// ─── Signed — +/- coloured number ──────────────────────────────
export function Signed({
  value, prefix = '£', size = 14, muted,
}: { value: number; prefix?: string; size?: number; muted?: boolean }) {
  const pos = value >= 0;
  return (
    <span style={{
      fontFamily: TOKENS.mono, fontSize: size,
      fontVariantNumeric: 'tabular-nums',
      color: muted ? TOKENS.ink2 : pos ? TOKENS.profit : TOKENS.loss,
      letterSpacing: '-0.01em',
    }}>
      {pos ? '+' : '−'}{prefix}{Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}
    </span>
  );
}

// ─── Spark — tiny sparkline ────────────────────────────────────
export function Spark({
  values, width = 120, height = 28, accent, area = true, accent2,
}: { values: number[]; width?: number; height?: number; accent?: string; area?: boolean; accent2?: string }) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const rng = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - ((v - min) / rng) * height;
    return [x, y] as const;
  });
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0]},${p[1]}`).join(' ');
  const area_d = `${line} L${width},${height} L0,${height} Z`;
  const last = values[values.length - 1];
  const first = values[0];
  const color = last >= first ? accent || TOKENS.profit : accent2 || TOKENS.loss;
  const gradId = `ds-spark-${color.replace(/[^a-z0-9]/gi, '')}`;
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.25" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {area && <path d={area_d} fill={`url(#${gradId})`} />}
      <path d={line} stroke={color} strokeWidth="1.3" fill="none" strokeLinejoin="round" />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="2" fill={color} />
    </svg>
  );
}

// ─── Icons — lightweight Lucide-style stroke SVGs ──────────────
type IconProps = SVGProps<SVGSVGElement>;

const iconBase = (children: ReactNode): ((p?: IconProps) => ReactElement) =>
  (p = {}) => (
    <svg
      width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
      {...p}
    >
      {children}
    </svg>
  );

export const I = {
  dash:   iconBase(<><rect x="3" y="3" width="7" height="9" /><rect x="14" y="3" width="7" height="5" /><rect x="14" y="12" width="7" height="9" /><rect x="3" y="16" width="7" height="5" /></>),
  signal: iconBase(<><path d="M2 20h.01" /><path d="M7 20v-4" /><path d="M12 20v-8" /><path d="M17 20V8" /><path d="M22 4v16" /></>),
  wallet: iconBase(<><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4" /><path d="M3 5v14a2 2 0 0 0 2 2h16v-5" /><path d="M18 12a2 2 0 0 0 0 4h4v-4Z" /></>),
  shield: iconBase(<><path d="M20 13c0 5-3.5 7.5-8 8.5-4.5-1-8-3.5-8-8.5V5l8-3 8 3Z" /></>),
  brain:  iconBase(<><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z" /><path d="M14.5 2a2.5 2.5 0 0 0-2.5 2.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z" /></>),
  log:    iconBase(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><path d="M8 13h8M8 17h5" /></>),
  cmd:    iconBase(<><path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3H6a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3h12a3 3 0 0 0 3-3 3 3 0 0 0-3-3z" /></>),
  power:  iconBase(<><path d="M18.36 6.64a9 9 0 1 1-12.73 0" strokeWidth="1.8" /><line x1="12" y1="2" x2="12" y2="12" strokeWidth="1.8" /></>),
  x:      iconBase(<><path d="M18 6 6 18M6 6l12 12" /></>),
  arrow:  iconBase(<><path d="M5 12h14M12 5l7 7-7 7" /></>),
} as const;
