// ─── Config (read-only) and Overrides (audit) ────────────────────────

UNI.ConfigView = function ConfigView() {
  const T = UNI.TOKENS;
  const c = UNI.CONFIG;
  return <div style={{ display:'flex', flexDirection:'column', gap:18 }}>
    <UNI.Card padding={20}>
      <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', marginBottom:14 }}>
        <div>
          <UNI.Label>discovery config · read only</UNI.Label>
          <h2 style={{ margin:'6px 0 0', fontFamily:T.sans, fontSize:22, fontWeight:300, letterSpacing:'-0.02em', color:T.ink0 }}>
            Rules of the river
          </h2>
          <div style={{ marginTop:6, fontFamily:T.sans, fontSize:12, color:T.ink2, maxWidth: 520, lineHeight:1.5 }}>
            These values come from the backend. Changes happen in admin / overrides
            with staged diff, explicit commit, reason and audit trail — never inline here.
          </div>
        </div>
        <UNI.Pill tone={UNI.TOKENS.accent} dim>read only</UNI.Pill>
      </div>
    </UNI.Card>

    <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:18 }}>
      {/* Capacity */}
      <UNI.Card>
        <UNI.Label>capacity</UNI.Label>
        <div style={{ marginTop:12, display:'flex', flexDirection:'column', gap:8 }}>
          <Kv k="Watching cap" v={UNI.fmt.num(c.capacity.watching)} note="50 core + 250 scan"/>
          <Kv k="Core" v={UNI.fmt.num(c.capacity.core)} note="Always-evaluated tier"/>
          <Kv k="Scan" v={UNI.fmt.num(c.capacity.scan)} note="Sampled tier"/>
          <Kv k="Candidates / loop" v={UNI.fmt.num(c.capacity.candidates)}
            note="Up to N symbols strategy-scored per cycle"/>
        </div>
      </UNI.Card>

      {/* Hard filters */}
      <UNI.Card>
        <UNI.Label>hard filters · eligibility</UNI.Label>
        <div style={{ marginTop:12, display:'flex', flexDirection:'column', gap:8 }}>
          <Kv k="Min liquidity" v={`£${UNI.fmt.num(c.filters.minLiquidityADV)}`} note="Average daily volume"/>
          <Kv k="Max spread" v={`${c.filters.maxSpreadBps} bp`}/>
          <Kv k="Max data age" v={`${c.filters.maxDataAgeSec}s`}/>
          <Kv k="Allowed classes" v={c.filters.allowedClasses.join(', ')}/>
          <Kv k="Allowed regions" v={c.filters.allowedRegions.join(', ')}/>
        </div>
      </UNI.Card>

      {/* Factor weights */}
      <UNI.Card>
        <UNI.Label>factor weights</UNI.Label>
        <div style={{ marginTop:12, display:'flex', flexDirection:'column', gap:6 }}>
          {Object.entries(c.factorWeights).map(([k, v]) => (
            <div key={k}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'baseline', marginBottom:3 }}>
                <span style={{ fontFamily:T.sans, fontSize:11, color:T.ink2, textTransform:'capitalize' }}>{k.replace(/([A-Z])/g, ' $1')}</span>
                <UNI.Mono size={11}>{(v * 100).toFixed(0)}%</UNI.Mono>
              </div>
              <div style={{ height:3, borderRadius:2, background:T.bg3 }}>
                <div style={{ height:'100%', borderRadius:2, width:`${v*100*4}%`, background:UNI.TOKENS.accent, opacity:0.6, maxWidth:'100%' }}/>
              </div>
            </div>
          ))}
        </div>
      </UNI.Card>

      {/* Promotion + rebuild */}
      <UNI.Card>
        <UNI.Label>promotion & rebuild</UNI.Label>
        <div style={{ marginTop:12, display:'flex', flexDirection:'column', gap:8 }}>
          <Kv k="Promotion threshold" v={c.promotion.convictionThreshold} note="Conviction score"/>
          <Kv k="Hold time min" v={`${c.promotion.holdTimeMin}m`} note="Before re-evaluation"/>
          <Kv k="Rebuild interval" v={`${c.rebuild.intervalSec}s`}/>
          <Kv k="Last duration" v={`${(c.rebuild.lastDurationMs/1000).toFixed(1)}s`}/>
        </div>
      </UNI.Card>
    </div>

    <UNI.DataState kind="pending" message="Config endpoints aren't wired yet — values shown are mocked from backend defaults. When the admin schema lands, this view becomes live."/>
  </div>;
};

function Kv({ k, v, note }) {
  const T = UNI.TOKENS;
  return <div style={{ display:'flex', justifyContent:'space-between', alignItems:'baseline', gap:14 }}>
    <span style={{ display:'flex', flexDirection:'column' }}>
      <span style={{ fontFamily:T.sans, fontSize:11, color:T.ink2 }}>{k}</span>
      {note && <span style={{ fontFamily:T.sans, fontSize:10, color:T.ink3, marginTop:2 }}>{note}</span>}
    </span>
    <UNI.Mono size={13} bold>{v}</UNI.Mono>
  </div>;
}

