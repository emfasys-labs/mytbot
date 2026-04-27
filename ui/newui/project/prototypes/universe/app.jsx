// ─── App shell ────────────────────────────────────────────────────────
const { useState: appUseState, useEffect: appUseEffect } = React;

function App() {
  const T = UNI.TOKENS;
  const [tab, setTab] = appUseState('overview');
  const [stageFilter, setStageFilter] = appUseState(null);
  const [selected, setSelected] = appUseState(null);

  // ESC to close inspector
  appUseEffect(() => {
    const h = (e) => { if (e.key === 'Escape') setSelected(null); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  const onJumpTo = ({ tab: t, stage }) => {
    setTab(t);
    if (stage) setStageFilter(stage);
  };

  const TABS = [
    { k:'overview',    label:'Overview',    desc:'River at a glance' },
    { k:'funnel',      label:'Funnel',      desc:'Why symbols drop' },
    { k:'instruments', label:'Instruments', desc:'Constellation · grid · list' },
    { k:'config',      label:'Config',      desc:'Read-only rules' },
    { k:'overrides',   label:'Overrides',   desc:'Manual interventions' },
  ];

  return <div style={{
    minHeight:'100vh',
    background: `radial-gradient(ellipse at top left, ${UNI.TOKENS.accent}08, transparent 50%), ${T.bg0}`,
    color: T.ink1,
  }}>
    <Shell tab={tab} setTab={(t) => { setTab(t); setStageFilter(null); }} TABS={TABS}>
      {tab === 'overview'    && <UNI.Overview onSelect={setSelected} onJumpTo={onJumpTo}/>}
      {tab === 'funnel'      && <UNI.FunnelView onJumpTo={onJumpTo} onSelect={setSelected}/>}
      {tab === 'instruments' && <UNI.InstrumentsView onSelect={setSelected} initialStage={stageFilter}/>}
      {tab === 'config'      && <UNI.ConfigView/>}
      {tab === 'overrides'   && <UNI.OverridesView onSelect={setSelected}/>}
    </Shell>

    {selected && <UNI.Inspector sym={selected} onClose={() => setSelected(null)}/>}
  </div>;
}

function Shell({ tab, setTab, TABS, children }) {
  const T = UNI.TOKENS;
  return <div style={{ maxWidth: 1440, margin: '0 auto' }}>
    {/* Top bar */}
    <header style={{
      display:'flex', alignItems:'center', justifyContent:'space-between',
      padding:'18px 28px', borderBottom:`1px solid ${T.line}`,
    }}>
      <div style={{ display:'flex', alignItems:'center', gap:14 }}>
        <span style={{ display:'flex', alignItems:'center', gap:8 }}>
          <span style={{ width: 8, height: 8, borderRadius:999, background: UNI.TOKENS.accent, boxShadow:`0 0 10px ${UNI.TOKENS.accent}88` }}/>
          <span style={{ fontFamily:T.sans, fontSize:14, fontWeight:500, color:T.ink0, letterSpacing:'-0.02em' }}>mytbot</span>
          <span style={{ fontFamily:T.sans, fontSize:13, color:T.ink3 }}>/ universe</span>
        </span>
      </div>

      <div style={{ display:'flex', alignItems:'center', gap:10 }}>
        <UNI.BuildPill build={UNI.BUILD} compact/>
        <span style={{ fontFamily:T.mono, fontSize:10, color:T.ink3 }}>· running · loop #4218 · global_edge · ws</span>
      </div>
    </header>

    {/* Sub-tabs */}
    <nav style={{
      padding:'0 28px', borderBottom:`1px solid ${T.line}`,
      display:'flex', alignItems:'center', gap:0,
    }}>
      {TABS.map(t => (
        <button key={t.k} onClick={() => setTab(t.k)} style={{
          background:'transparent', border:'none',
          padding:'14px 18px', cursor:'pointer',
          color: tab === t.k ? T.ink0 : T.ink2,
          fontFamily:T.sans, fontSize:13, fontWeight: tab === t.k ? 500 : 400,
          position:'relative',
        }}>
          {t.label}
          <span style={{ marginLeft:6, fontSize:11, fontWeight:400, color:T.ink3 }}>· {t.desc}</span>
          {tab === t.k && <span style={{
            position:'absolute', bottom:-1, left:14, right:14, height:2,
            background: UNI.TOKENS.accent, borderRadius:1,
          }}/>}
        </button>
      ))}
    </nav>

    {/* Content */}
    <main style={{ padding:'24px 28px 64px' }}>
      {children}
    </main>
  </div>;
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
