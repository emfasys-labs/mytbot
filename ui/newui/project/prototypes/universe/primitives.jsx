// ─── Universe primitives ─────────────────────────────────────────────
const { useState, useEffect, useRef, useMemo, useCallback } = React;

UNI.Card = function Card({ children, style, padding = 16, accent }) {
  return <div style={{
    borderRadius: 14,
    background: UNI.TOKENS.bg1,
    border: `1px solid ${UNI.TOKENS.line}`,
    padding, position: 'relative',
    ...(accent ? { boxShadow: `inset 0 1px 0 ${accent}10` } : {}),
    ...style,
  }}>{children}</div>;
};

UNI.Label = function Label({ children, accent, style }) {
  return <div style={{
    fontFamily: UNI.TOKENS.sans, fontSize: 10, fontWeight: 500,
    textTransform: 'uppercase', letterSpacing: '0.14em',
    color: accent || UNI.TOKENS.ink3,
    ...style,
  }}>{children}</div>;
};

UNI.Mono = function Mono({ children, size = 12, tone, bold, style }) {
  return <span style={{
    fontFamily: UNI.TOKENS.mono, fontSize: size,
    fontVariantNumeric: 'tabular-nums',
    color: tone ?? UNI.TOKENS.ink1,
    fontWeight: bold ? 500 : 400,
    letterSpacing: '-0.01em',
    ...style,
  }}>{children}</span>;
};

UNI.Pill = function Pill({ children, tone, dim, style }) {
  return <span style={{
    display: 'inline-flex', alignItems: 'center', gap: 5,
    padding: '3px 8px', borderRadius: 999,
    fontFamily: UNI.TOKENS.sans, fontSize: 10, fontWeight: 500,
    textTransform: 'uppercase', letterSpacing: '0.08em',
    background: dim ? `${tone}14` : 'transparent',
    border: `1px solid ${tone}55`,
    color: tone, ...style,
  }}>{children}</span>;
};

// Sparkline — used in promotion cards & instrument rows
UNI.Spark = function Spark({ data, w = 60, h = 18, tone, fill }) {
  if (!data || !data.length) return null;
  const lo = Math.min(...data), hi = Math.max(...data);
  const range = Math.max(1, hi - lo);
  const pts = data.map((v, i) => [
    (i / (data.length - 1)) * w,
    h - 1 - ((v - lo) / range) * (h - 2),
  ]);
  const path = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const area = `${path} L${w} ${h} L0 ${h} Z`;
  return <svg width={w} height={h} style={{ display: 'block' }}>
    {fill && <path d={area} fill={tone} opacity={0.16}/>}
    <path d={path} fill="none" stroke={tone} strokeWidth="1.2" strokeLinejoin="round" strokeLinecap="round"/>
  </svg>;
};

// Trend arrow
UNI.Trend = function Trend({ trend, size = 10 }) {
  const c = trend === 'rising' ? UNI.TOKENS.profit : trend === 'falling' ? UNI.TOKENS.loss : UNI.TOKENS.ink3;
  const ch = trend === 'rising' ? '↑' : trend === 'falling' ? '↓' : '→';
  return <span style={{ color: c, fontSize: size, fontFamily: UNI.TOKENS.mono, fontWeight: 500 }}>{ch}</span>;
};

// Honest data state — the universal "no fake numbers" component
UNI.DataState = function DataState({ kind = 'no-data', message, style }) {
  const map = {
    'no-data':       { color: UNI.TOKENS.ink3, label: 'No data yet',     desc: 'Backend hasn\'t produced this surface yet.' },
    'first-build':   { color: UNI.TOKENS.info, label: 'Waiting for first build', desc: 'Discovery loop hasn\'t completed a cycle.' },
    'stale':         { color: UNI.TOKENS.caution, label: 'Stale',         desc: 'Last build older than expected refresh window.' },
    'error':         { color: UNI.TOKENS.danger,  label: 'Backend unavailable', desc: 'Failing health check. Retrying…' },
    'pending':       { color: UNI.TOKENS.info,    label: 'Build in progress', desc: 'Discovery loop is running. Counts may shift.' },
  };
  const m = map[kind] || map['no-data'];
  return <div style={{
    display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center',
    padding:'24px 16px', borderRadius:10,
    border:`1px dashed ${m.color}55`, background:`${m.color}06`,
    fontFamily:UNI.TOKENS.sans, ...style,
  }}>
    <div style={{ fontSize:11, fontWeight:500, textTransform:'uppercase', letterSpacing:'0.14em', color:m.color }}>{m.label}</div>
    <div style={{ marginTop:6, fontSize:12, color:UNI.TOKENS.ink2, textAlign:'center', maxWidth: 280 }}>{message || m.desc}</div>
  </div>;
};

