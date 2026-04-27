// ─── Instrument inspector — slide-in drawer ──────────────────────────
// Factor radar, why-listed, why-promoted, recent signals, override info.

UNI.Inspector = function Inspector({ sym, onClose }) {
  const T = UNI.TOKENS;
  const s = useMemo(() => UNI.SYMBOLS.find(x => x.sym === sym), [sym]);
  if (!s) return null;
  const c = UNI.STAGE_COLORS[s.stage];

  return <div style={{
    position:'fixed', inset:0, zIndex:50, display:'flex', justifyContent:'flex-end',
    background:'rgba(0,0,0,0.55)', backdropFilter:'blur(4px)',
    animation:`uni-fade 200ms ${T.ease}`,
  }} onClick={onClose}>
    <div onClick={e => e.stopPropagation()} style={{
      width: 540, maxWidth:'94vw', height:'100vh', background:T.bg1,
      borderLeft:`1px solid ${T.line}`, overflowY:'auto',
      animation:`uni-rise 320ms ${T.ease}`,
      display:'flex', flexDirection:'column',
    }}>
      {/* Header */}
      <div style={{ padding:'20px 24px', borderBottom:`1px solid ${T.line}`,
        position:'sticky', top:0, background:`${T.bg1}dd`, backdropFilter:'blur(8px)', zIndex:1 }}>
        <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between' }}>
          <div>
            <div style={{ display:'flex', alignItems:'center', gap:10 }}>
              <UNI.ClassGlyph klass={s.klass} size={14}/>
              <h2 style={{ margin:0, fontFamily:T.sans, fontSize:28, fontWeight:300, letterSpacing:'-0.02em', color:T.ink0 }}>{s.sym}</h2>
              <UNI.StageChip stage={s.stage} active/>
              {s.override && <UNI.OverrideBadge kind={s.override.kind}/>}
            </div>
            <div style={{ marginTop:6, fontFamily:T.sans, fontSize:12, color:T.ink3, textTransform:'capitalize' }}>
              {s.klass} · {s.sector}
            </div>
          </div>
          <button onClick={onClose} style={{
            background:'transparent', border:`1px solid ${T.line}`,
            color:T.ink2, borderRadius:6, padding:'4px 10px',
            fontFamily:T.sans, fontSize:11, cursor:'pointer',
          }}>esc</button>
        </div>
      </div>

      <div style={{ padding:'20px 24px', display:'flex', flexDirection:'column', gap:18 }}>
        {/* Hero — conviction + spark */}
        <div style={{ display:'flex', alignItems:'flex-end', gap:18 }}>
          <div>
            <UNI.Label>conviction</UNI.Label>
            <div style={{ display:'flex', alignItems:'baseline', gap:8, marginTop:6 }}>
              <span style={{ fontFamily:T.sans, fontSize:48, fontWeight:200, color:T.ink0, letterSpacing:'-0.04em', lineHeight:1 }}>{s.conviction}</span>
              <UNI.Trend trend={s.trend} size={14}/>
            </div>
          </div>
          <div style={{ flex:1 }}>
            <UNI.Spark data={s.spark} w={260} h={48} tone={c} fill/>
            <div style={{ marginTop:4, fontFamily:T.mono, fontSize:9, color:T.ink3, textAlign:'right' }}>last 12 hourly samples</div>
          </div>
        </div>

        {/* Factor radar */}
        <UNI.Card padding={16}>
          <UNI.Label>factor radar</UNI.Label>
          <div style={{ marginTop:10, display:'flex', alignItems:'center', gap:18 }}>
            <UNI.FactorRadar factors={s.factors} accent={c}/>
            <div style={{ flex:1, display:'flex', flexDirection:'column', gap:6 }}>
              {Object.entries(s.factors).map(([k, v]) => (
                <div key={k} style={{ display:'flex', alignItems:'center', gap:8 }}>
                  <span style={{ flex:1, fontFamily:T.sans, fontSize:11, color:T.ink2, textTransform:'capitalize' }}>
                    {k.replace(/([A-Z])/g, ' $1')}
                  </span>
                  <div style={{ width:80, height:3, borderRadius:2, background:T.bg3 }}>
                    <div style={{ height:'100%', borderRadius:2, width:`${v}%`, background: c, opacity: 0.7 }}/>
                  </div>
                  <UNI.Mono size={10} tone={T.ink2} style={{ width:24, textAlign:'right' }}>{v}</UNI.Mono>
                </div>
              ))}
            </div>
          </div>
        </UNI.Card>

        {/* Why-listed reasoning */}
        <UNI.Card padding={16}>
          <UNI.Label>why listed</UNI.Label>
          <div style={{ marginTop:10, display:'flex', flexDirection:'column', gap:8 }}>
            <Reason ok>Liquidity {s.factors.liquidity}/100 · ADV £{s.avgVol.toLocaleString()}</Reason>
            <Reason ok>Spread {s.spread}bp · within max 20bp</Reason>
            <Reason ok>Data freshness · within 5min window</Reason>
            <Reason ok>Asset class allowed · {s.klass}</Reason>
            {s.tierReason && <Reason ok>Tier · <span style={{ color: s.tierReason === 'core' ? UNI.TOKENS.accent : T.ink2 }}>{s.tierReason}</span></Reason>}
            {s.override && <Reason warn>Override · <UNI.OverrideBadge kind={s.override.kind} compact/></Reason>}
          </div>
        </UNI.Card>

        {/* Why-promoted (only for promoted/active) */}
        {(s.stage === 'promoted' || s.stage === 'active') && <UNI.Card padding={16}>
          <UNI.Label accent={UNI.TOKENS.caution}>why promoted</UNI.Label>
          <div style={{ marginTop:10, fontFamily:T.sans, fontSize:13, color:T.ink1, fontStyle:'italic' }}>
            "{s.why}"
          </div>
          <div style={{ marginTop:10, paddingTop:10, borderTop:`1px solid ${T.line}`, display:'flex', flexDirection:'column', gap:6 }}>
            {Object.entries(s.factors).sort((a,b) => b[1]-a[1]).slice(0,3).map(([k, v]) => (
              <div key={k} style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
                <span style={{ fontFamily:T.sans, fontSize:11, color:T.ink2, textTransform:'capitalize' }}>
                  {k.replace(/([A-Z])/g, ' $1')}
                </span>
                <UNI.Mono size={11} tone={UNI.TOKENS.caution} bold>{v}</UNI.Mono>
              </div>
            ))}
          </div>
          <div style={{ marginTop:10, paddingTop:10, borderTop:`1px solid ${T.line}`, fontFamily:T.mono, fontSize:11, color:T.ink3 }}>
            promoted {UNI.fmt.ago(s.promotedAt)} · ρ to book {s.bookCorr >= 0 ? '+' : ''}{s.bookCorr.toFixed(2)}
          </div>
        </UNI.Card>}

        {/* Override info */}
        {s.override && <UNI.Card padding={16} style={{ borderColor: `${UNI.OVERRIDE_KINDS[s.override.kind].tone}55` }}>
          <UNI.Label accent={UNI.OVERRIDE_KINDS[s.override.kind].tone}>active override</UNI.Label>
          <div style={{ marginTop:10, fontFamily:T.sans, fontSize:13, color:T.ink1 }}>{s.override.reason}</div>
          <div style={{ marginTop:8, fontFamily:T.mono, fontSize:11, color:T.ink3 }}>
            by {s.override.by} · {UNI.fmt.ago(s.override.at)}
            {s.override.expiresAt && <> · expires {UNI.fmt.in(s.override.expiresAt)}</>}
          </div>
        </UNI.Card>}

        {/* Read-only footer */}
        <div style={{ padding:'12px 14px', borderRadius:8,
          background:`${UNI.TOKENS.accent}06`, border:`1px solid ${UNI.TOKENS.accent}33`,
          fontFamily:T.sans, fontSize:11, color:T.ink2, lineHeight:1.5 }}>
          <span style={{ color:UNI.TOKENS.accent, fontWeight:500 }}>Read-only.</span> Universe is
          observational. To pin, exclude, force-scan or temp-promote, use the Overrides tab —
          every action requires a reason and is logged.
        </div>
      </div>
    </div>
  </div>;
};

