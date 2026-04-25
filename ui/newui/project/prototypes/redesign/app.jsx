// ─── App root ────────────────────────────────────────────────

function App() {
  const defaults = /*EDITMODE-BEGIN*/{
    "accent": "cyan",
    "density": "comfort",
    "state": "running",
    "theme": "dark",
    "viewport": "desktop"
  }/*EDITMODE-END*/;

  const [tweaks, setTweaks] = useState(() => {
    try { return { ...defaults, ...JSON.parse(localStorage.getItem('mytbot-tweaks') || '{}') }; }
    catch { return defaults; }
  });
  useEffect(() => { localStorage.setItem('mytbot-tweaks', JSON.stringify(tweaks)); }, [tweaks]);

  const [route, setRoute] = useState(() => localStorage.getItem('mytbot-route') || 'dash');
  useEffect(() => { localStorage.setItem('mytbot-route', route); }, [route]);

  const [cmdOpen, setCmdOpen] = useState(false);
  const [tweaksOpen, setTweaksOpen] = useState(false);
  const [armed, setArmed] = useState(false);

  const accent = ACCENTS[tweaks.accent].main;
  const state = tweaks.state;

  // Tweaks panel availability
  useEffect(() => {
    const handler = (e) => {
      if (e.data?.type === '__activate_edit_mode') setTweaksOpen(true);
      if (e.data?.type === '__deactivate_edit_mode') setTweaksOpen(false);
    };
    window.addEventListener('message', handler);
    window.parent.postMessage({ type: '__edit_mode_available' }, '*');
    return () => window.removeEventListener('message', handler);
  }, []);

  // ⌘K shortcut
  useEffect(() => {
    const h = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); setCmdOpen(o => !o); }
      if (e.key === 'Escape') { setCmdOpen(false); setArmed(false); }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  // Armed confirmation → after 1.4s kill
  useEffect(() => {
    if (armed) {
      const t = setTimeout(() => {
        setArmed(false);
        setTweaks(t => ({ ...t, state: 'off' }));
      }, 1800);
      return () => clearTimeout(t);
    }
  }, [armed]);

  function togglePower() {
    setTweaks(t => ({ ...t, state: t.state === 'running' ? 'paused' : 'running' }));
  }

  const titles = { dash: 'Dashboard', signals: 'Signals', book: 'Book', risk: 'Risk', strat: 'Strategies', log: 'Trade log' };

  // Light theme overrides
  const isLight = tweaks.theme === 'light';
  const bg = isLight ? '#f7f7f5' : TOKENS.bg0;
  const sidebarBg = isLight ? '#efefec' : TOKENS.bg0;

  // Mobile/tablet viewport showcase
  if (tweaks.viewport === 'mobile') {
    return (
      <div data-screen-label={`mobile · ${titles[route]}`} style={{ width: '100vw', height: '100vh', background: TOKENS.bg0, display: 'flex', flexDirection: 'column', alignItems: 'center', overflow: 'auto', padding: '20px 0' }}>
        <MobileApp state={state} accent={tweaks.accent} armed={armed} onArm={setArmed} onPower={togglePower} />
        <TweaksPanel open={tweaksOpen} onClose={() => setTweaksOpen(false)} tweaks={tweaks} setTweaks={setTweaks} />
      </div>
    );
  }

  const containerStyle = tweaks.viewport === 'tablet'
    ? { width: 1024, height: 768, margin: '20px auto', border: `1px solid ${TOKENS.lineStrong}`, borderRadius: 12, overflow: 'hidden' }
    : { width: '100vw', height: '100vh' };

  return (
    <div data-screen-label={`${tweaks.viewport} · ${titles[route]}`} style={{
      ...containerStyle,
      display: 'flex', background: bg, color: TOKENS.ink1,
      filter: isLight ? 'invert(0.93) hue-rotate(180deg)' : 'none',
    }}>
      <Sidebar current={route} onNav={setRoute} accent={accent} state={state} collapsed={tweaks.density === 'compact'}/>
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <TopBar state={state} accent={accent} onArm={setArmed} armed={armed} onPower={togglePower} currentTitle={titles[route]} onOpenCmd={() => setCmdOpen(true)}/>
        <div style={{ flex: 1, minHeight: 0, background: TOKENS.bg0, position: 'relative' }}>
          {route === 'dash'    && <DashboardScreen state={state} accent={tweaks.accent} density={tweaks.density} onArm={setArmed} armed={armed}/>}
          {route === 'signals' && <SignalsScreen accent={tweaks.accent}/>}
          {route === 'book'    && <BookScreen accent={tweaks.accent}/>}
          {route === 'risk'    && <RiskScreen accent={tweaks.accent}/>}
          {route === 'strat'   && <StrategiesScreen accent={tweaks.accent}/>}
          {route === 'log'     && <TradeLogScreen/>}
          {state === 'error' && <ErrorOverlay accent={accent}/>}
        </div>
      </main>
      <CmdPalette open={cmdOpen} onClose={() => setCmdOpen(false)} onNav={setRoute}/>
      <TweaksPanel open={tweaksOpen} onClose={() => setTweaksOpen(false)} tweaks={tweaks} setTweaks={setTweaks}/>
      {armed && <ArmOverlay/>}
    </div>
  );
}

function ArmOverlay() {
  return (
    <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 50 }}>
      <div style={{ position: 'absolute', inset: 0, border: `2px solid ${TOKENS.danger}`, animation: 'pulse 0.9s ease-in-out infinite' }}/>
      <div style={{ position: 'absolute', top: 16, left: '50%', transform: 'translateX(-50%)', padding: '8px 14px', background: TOKENS.bg2, border: `1px solid ${TOKENS.danger}`, borderRadius: 8, color: TOKENS.danger, fontFamily: TOKENS.sans, fontSize: 12, fontWeight: 500, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
        stopping · flattening book
      </div>
    </div>
  );
}

function ErrorOverlay({ accent }) {
  return (
    <div style={{ position: 'absolute', top: 18, right: 18, zIndex: 40, padding: 14, background: TOKENS.bg2, border: `1px solid ${TOKENS.danger}`, borderRadius: 10, boxShadow: '0 20px 40px rgba(0,0,0,0.4)', maxWidth: 320 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <Glyph state="error" size={12}/>
        <Label style={{ color: TOKENS.danger }}>Broker disconnected</Label>
      </div>
      <div style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink1, lineHeight: 1.4 }}>
        ibkr-gateway unreachable for 45s. Trading halted. Positions preserved. Retrying in 12s.
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
