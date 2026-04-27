// ─── Overview ────────────────────────────────────────────────────────
// The "river at a glance" — funnel hero + status + recent promotions.

UNI.Overview = function Overview({ onSelect, onJumpTo }) {
  const T = UNI.TOKENS;
  return <div style={{ display:'flex', flexDirection:'column', gap:18 }}>
    {/* Hero: the funnel river */}
    <UNI.Card padding={24}>
      <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', marginBottom:18 }}>
        <div>
          <UNI.Label>discovery funnel · live</UNI.Label>
          <h2 style={{ margin:'6px 0 0', fontFamily:T.sans, fontSize:28, fontWeight:300, letterSpacing:'-0.03em', color:T.ink0 }}>
            8,742 sourced · 300 watched · 7 active
          </h2>
          <div style={{ marginTop:6, fontFamily:T.sans, fontSize:13, color:T.ink2, maxWidth: 540, lineHeight:1.5 }}>
            The bot watches the river autonomously. Each stage drops symbols for
            transparent reasons. Manual overrides are exceptional — see Overrides tab.
          </div>
        </div>
        <UNI.BuildPill build={UNI.BUILD}/>
      </div>

      <UNI.FunnelRiver onJumpTo={onJumpTo}/>
    </UNI.Card>

    <div style={{ display:'grid', gridTemplateColumns: '2fr 1.1fr', gap: 18 }}>
      {/* Promotion stream */}
      <UNI.Card>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12 }}>
          <UNI.Label>recent promotions · why now</UNI.Label>
          <span style={{ fontFamily:T.mono, fontSize:10, color:T.ink3 }}>{UNI.STREAM.length} in last 4h</span>
        </div>
        <div style={{ display:'flex', flexDirection:'column', gap:8, maxHeight: 540, overflowY:'auto', paddingRight:4 }}>
          {UNI.STREAM.map((p, i) => <UNI.PromotionCard key={p.sym + i} p={p} onSelect={onSelect}/>)}
        </div>
      </UNI.Card>

      {/* Right rail */}
      <div style={{ display:'flex', flexDirection:'column', gap:18 }}>
        <UNI.BuildDetail/>
        <UNI.OverrideSummary onJumpTo={onJumpTo}/>
        <UNI.UniverseSnapshot onJumpTo={onJumpTo}/>
      </div>
    </div>
  </div>;
};