// Build status — the "discovery loop" indicator. Both global pill + local card.
UNI.BuildPill = function BuildPill({ build, compact }) {
  const s = build.state;
  const map = {
    fresh:    { color: UNI.TOKENS.profit,  glow: true,  label: 'Fresh' },
    building: { color: UNI.TOKENS.info,    glow: true,  label: 'Building' },
    stale:    { color: UNI.TOKENS.caution, glow: false, label: 'Stale' },
    error:    { color: UNI.TOKENS.danger,  glow: true,  label: 'Build error' },
    pending:  { color: UNI.TOKENS.ink3,    glow: false, label: 'Waiting for first build' },
  };
  const m = map[s] || map.pending;
  return <span style={{
    display:'inline-flex', alignItems:'center', gap:8,
    padding: compact ? '4px 10px' : '6px 12px',
    borderRadius: 999,
    background: `${m.color}10`,
    border: `1px solid ${m.color}44`,
    fontFamily: UNI.TOKENS.sans, fontSize: 11, fontWeight: 500,
    color: m.color, letterSpacing:'-0.01em',
  }}>
    <span style={{
      width: 6, height: 6, borderRadius: 999, background: m.color,
      boxShadow: m.glow ? `0 0 8px ${m.color}` : 'none',
      animation: s === 'building' ? 'uni-pulse 1.4s ease-in-out infinite' : 'none',
    }}/>
    {m.label}
    {!compact && build.lastBuildAt && s !== 'pending' && <span style={{ color: UNI.TOKENS.ink3, fontFamily: UNI.TOKENS.mono, fontSize: 10 }}>
      · {UNI.fmt.ago(build.lastBuildAt)}
    </span>}
    {!compact && build.loopId && <span style={{ color: UNI.TOKENS.ink3, fontFamily: UNI.TOKENS.mono, fontSize: 10 }}>
      · loop #{build.loopId.toLocaleString()}
    </span>}
  </span>;
};

// Stage chip — used in funnel and on instruments
UNI.StageChip = function StageChip({ stage, count, active, onClick, style }) {
  const c = UNI.STAGE_COLORS[stage];
  return <button
    onClick={onClick}
    style={{
      display:'inline-flex', alignItems:'center', gap:8,
      padding:'5px 10px', borderRadius: 8,
      background: active ? `${c}18` : 'transparent',
      border: `1px solid ${active ? c + '55' : UNI.TOKENS.line}`,
      color: active ? UNI.TOKENS.ink0 : UNI.TOKENS.ink2,
      fontFamily: UNI.TOKENS.sans, fontSize: 11, fontWeight: 500,
      cursor: onClick ? 'pointer' : 'default', textAlign:'left',
      transition: `all 200ms ${UNI.TOKENS.ease}`, ...style,
    }}>
    <span style={{ width:6, height:6, borderRadius: 999, background: c }}/>
    {UNI.STAGE_LABELS[stage]}
    {count != null && <UNI.Mono size={11} tone={active ? UNI.TOKENS.ink0 : UNI.TOKENS.ink2}>{UNI.fmt.num(count)}</UNI.Mono>}
  </button>;
};

// Asset class glyph — small, calm
UNI.ClassGlyph = function ClassGlyph({ klass, size = 12 }) {
  const map = {
    equity:'E', crypto:'₿', etf:'F', fx:'$', bond:'B',
  };
  const tone = {
    equity: UNI.TOKENS.ink2, crypto:'#fbbf24', etf:'#93c5fd', fx:'#5eead4', bond:'#c4b5fd',
  };
  return <span style={{
    display:'inline-flex', alignItems:'center', justifyContent:'center',
    width: size + 6, height: size + 6, borderRadius: 4,
    background: `${tone[klass]}14`, color: tone[klass],
    fontFamily: UNI.TOKENS.mono, fontSize: size - 1, fontWeight: 500,
  }}>{map[klass]}</span>;
};

// Override badge — small, tone-coded
UNI.OverrideBadge = function OverrideBadge({ kind, compact }) {
  const m = UNI.OVERRIDE_KINDS[kind];
  if (!m) return null;
  return <span title={m.desc} style={{
    display:'inline-flex', alignItems:'center', gap:4,
    padding: compact ? '2px 5px' : '3px 7px', borderRadius:4,
    background: `${m.tone}14`, color: m.tone,
    border:`1px solid ${m.tone}44`,
    fontFamily: UNI.TOKENS.sans, fontSize: 9, fontWeight:500,
    textTransform:'uppercase', letterSpacing:'0.08em',
  }}>
    <span style={{ width:4, height:4, borderRadius:999, background:m.tone }}/>
    {compact ? m.label.split(' ')[0] : m.label}
  </span>;
};
