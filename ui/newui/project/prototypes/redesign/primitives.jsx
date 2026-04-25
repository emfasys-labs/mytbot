// ─── Primitives ───────────────────────────────────────────────
// Wordmark, living glyph, NAV digits, cards, pills, etc.

const { useState, useEffect, useRef, useMemo, useLayoutEffect } = React;

// The living glyph — this is the brand identity.
// Off: empty ring · Running: filled dot with accent glow pulse · Paused: half · Error: danger pulse
function Glyph({ state = 'running', size = 14, accent = '#67e8f9' }) {
  const danger = TOKENS.danger;
  const off = TOKENS.ink3;
  const color = state === 'running' ? accent : state === 'paused' ? TOKENS.caution : state === 'error' ? danger : off;
  const glow = state === 'running' || state === 'error' ? color : 'transparent';
  const pulse = state === 'running' ? 'glyph-pulse 2.4s ease-in-out infinite' : state === 'error' ? 'glyph-err 0.9s ease-in-out infinite' : 'none';

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
          <circle cx="5" cy="5" r="4" fill="none" stroke={color} strokeWidth="1"/>
          <path d="M5 1 A4 4 0 0 1 5 9 Z" fill={color}/>
        </svg>
      ) : state === 'off' ? (
        <svg width={size} height={size} viewBox="0 0 10 10" style={{ position: 'relative' }}>
          <circle cx="5" cy="5" r="4" fill="none" stroke={color} strokeWidth="1"/>
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

// Wordmark — the identity is the type.
function Wordmark({ state = 'running', accent = '#67e8f9', size = 18, showGlyph = true }) {
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

// Big NAV number with digit-flip animation on change.
function NavNumber({ value, accent = '#67e8f9', size = 80, currency = '£' }) {
  const [display, setDisplay] = useState(value);
  const [flash, setFlash] = useState(null);

  useEffect(() => {
    if (value !== display) {
      setFlash(value > display ? 'up' : 'down');
      setDisplay(value);
      const t = setTimeout(() => setFlash(null), 600);
      return () => clearTimeout(t);
    }
  }, [value]);

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
      <span style={{ color: flash === 'up' ? TOKENS.profit : flash === 'down' ? TOKENS.loss : TOKENS.ink0, transition: `color 600ms ${TOKENS.ease}` }}>
        {formatted}
      </span>
      <span style={{ color: TOKENS.ink3, fontSize: size * 0.45, fontWeight: 300 }}>{decimals}</span>
    </div>
  );
}

// Card — base container.
function Card({ children, style, noPad, accent, glow, ...rest }) {
  return (
    <div style={{
      borderRadius: 14,
      background: TOKENS.bg1,
      border: `1px solid ${TOKENS.line}`,
      padding: noPad ? 0 : 16,
      position: 'relative',
      boxShadow: glow ? `0 0 0 1px ${accent}33, 0 0 24px ${accent}22` : 'none',
      transition: `box-shadow ${TOKENS.med}ms ${TOKENS.ease}, border-color ${TOKENS.med}ms ${TOKENS.ease}`,
      ...style,
    }} {...rest}>
      {children}
    </div>
  );
}

// Section label — uppercase micro.
function Label({ children, accent, style }) {
  return (
    <div style={{
      fontFamily: TOKENS.sans, fontSize: 10, fontWeight: 500,
      textTransform: 'uppercase', letterSpacing: '0.14em',
      color: accent || TOKENS.ink3,
      ...style,
    }}>{children}</div>
  );
}

// Pill — small state or data tag.
function Pill({ children, tone = 'neutral', size = 'md', style }) {
  const tones = {
    neutral: { bg: 'rgba(255,255,255,0.04)', fg: TOKENS.ink2 },
    accent:  { bg: 'rgba(255,255,255,0.06)', fg: TOKENS.ink0 },
    profit:  { bg: 'rgba(94,234,212,0.10)',  fg: TOKENS.profit },
    loss:    { bg: 'rgba(253,164,175,0.10)', fg: TOKENS.loss },
    caution: { bg: 'rgba(252,211,77,0.10)',  fg: TOKENS.caution },
    danger:  { bg: 'rgba(248,113,113,0.10)', fg: TOKENS.danger },
    info:    { bg: 'rgba(147,197,253,0.10)', fg: TOKENS.info },
  };
  const t = tones[tone];
  const sizes = { sm: { px: 6, py: 1, fs: 9 }, md: { px: 8, py: 2, fs: 10 }, lg: { px: 10, py: 3, fs: 11 } };
  const s = sizes[size];
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: `${s.py}px ${s.px}px`, borderRadius: 999,
      background: t.bg, color: t.fg,
      fontFamily: TOKENS.sans, fontSize: s.fs, fontWeight: 500,
      letterSpacing: '0.02em',
      ...style,
    }}>{children}</span>
  );
}

// Number with +/- sign coloring for P&L.
function Signed({ value, prefix = '£', size = 14, muted }) {
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

// Sparkline — tiny chart.
function Spark({ values, width = 120, height = 28, accent, area = true, accent2 }) {
  const min = Math.min(...values), max = Math.max(...values);
  const rng = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - ((v - min) / rng) * height;
    return [x, y];
  });
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0]},${p[1]}`).join(' ');
  const area_d = line + ` L${width},${height} L0,${height} Z`;
  const last = values[values.length - 1];
  const first = values[0];
  const color = last >= first ? (accent || TOKENS.profit) : (accent2 || TOKENS.loss);
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <defs>
        <linearGradient id={`spark-${color}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.25"/>
          <stop offset="100%" stopColor={color} stopOpacity="0"/>
        </linearGradient>
      </defs>
      {area && <path d={area_d} fill={`url(#spark-${color})`}/>}
      <path d={line} stroke={color} strokeWidth="1.3" fill="none" strokeLinejoin="round"/>
      <circle cx={pts[pts.length-1][0]} cy={pts[pts.length-1][1]} r="2" fill={color}/>
    </svg>
  );
}

// Lucide-style SVG icons (stroke-based).
const I = {
  dash:   (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>,
  signal: (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M2 20h.01"/><path d="M7 20v-4"/><path d="M12 20v-8"/><path d="M17 20V8"/><path d="M22 4v16"/></svg>,
  wallet: (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/></svg>,
  shield: (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M20 13c0 5-3.5 7.5-8 8.5-4.5-1-8-3.5-8-8.5V5l8-3 8 3Z"/></svg>,
  brain:  (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2a2.5 2.5 0 0 0-2.5 2.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>,
  log:    (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M8 13h8M8 17h5"/></svg>,
  settings:(p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>,
  cmd:    (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 0 0 3 3 3 3 0 0 0 3-3 3 3 0 0 0-3-3H6a3 3 0 0 0-3 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3 3 3 0 0 0 3 3h12a3 3 0 0 0 3-3 3 3 0 0 0-3-3z"/></svg>,
  power:  (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>,
  x:      (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M18 6 6 18M6 6l12 12"/></svg>,
  arrow:  (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M5 12h14M12 5l7 7-7 7"/></svg>,
  spark:  (p={}) => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="m12 3-1.5 6L4 11l6.5 2 1.5 6 1.5-6 6.5-2-6.5-2Z"/></svg>,
};

Object.assign(window, { Glyph, Wordmark, NavNumber, Card, Label, Pill, Signed, Spark, I });