// ─── The Funnel "River" — stage cards connected by flowing dashed lines
UNI.FunnelRiver = function FunnelRiver({ onJumpTo }) {
  const T = UNI.TOKENS;
  const total = UNI.FUNNEL[0].count;
  const widthFor = (count, stage) => {
    if (stage === 'banned') return 80;  // banned is a side-channel, not a stage
    const min = 80, max = 100;
    const pct = Math.max(0.04, count / total);
    return Math.max(min, max * Math.sqrt(pct));
  };
  return <div>
    <div style={{ display:'flex', alignItems:'stretch', gap:0, position:'relative', overflowX:'auto', paddingBottom:8 }}>
      {UNI.FUNNEL.filter(f => f.stage !== 'banned').map((f, i, arr) => {
        const c = UNI.STAGE_COLORS[f.stage];
        const w = widthFor(f.count, f.stage);
        const next = arr[i + 1];
        const dropCount = next ? f.count - next.count : null;
        return <React.Fragment key={f.stage}>
          <button onClick={() => onJumpTo({ tab:'instruments', stage:f.stage })} style={{
            flex: `1 1 ${w}%`, minWidth: 140,
            position:'relative', padding:'18px 16px',
            background: `linear-gradient(180deg, ${c}10, transparent 80%)`,
            border:`1px solid ${T.line}`, borderTop: `1px solid ${c}66`,
            borderRadius: 10,
            display:'flex', flexDirection:'column', alignItems:'flex-start', gap:6,
            cursor:'pointer', transition:`all 200ms ${T.ease}`,
            textAlign:'left',
          }}
          onMouseEnter={e => { e.currentTarget.style.background = `linear-gradient(180deg, ${c}1c, transparent 80%)`; }}
          onMouseLeave={e => { e.currentTarget.style.background = `linear-gradient(180deg, ${c}10, transparent 80%)`; }}>
            <span style={{ display:'flex', alignItems:'center', gap:8 }}>
              <span style={{ width:8, height:8, borderRadius:999, background:c, boxShadow:`0 0 8px ${c}66` }}/>
              <UNI.Label accent={c}>{UNI.STAGE_LABELS[f.stage]}</UNI.Label>
            </span>
            <div style={{ fontFamily:T.sans, fontSize:30, fontWeight:300, color:T.ink0, letterSpacing:'-0.03em', lineHeight:1 }}>
              {UNI.fmt.num(f.count)}
            </div>
            <div style={{ fontFamily:T.sans, fontSize:11, color:T.ink3, lineHeight:1.4, minHeight: 30 }}>
              {UNI.STAGE_DESC[f.stage].split('.')[0]}
            </div>
          </button>
          {next && <div style={{
            flex:'0 0 36px', position:'relative', display:'flex', flexDirection:'column',
            alignItems:'center', justifyContent:'center',
          }}>
            <svg width="36" height="60" style={{ overflow:'visible' }}>
              <line x1="0" y1="30" x2="36" y2="30"
                stroke={T.lineStrong} strokeWidth="1" strokeDasharray="2 4"
                style={{ animation:'uni-flow 2s linear infinite' }}/>
              <polygon points="36,30 30,26 30,34" fill={T.lineStrong}/>
            </svg>
            {dropCount != null && dropCount > 0 && <div style={{
              position:'absolute', top: -2,
              fontFamily:T.mono, fontSize:9, color:T.ink3, whiteSpace:'nowrap',
            }}>−{UNI.fmt.num(dropCount)}</div>}
          </div>}
        </React.Fragment>;
      })}
    </div>

    {/* Banned side-rail */}
    <div style={{ marginTop:14, paddingTop:14, borderTop:`1px dashed ${T.line}`, display:'flex', alignItems:'center', justifyContent:'space-between' }}>
      <div style={{ display:'flex', alignItems:'center', gap:12 }}>
        <span style={{ fontFamily:T.mono, fontSize:10, color:T.ink3, textTransform:'uppercase', letterSpacing:'0.1em' }}>side channel</span>
        <button onClick={() => onJumpTo({ tab:'instruments', stage:'banned' })} style={{ background:'none', border:'none', padding:0, cursor:'pointer', display:'flex', alignItems:'center', gap:8 }}>
          <span style={{ width:6, height:6, borderRadius:999, background:UNI.STAGE_COLORS.banned }}/>
          <span style={{ fontFamily:T.sans, fontSize:12, color:T.ink1 }}>{UNI.fmt.num(UNI.FUNNEL[5].count)} banned / blocked</span>
        </button>
      </div>
      <button onClick={() => onJumpTo({ tab:'funnel' })} style={{
        background:'transparent', border:`1px solid ${T.line}`, borderRadius:6,
        padding:'5px 10px', color:T.ink2, fontFamily:T.sans, fontSize:11, cursor:'pointer',
      }}>open funnel deep dive →</button>
    </div>
  </div>;
};