// Hexagonal radar for 8 factors
UNI.FactorRadar = function FactorRadar({ factors, accent }) {
  const T = UNI.TOKENS;
  const keys = ['momentum','meanRev','volRegime','liquidity','news','correlation','macro','strategyFit'];
  const labels = { momentum:'mom', meanRev:'rev', volRegime:'vol', liquidity:'liq', news:'news', correlation:'corr', macro:'macro', strategyFit:'fit' };
  const cx = 70, cy = 70, R = 56;
  const angle = (i) => (Math.PI * 2 * i) / keys.length - Math.PI / 2;

  const ringPath = (frac) => keys.map((_, i) => {
    const x = cx + Math.cos(angle(i)) * R * frac;
    const y = cy + Math.sin(angle(i)) * R * frac;
    return (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
  }).join(' ') + ' Z';

  const dataPath = keys.map((k, i) => {
    const v = (factors[k] ?? 0) / 100;
    const x = cx + Math.cos(angle(i)) * R * v;
    const y = cy + Math.sin(angle(i)) * R * v;
    return (i ? 'L' : 'M') + x.toFixed(1) + ' ' + y.toFixed(1);
  }).join(' ') + ' Z';

  return <svg width="160" height="160" viewBox="0 0 140 140">
    {[0.25, 0.5, 0.75, 1].map(f => (
      <path key={f} d={ringPath(f)} fill="none" stroke={T.line} strokeWidth="0.6"/>
    ))}
    {keys.map((k, i) => {
      const x = cx + Math.cos(angle(i)) * R;
      const y = cy + Math.sin(angle(i)) * R;
      return <line key={k} x1={cx} y1={cy} x2={x} y2={y} stroke={T.line} strokeWidth="0.6"/>;
    })}
    <path d={dataPath} fill={accent} fillOpacity="0.18" stroke={accent} strokeWidth="1.2"/>
    {keys.map((k, i) => {
      const x = cx + Math.cos(angle(i)) * (R + 10);
      const y = cy + Math.sin(angle(i)) * (R + 10);
      return <text key={k} x={x} y={y + 3} fill={T.ink3} fontSize="8"
        fontFamily={T.mono} textAnchor="middle">{labels[k]}</text>;
    })}
  </svg>;
};

function Reason({ ok, warn, children }) {
  const T = UNI.TOKENS;
  const c = ok ? UNI.TOKENS.profit : warn ? UNI.TOKENS.caution : UNI.TOKENS.danger;
  return <div style={{ display:'flex', alignItems:'center', gap:10 }}>
    <span style={{ width:5, height:5, borderRadius:999, background:c }}/>
    <span style={{ fontFamily:UNI.TOKENS.sans, fontSize:12, color:T.ink1 }}>{children}</span>
  </div>;
}
