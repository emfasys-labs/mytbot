// ─── Funnel deep dive ────────────────────────────────────────────────
// Per-stage drop reasons, tier reasons, and a horizontal flow diagram.

UNI.FunnelView = function FunnelView({ onJumpTo, onSelect }) {
  const T = UNI.TOKENS;
  return <div style={{ display:'flex', flexDirection:'column', gap:18 }}>
    {/* Vertical flow with drop reasons branching off */}
    <UNI.Card padding={24}>
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', marginBottom:18 }}>
        <div>
          <UNI.Label>funnel · why symbols drop</UNI.Label>
          <h2 style={{ margin:'6px 0 0', fontFamily:T.sans, fontSize:24, fontWeight:300, letterSpacing:'-0.02em', color:T.ink0 }}>
            8,742 → 7 active
          </h2>
        </div>
        <UNI.BuildPill build={UNI.BUILD}/>
      </div>

      <UNI.VerticalFunnel onJumpTo={onJumpTo}/>
    </UNI.Card>

    <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:18 }}>
      <UNI.TierBreakdown onSelect={onSelect}/>
      <UNI.BannedView onSelect={onSelect}/>
    </div>
  </div>;
};

UNI.VerticalFunnel = function VerticalFunnel({ onJumpTo }) {
  const T = UNI.TOKENS;
  const stages = UNI.FUNNEL.filter(f => f.stage !== 'banned');
  const total = stages[0].count;

  return <div style={{ display:'flex', flexDirection:'column', gap:14 }}>
    {stages.map((f, i) => {
      const c = UNI.STAGE_COLORS[f.stage];
      const next = stages[i + 1];
      const dropCount = next ? f.count - next.count : null;
      const widthPct = Math.max(8, (f.count / total) * 100);
      const nextPct = next ? Math.max(8, (next.count / total) * 100) : 0;

      return <div key={f.stage}>
        <div style={{ display:'flex', alignItems:'center', gap:16 }}>
          {/* stage row */}
          <button onClick={() => onJumpTo({ tab:'instruments', stage:f.stage })} style={{
            flex: 1, position:'relative', padding:'14px 18px',
            background: `linear-gradient(90deg, ${c}14, ${c}05)`,
            border:`1px solid ${c}55`, borderLeft:`3px solid ${c}`,
            borderRadius: 8, cursor:'pointer', textAlign:'left',
            display:'flex', alignItems:'center', justifyContent:'space-between',
            transition:`all 200ms ${T.ease}`,
          }}>
            <div>
              <UNI.Label accent={c}>{UNI.STAGE_LABELS[f.stage]}</UNI.Label>
              <div style={{ fontFamily:T.sans, fontSize:11, color:T.ink3, marginTop:3 }}>{UNI.STAGE_DESC[f.stage]}</div>
            </div>
            <div style={{ display:'flex', alignItems:'baseline', gap:14 }}>
              <span style={{ fontFamily:T.sans, fontSize:24, fontWeight:300, color:T.ink0, letterSpacing:'-0.02em' }}>
                {UNI.fmt.num(f.count)}
              </span>
              <UNI.Mono size={10} tone={T.ink3}>{((f.count/total)*100).toFixed(1)}%</UNI.Mono>
            </div>
          </button>
        </div>

        {/* drop reasons branch */}
        {f.drops && next && <div style={{
          margin: '8px 0 8px 28px', display:'flex', alignItems:'flex-start', gap:14,
          paddingLeft:18, borderLeft:`1px dashed ${T.line}`,
        }}>
          <div style={{ flex:1 }}>
            <UNI.Mono size={10} tone={T.ink3}>− {UNI.fmt.num(dropCount)} dropped</UNI.Mono>
            <div style={{ marginTop:6, display:'flex', flexDirection:'column', gap:4 }}>
              {f.drops.map(d => {
                const pct = (d.count / dropCount) * 100;
                return <div key={d.reason} style={{ display:'flex', alignItems:'center', gap:10 }}>
                  <div style={{ flex:1, fontFamily:T.sans, fontSize:11, color:T.ink2 }}>{d.reason}</div>
                  <div style={{ width:120, height:3, borderRadius:2, background:T.bg3 }}>
                    <div style={{ height:'100%', borderRadius:2, width:`${pct}%`, background:T.ink3 }}/>
                  </div>
                  <UNI.Mono size={10} tone={T.ink2}>{UNI.fmt.num(d.count)}</UNI.Mono>
                </div>;
              })}
            </div>
          </div>
        </div>}
      </div>;
    })}
  </div>;
};