// ─── Promotion card — the causal "why now" for a recent promotion
UNI.PromotionCard = function PromotionCard({ p, onSelect }) {
  const T = UNI.TOKENS;
  return <button onClick={() => onSelect(p.sym)} style={{
    display:'block', textAlign:'left', width:'100%',
    padding:12, borderRadius: 10,
    background: T.bg2, border: `1px solid ${T.line}`,
    cursor:'pointer', animation:`uni-rise 300ms ${T.ease}`,
    transition: `all 180ms ${T.ease}`,
  }}
  onMouseEnter={e => { e.currentTarget.style.borderColor = T.lineStrong; }}
  onMouseLeave={e => { e.currentTarget.style.borderColor = T.line; }}>
    <div style={{ display:'flex', alignItems:'flex-start', gap:12 }}>
      <UNI.ClassGlyph klass={p.klass} size={14}/>
      <div style={{ flex:1 }}>
        <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between' }}>
          <div style={{ display:'flex', alignItems:'baseline', gap:10 }}>
            <span style={{ fontFamily:T.sans, fontSize:15, fontWeight:500, color:T.ink0 }}>{p.sym}</span>
            <span style={{ fontFamily:T.sans, fontSize:11, color:T.ink3 }}>{p.why}</span>
          </div>
          <span style={{ fontFamily:T.mono, fontSize:10, color:T.ink3 }}>{UNI.fmt.ago(p.promotedAt)}</span>
        </div>

        <div style={{ marginTop:8, display:'flex', alignItems:'center', gap:14 }}>
          {/* conviction + trend + spark */}
          <div style={{ display:'flex', alignItems:'center', gap:8 }}>
            <span style={{ fontFamily:T.sans, fontSize:18, fontWeight:300, color:T.ink0, letterSpacing:'-0.02em' }}>
              {p.conviction}
              <span style={{ fontSize:10, color:T.ink3, marginLeft:1 }}>conv</span>
            </span>
            <UNI.Trend trend={p.trend}/>
            <UNI.Spark data={p.spark} w={56} h={16} tone={UNI.TOKENS.accent} fill/>
          </div>

          {/* top 3 factors */}
          <div style={{ display:'flex', gap:5, flex:1 }}>
            {p.topFactors.map(([name, val]) => <div key={name} style={{
              padding:'2px 6px', borderRadius:4,
              background:`${UNI.TOKENS.accent}10`, border:`1px solid ${UNI.TOKENS.accent}33`,
              fontFamily:T.mono, fontSize:9, color:T.ink2,
              display:'flex', alignItems:'center', gap:4,
            }}>
              <span>{name}</span>
              <span style={{ color:UNI.TOKENS.accent, fontWeight:500 }}>{val}</span>
            </div>)}
          </div>

          {/* book correlation */}
          <span title="Correlation to current book" style={{ fontFamily:T.mono, fontSize:10, color: Math.abs(p.bookCorr) < 0.3 ? T.ink2 : T.caution }}>
            ρ{p.bookCorr >= 0 ? '+' : ''}{p.bookCorr.toFixed(2)}
          </span>
        </div>

        {p.relatedNews && p.relatedNews.length > 0 && <div style={{
          marginTop:8, paddingTop:8, borderTop:`1px solid ${T.line}`,
          fontFamily:T.sans, fontSize:11, color:T.ink2, display:'flex', alignItems:'center', gap:6,
        }}>
          <span style={{ width:3, height:3, borderRadius:999, background:T.info }}/>
          <span style={{ color:T.ink3, textTransform:'uppercase', fontSize:9, letterSpacing:'0.08em' }}>{p.relatedNews[0].source}</span>
          <span>{p.relatedNews[0].text}</span>
        </div>}
      </div>
    </div>
  </button>;
};