// ─── Overrides — audit log + active list ─────────────────────────────
UNI.OverridesView = function OverridesView({ onSelect }) {
  const T = UNI.TOKENS;
  const [filter, setFilter] = useState('all');

  const log = filter === 'all' ? UNI.OVERRIDES_LOG : UNI.OVERRIDES_LOG.filter(o => o.kind === filter);
  const active = UNI.SYMBOLS.filter(s => s.override);

  return <div style={{ display:'flex', flexDirection:'column', gap:18 }}>
    <UNI.Card padding={20}>
      <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between' }}>
        <div>
          <UNI.Label accent={UNI.TOKENS.danger}>advanced · overrides</UNI.Label>
          <h2 style={{ margin:'6px 0 0', fontFamily:T.sans, fontSize:22, fontWeight:300, letterSpacing:'-0.02em', color:T.ink0 }}>
            Manual interventions
          </h2>
          <div style={{ marginTop:6, fontFamily:T.sans, fontSize:12, color:T.ink2, maxWidth: 540, lineHeight:1.5 }}>
            Overrides are exceptional. Each requires reason, timestamp, optional expiry —
            and is auditable forever. Default behaviour is fully autonomous.
          </div>
        </div>
        <UNI.Pill tone={UNI.TOKENS.danger} dim>requires admin</UNI.Pill>
      </div>
    </UNI.Card>

    <div style={{ display:'grid', gridTemplateColumns:'1.2fr 1fr', gap:18 }}>
      {/* Audit log */}
      <UNI.Card>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12 }}>
          <UNI.Label>audit log</UNI.Label>
          <select value={filter} onChange={e => setFilter(e.target.value)} style={{
            padding:'4px 8px', borderRadius:6, background:T.bg2, color:T.ink1,
            border:`1px solid ${T.line}`, fontFamily:T.sans, fontSize:11,
          }}>
            <option value="all">all kinds</option>
            {Object.entries(UNI.OVERRIDE_KINDS).map(([k, m]) => <option key={k} value={k}>{m.label}</option>)}
          </select>
        </div>
        <div style={{ display:'flex', flexDirection:'column', gap:8, maxHeight: 420, overflowY:'auto' }}>
          {log.map((o, i) => {
            const m = UNI.OVERRIDE_KINDS[o.kind];
            return <div key={i} style={{
              padding:12, borderRadius:8, background:T.bg2,
              border:`1px solid ${T.line}`, borderLeft:`2px solid ${m.tone}`,
            }}>
              <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:6 }}>
                <span style={{ display:'flex', alignItems:'center', gap:10 }}>
                  <UNI.OverrideBadge kind={o.kind}/>
                  <button onClick={() => onSelect(o.sym)} style={{ background:'none', border:'none', padding:0, cursor:'pointer',
                    fontFamily:T.sans, fontSize:13, fontWeight:500, color:T.ink0 }}>{o.sym}</button>
                </span>
                <span style={{ fontFamily:T.mono, fontSize:10, color:T.ink3 }}>{UNI.fmt.ago(o.at)}</span>
              </div>
              <div style={{ fontFamily:T.sans, fontSize:12, color:T.ink2, marginBottom:4 }}>{o.reason}</div>
              <div style={{ display:'flex', alignItems:'center', gap:14, fontFamily:T.mono, fontSize:10, color:T.ink3 }}>
                <span>by {o.by}</span>
                {o.expiresAt
                  ? <span style={{ color:UNI.TOKENS.caution }}>expires {UNI.fmt.in(o.expiresAt)}</span>
                  : <span>no expiry</span>}
              </div>
            </div>;
          })}
        </div>
      </UNI.Card>

      {/* Currently active */}
      <UNI.Card>
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', marginBottom:12 }}>
          <UNI.Label>currently active</UNI.Label>
          <UNI.Mono size={10} tone={T.ink3}>{active.length}</UNI.Mono>
        </div>
        <div style={{ display:'flex', flexDirection:'column', gap:6, maxHeight:420, overflowY:'auto' }}>
          {active.map(s => {
            const m = UNI.OVERRIDE_KINDS[s.override.kind];
            return <button key={s.sym} onClick={() => onSelect(s.sym)} style={{
              padding:'8px 10px', borderRadius:6, background:'transparent',
              border:`1px solid ${T.line}`, display:'flex', alignItems:'center', gap:10,
              cursor:'pointer', textAlign:'left',
            }}>
              <UNI.ClassGlyph klass={s.klass}/>
              <span style={{ fontFamily:T.sans, fontSize:12, fontWeight:500, color:T.ink0, width:60 }}>{s.sym}</span>
              <UNI.OverrideBadge kind={s.override.kind} compact/>
              <span style={{ flex:1, fontFamily:T.sans, fontSize:11, color:T.ink3, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{s.override.reason}</span>
              {s.override.expiresAt && <UNI.Mono size={9} tone={UNI.TOKENS.caution}>{UNI.fmt.in(s.override.expiresAt)}</UNI.Mono>}
            </button>;
          })}
        </div>
      </UNI.Card>
    </div>

    {/* Override kinds reference */}
    <UNI.Card>
      <UNI.Label>kinds</UNI.Label>
      <div style={{ marginTop:12, display:'grid', gridTemplateColumns:'repeat(3, 1fr)', gap:14 }}>
        {Object.entries(UNI.OVERRIDE_KINDS).filter(([k]) => k !== 'auto-blacklist').map(([k, m]) => (
          <div key={k} style={{ padding:10, borderRadius:8, background:T.bg2, border:`1px solid ${T.line}`, borderLeft:`2px solid ${m.tone}` }}>
            <UNI.OverrideBadge kind={k}/>
            <div style={{ marginTop:8, fontFamily:T.sans, fontSize:11, color:T.ink2, lineHeight:1.5 }}>{m.desc}</div>
          </div>
        ))}
      </div>
    </UNI.Card>
  </div>;
};