UNI.TierBreakdown = function TierBreakdown({ onSelect }) {
  const T = UNI.TOKENS;
  const watching = UNI.SYMBOLS.filter(s => s.stage === 'watching');
  const core = watching.filter(s => s.tierReason === 'core');
  const scan = watching.filter(s => s.tierReason === 'scan');
  return <UNI.Card>
    <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
      <UNI.Label>watching · tier reason</UNI.Label>
      <UNI.Mono size={10} tone={T.ink3}>{watching.length} symbols</UNI.Mono>
    </div>

    <div style={{ marginTop:14 }}>
      <Tier label="Core" desc="Always-evaluated, fully scored every loop" count={core.length} cap={50} tone={T.ink0}/>
      <Tier label="Scan" desc="Sampled & scored opportunistically" count={scan.length} cap={250} tone={UNI.TOKENS.accent}/>
    </div>

    <div style={{ marginTop:12, padding:10, background:`${T.info}08`, border:`1px solid ${T.info}33`, borderRadius:6, fontFamily:T.sans, fontSize:11, color:T.ink2, lineHeight:1.5 }}>
      <span style={{ color:T.info, fontWeight:500 }}>Honest note.</span> Not all 300 watched symbols are
      strategy-scored every loop. The system caps at 50 core + 250 scan, with up to 400 candidates
      evaluated per cycle. Counts here are real; promotion is best-effort.
    </div>
  </UNI.Card>;
};

function Tier({ label, desc, count, cap, tone }) {
  const T = UNI.TOKENS;
  const pct = (count / cap) * 100;
  return <div style={{ marginBottom: 12 }}>
    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'baseline', marginBottom:5 }}>
      <span style={{ fontFamily:T.sans, fontSize:13, fontWeight:500, color:T.ink0 }}>
        {label} <span style={{ color:T.ink3, fontWeight:400, fontSize:11, marginLeft:6 }}>{desc}</span>
      </span>
      <UNI.Mono size={12} tone={tone} bold>{count}<span style={{ color:T.ink3, fontWeight:400 }}> / {cap}</span></UNI.Mono>
    </div>
    <div style={{ height:5, borderRadius:3, background:T.bg3, overflow:'hidden' }}>
      <div style={{ height:'100%', width:`${pct}%`, background:tone, opacity: 0.7, borderRadius:3, transition:`width 400ms ${T.ease}` }}/>
    </div>
  </div>;
}

UNI.BannedView = function BannedView({ onSelect }) {
  const T = UNI.TOKENS;
  const banned = UNI.SYMBOLS.filter(s => s.stage === 'banned');
  return <UNI.Card>
    <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
      <UNI.Label accent={T.danger}>banned / blocked</UNI.Label>
      <UNI.Mono size={10} tone={T.ink3}>{banned.length} symbols</UNI.Mono>
    </div>

    <div style={{ marginTop:12, display:'flex', flexDirection:'column', gap:5, maxHeight:340, overflowY:'auto' }}>
      {banned.map(s => {
        const m = UNI.OVERRIDE_KINDS[s.override.kind];
        return <button key={s.sym} onClick={() => onSelect(s.sym)} style={{
          padding:'8px 10px', borderRadius:6,
          background:'transparent', border:`1px solid ${T.line}`,
          display:'flex', alignItems:'center', gap:10, cursor:'pointer', textAlign:'left',
        }}>
          <UNI.ClassGlyph klass={s.klass}/>
          <span style={{ fontFamily:T.sans, fontSize:12, fontWeight:500, color:T.ink0, width:64 }}>{s.sym}</span>
          <UNI.OverrideBadge kind={s.override.kind} compact/>
          <span style={{ flex:1, fontFamily:T.sans, fontSize:11, color:T.ink3, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>
            {s.override.reason}
          </span>
          <UNI.Mono size={10} tone={T.ink3}>{UNI.fmt.ago(s.override.at)}</UNI.Mono>
        </button>;
      })}
    </div>
  </UNI.Card>;
};