UNI.BuildDetail = function BuildDetail() {
  const T = UNI.TOKENS;
  const b = UNI.BUILD;
  return <UNI.Card>
    <UNI.Label>discovery loop</UNI.Label>
    <div style={{ marginTop:10, display:'grid', gridTemplateColumns:'1fr 1fr', gap:8, fontFamily:T.mono, fontSize:11 }}>
      <Row k="State" v={<UNI.BuildPill build={b} compact/>}/>
      <Row k="Loop" v={<UNI.Mono size={11}>#{b.loopId.toLocaleString()}</UNI.Mono>}/>
      <Row k="Last build" v={<UNI.Mono size={11} tone={T.ink2}>{UNI.fmt.ago(b.lastBuildAt)}</UNI.Mono>}/>
      <Row k="Next" v={<UNI.Mono size={11} tone={T.ink2}>{UNI.fmt.in(b.nextBuildAt)}</UNI.Mono>}/>
      <Row k="Duration" v={<UNI.Mono size={11} tone={T.ink2}>{(b.durationMs/1000).toFixed(1)}s</UNI.Mono>}/>
      <Row k="Interval" v={<UNI.Mono size={11} tone={T.ink2}>120s</UNI.Mono>}/>
    </div>
  </UNI.Card>;
};

UNI.OverrideSummary = function OverrideSummary({ onJumpTo }) {
  const T = UNI.TOKENS;
  const counts = {};
  UNI.SYMBOLS.forEach(s => { if (s.override) counts[s.override.kind] = (counts[s.override.kind] || 0) + 1; });
  const total = Object.values(counts).reduce((a,b) => a+b, 0);
  return <UNI.Card>
    <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
      <UNI.Label>overrides</UNI.Label>
      <UNI.Mono size={10} tone={T.ink3}>{total} active</UNI.Mono>
    </div>
    <div style={{ marginTop:10, display:'flex', flexDirection:'column', gap:5 }}>
      {Object.entries(counts).map(([kind, n]) => {
        const m = UNI.OVERRIDE_KINDS[kind];
        return <div key={kind} style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <span style={{ display:'flex', alignItems:'center', gap:6 }}>
            <span style={{ width:5, height:5, borderRadius:999, background:m.tone }}/>
            <span style={{ fontFamily:T.sans, fontSize:11, color:T.ink2 }}>{m.label}</span>
          </span>
          <UNI.Mono size={11}>{n}</UNI.Mono>
        </div>;
      })}
    </div>
    <button onClick={() => onJumpTo({ tab:'overrides' })} style={{
      marginTop:12, width:'100%', padding:'7px 10px', background:'transparent',
      border:`1px solid ${T.line}`, borderRadius:6, color:T.ink2,
      fontFamily:T.sans, fontSize:11, cursor:'pointer',
    }}>open overrides log →</button>
  </UNI.Card>;
};

UNI.UniverseSnapshot = function UniverseSnapshot({ onJumpTo }) {
  const T = UNI.TOKENS;
  const byClass = {};
  UNI.SYMBOLS.filter(s => s.stage !== 'banned').forEach(s => { byClass[s.klass] = (byClass[s.klass] || 0) + 1; });
  const total = Object.values(byClass).reduce((a,b) => a+b, 0);
  return <UNI.Card>
    <UNI.Label>composition</UNI.Label>
    <div style={{ marginTop:10, display:'flex', flexDirection:'column', gap:6 }}>
      {Object.entries(byClass).sort((a,b) => b[1]-a[1]).map(([klass, n]) => {
        const pct = (n / total) * 100;
        return <div key={klass}>
          <div style={{ display:'flex', alignItems:'baseline', justifyContent:'space-between', marginBottom:3 }}>
            <span style={{ display:'flex', alignItems:'center', gap:6 }}>
              <UNI.ClassGlyph klass={klass} size={11}/>
              <span style={{ fontFamily:T.sans, fontSize:11, color:T.ink1, textTransform:'capitalize' }}>{klass}</span>
            </span>
            <UNI.Mono size={11}>{n} <span style={{ color:T.ink3 }}>· {pct.toFixed(0)}%</span></UNI.Mono>
          </div>
          <div style={{ height:3, borderRadius:2, background:T.bg3 }}>
            <div style={{ height:'100%', borderRadius:2, width:`${pct}%`, background:UNI.TOKENS.accent, opacity:0.6 }}/>
          </div>
        </div>;
      })}
    </div>
  </UNI.Card>;
};

function Row({ k, v }) {
  return <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
    <span style={{ fontFamily:UNI.TOKENS.sans, fontSize:10, color:UNI.TOKENS.ink3, textTransform:'uppercase', letterSpacing:'0.08em' }}>{k}</span>
    <span>{v}</span>
  </div>;
}
