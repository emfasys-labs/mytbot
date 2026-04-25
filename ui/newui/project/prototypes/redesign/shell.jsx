// ─── App shell: sidebar, top bar, command palette, tweaks ─────

function Sidebar({ current, onNav, accent, state, collapsed }) {
  const nav = [
    { id: 'dash',    label: 'Dashboard',  icon: I.dash },
    { id: 'signals', label: 'Signals',    icon: I.signal },
    { id: 'book',    label: 'Book',       icon: I.wallet },
    { id: 'risk',    label: 'Risk',       icon: I.shield },
    { id: 'strat',   label: 'Strategies', icon: I.brain },
    { id: 'log',     label: 'Trade log',  icon: I.log },
  ];
  const w = collapsed ? 56 : 200;
  return (
    <aside style={{
      width: w, flexShrink: 0, background: TOKENS.bg0, borderRight: `1px solid ${TOKENS.line}`,
      display: 'flex', flexDirection: 'column', padding: '14px 10px',
      transition: `width ${TOKENS.med}ms ${TOKENS.ease}`,
    }}>
      <div style={{ padding: '4px 8px 18px', display: 'flex', alignItems: 'center' }}>
        {collapsed
          ? <Glyph state={state} accent={accent} size={14} />
          : <Wordmark state={state} accent={accent} size={17} />}
      </div>
      <nav style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {nav.map(n => {
          const active = n.id === current;
          return (
            <button key={n.id} onClick={() => onNav(n.id)} style={{
              display: 'flex', alignItems: 'center', gap: 12, padding: '8px 10px',
              borderRadius: 8, background: active ? 'rgba(255,255,255,0.06)' : 'transparent',
              border: 'none', cursor: 'pointer',
              color: active ? TOKENS.ink0 : TOKENS.ink2,
              fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 450,
              letterSpacing: '-0.01em', textAlign: 'left',
              transition: `background ${TOKENS.fast}ms ${TOKENS.ease}, color ${TOKENS.fast}ms ${TOKENS.ease}`,
            }}
            onMouseEnter={e => !active && (e.currentTarget.style.background = 'rgba(255,255,255,0.03)')}
            onMouseLeave={e => !active && (e.currentTarget.style.background = 'transparent')}
            >
              <span style={{ color: active ? accent : TOKENS.ink3, display: 'flex' }}>{n.icon()}</span>
              {!collapsed && <span>{n.label}</span>}
            </button>
          );
        })}
      </nav>
      <div style={{ flex: 1 }} />
      {!collapsed && (
        <div style={{ padding: '10px', borderTop: `1px solid ${TOKENS.line}`, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            <span style={{ color: accent }}>⌘K</span>
            <span>command</span>
          </div>
          <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4 }}>
            path {DATA.path} · #{DATA.loop}
          </div>
        </div>
      )}
    </aside>
  );
}

// TopBar — slim, with live tape
function TopBar({ state, accent, onArm, onPower, armed, currentTitle, onOpenCmd }) {
  return (
    <div style={{
      height: 48, flexShrink: 0, display: 'flex', alignItems: 'center',
      borderBottom: `1px solid ${TOKENS.line}`, padding: '0 18px',
      background: TOKENS.bg0,
    }}>
      <div style={{
        fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500,
        color: TOKENS.ink1, letterSpacing: '-0.01em',
      }}>
        {currentTitle}
      </div>
      <div style={{ marginLeft: 18, display: 'flex', alignItems: 'center', gap: 8, fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
        <Glyph state={state} accent={accent} size={10} />
        <span style={{ color: state === 'running' ? accent : state === 'error' ? TOKENS.danger : TOKENS.ink3 }}>{state}</span>
        <span style={{ color: TOKENS.ink4 }}>·</span>
        <span>loop #{DATA.loop}</span>
        <span style={{ color: TOKENS.ink4 }}>·</span>
        <span>{DATA.path}</span>
      </div>
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
        <button onClick={onOpenCmd} style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
          background: TOKENS.bg2, border: `1px solid ${TOKENS.line}`, borderRadius: 8,
          color: TOKENS.ink2, fontFamily: TOKENS.sans, fontSize: 12, cursor: 'pointer',
          letterSpacing: '-0.01em',
        }}>
          <span style={{ color: TOKENS.ink3 }}>Search, jump, command</span>
          <span style={{
            fontFamily: TOKENS.mono, fontSize: 10, padding: '1px 5px',
            borderRadius: 4, background: TOKENS.bg3, color: TOKENS.ink3,
          }}>⌘K</span>
        </button>
        <MasterButton state={state} accent={accent} onArm={onArm} onPower={onPower} armed={armed} />
      </div>
    </div>
  );
}

// MasterButton — single tap = run/pause · hold = arm to stop
function MasterButton({ state, accent, onArm, onPower, armed }) {
  const [pressing, setPressing] = useState(false);
  const [progress, setProgress] = useState(0);
  const timerRef = useRef(null);
  const rafRef = useRef(null);
  const startRef = useRef(0);
  const holdMs = 900;

  const running = state === 'running';
  const off = state === 'off';
  const danger = armed;

  const c = danger ? TOKENS.danger : running ? accent : off ? TOKENS.ink3 : TOKENS.caution;

  function down() {
    if (off) return onPower();
    setPressing(true);
    startRef.current = performance.now();
    const tick = () => {
      const p = Math.min(1, (performance.now() - startRef.current) / holdMs);
      setProgress(p);
      if (p < 1) rafRef.current = requestAnimationFrame(tick);
      else {
        onArm(true);
        setPressing(false);
      }
    };
    rafRef.current = requestAnimationFrame(tick);
  }
  function up() {
    cancelAnimationFrame(rafRef.current);
    if (pressing) {
      if (progress < 1) onPower();  // tap = toggle
      setProgress(0);
      setPressing(false);
    }
  }
  function cancel() {
    cancelAnimationFrame(rafRef.current);
    setPressing(false);
    setProgress(0);
  }

  return (
    <div style={{ position: 'relative' }}>
      <button
        onMouseDown={down} onMouseUp={up} onMouseLeave={cancel}
        onTouchStart={down} onTouchEnd={up}
        style={{
          position: 'relative', width: 80, height: 32, borderRadius: 8,
          border: `1px solid ${c}44`, background: `${c}10`,
          color: c, fontFamily: TOKENS.sans, fontSize: 11, fontWeight: 500,
          letterSpacing: '0.04em', textTransform: 'uppercase',
          cursor: 'pointer', overflow: 'hidden',
          transition: `border-color ${TOKENS.fast}ms ${TOKENS.ease}`,
        }}
      >
        <div style={{
          position: 'absolute', inset: 0, background: c, opacity: progress * 0.35,
          transition: 'opacity 60ms linear',
        }} />
        <span style={{ position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6 }}>
          <I.power />
          {armed ? 'stop?' : running ? 'live' : off ? 'start' : 'paused'}
        </span>
      </button>
      {pressing && !armed && (
        <div style={{ position: 'absolute', bottom: -14, left: 0, right: 0, fontFamily: TOKENS.mono, fontSize: 9, color: TOKENS.ink3, textAlign: 'center' }}>
          hold to arm
        </div>
      )}
    </div>
  );
}

// Command palette — ⌘K
function CmdPalette({ open, onClose, onNav }) {
  const [q, setQ] = useState('');
  const items = [
    { id: 'dash', label: 'Go to Dashboard', hint: 'overview', kind: 'nav' },
    { id: 'signals', label: 'Go to Signals', hint: 'conviction', kind: 'nav' },
    { id: 'book', label: 'Go to Book', hint: 'positions', kind: 'nav' },
    { id: 'risk', label: 'Go to Risk', hint: 'approvals', kind: 'nav' },
    { id: 'strat', label: 'Go to Strategies', hint: 'performance', kind: 'nav' },
    { id: 'log', label: 'Go to Trade log', hint: 'events', kind: 'nav' },
    { id: 'pause', label: 'Pause system',    hint: 'soft stop',    kind: 'action' },
    { id: 'flatten', label: 'Flatten book',  hint: 'close all',    kind: 'action' },
    { id: 'query-nvda', label: 'NVDA',       hint: 'conviction 0.84', kind: 'sym' },
    { id: 'query-aapl', label: 'AAPL',       hint: 'conviction 0.71', kind: 'sym' },
  ];
  const filtered = q ? items.filter(i => i.label.toLowerCase().includes(q.toLowerCase()) || i.hint.toLowerCase().includes(q.toLowerCase())) : items;

  useEffect(() => {
    if (!open) setQ('');
  }, [open]);

  if (!open) return null;
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
      backdropFilter: 'blur(12px)', zIndex: 100, display: 'flex', justifyContent: 'center',
      paddingTop: '12vh', animation: 'fadeIn 160ms ease',
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        width: 520, maxHeight: '60vh', background: TOKENS.bg1,
        border: `1px solid ${TOKENS.lineStrong}`, borderRadius: 12,
        boxShadow: '0 20px 60px rgba(0,0,0,0.6)', overflow: 'hidden',
        display: 'flex', flexDirection: 'column',
      }}>
        <input
          autoFocus value={q} onChange={e => setQ(e.target.value)}
          placeholder="Search, jump, run commands…"
          style={{
            padding: 16, background: 'transparent', border: 'none', outline: 'none',
            color: TOKENS.ink0, fontFamily: TOKENS.sans, fontSize: 15, fontWeight: 400,
            letterSpacing: '-0.01em', borderBottom: `1px solid ${TOKENS.line}`,
          }}
        />
        <div style={{ flex: 1, overflowY: 'auto', padding: 8 }}>
          {filtered.length === 0 ? (
            <div style={{ padding: 14, color: TOKENS.ink3, fontSize: 12 }}>No matches</div>
          ) : filtered.map((it, i) => (
            <button key={it.id} onClick={() => { if (it.kind === 'nav') onNav(it.id); onClose(); }} style={{
              display: 'flex', alignItems: 'center', width: '100%', padding: '10px 12px',
              background: 'transparent', border: 'none', borderRadius: 8, cursor: 'pointer',
              color: TOKENS.ink1, fontFamily: TOKENS.sans, fontSize: 13, textAlign: 'left',
              justifyContent: 'space-between',
            }}
            onMouseEnter={e => e.currentTarget.style.background = 'rgba(255,255,255,0.04)'}
            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{
                  fontFamily: TOKENS.mono, fontSize: 9, padding: '2px 6px', borderRadius: 4,
                  background: TOKENS.bg3, color: TOKENS.ink3, textTransform: 'uppercase',
                }}>{it.kind}</span>
                {it.label}
              </span>
              <span style={{ color: TOKENS.ink3, fontSize: 11 }}>{it.hint}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

// Tweaks panel — floating control
function TweaksPanel({ open, onClose, tweaks, setTweaks }) {
  if (!open) return null;
  const row = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 0', borderBottom: `1px solid ${TOKENS.line}`, gap: 10 };
  const lbl = { fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink2, letterSpacing: '-0.01em' };
  const setK = (k, v) => {
    setTweaks({ ...tweaks, [k]: v });
    window.parent.postMessage({ type: '__edit_mode_set_keys', edits: { [k]: v } }, '*');
  };
  return (
    <div style={{
      position: 'fixed', bottom: 18, right: 18, width: 260,
      background: TOKENS.bg1, border: `1px solid ${TOKENS.lineStrong}`,
      borderRadius: 12, padding: 14, zIndex: 80,
      boxShadow: '0 20px 40px rgba(0,0,0,0.5)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <Label>Tweaks</Label>
        <button onClick={onClose} style={{ background: 'none', border: 'none', color: TOKENS.ink3, cursor: 'pointer', padding: 2 }}><I.x/></button>
      </div>

      <div style={row}>
        <span style={lbl}>Accent</span>
        <div style={{ display: 'flex', gap: 4 }}>
          {Object.entries(ACCENTS).map(([k, v]) => (
            <button key={k} onClick={() => setK('accent', k)} style={{
              width: 18, height: 18, borderRadius: 999, border: `1.5px solid ${tweaks.accent === k ? TOKENS.ink0 : 'transparent'}`,
              background: v.main, cursor: 'pointer', padding: 0,
            }} title={k}/>
          ))}
        </div>
      </div>

      <div style={row}>
        <span style={lbl}>Density</span>
        <div style={{ display: 'flex', gap: 4 }}>
          {['comfort', 'compact'].map(d => (
            <button key={d} onClick={() => setK('density', d)} style={{
              padding: '3px 8px', fontSize: 10, borderRadius: 4,
              background: tweaks.density === d ? 'rgba(255,255,255,0.08)' : 'transparent',
              color: tweaks.density === d ? TOKENS.ink0 : TOKENS.ink3,
              border: `1px solid ${tweaks.density === d ? TOKENS.lineStrong : TOKENS.line}`,
              cursor: 'pointer', fontFamily: TOKENS.sans,
            }}>{d}</button>
          ))}
        </div>
      </div>

      <div style={row}>
        <span style={lbl}>System state</span>
        <div style={{ display: 'flex', gap: 4 }}>
          {['running', 'paused', 'off', 'error'].map(s => (
            <button key={s} onClick={() => setK('state', s)} style={{
              padding: '3px 6px', fontSize: 9, borderRadius: 4,
              background: tweaks.state === s ? 'rgba(255,255,255,0.08)' : 'transparent',
              color: tweaks.state === s ? TOKENS.ink0 : TOKENS.ink3,
              border: `1px solid ${tweaks.state === s ? TOKENS.lineStrong : TOKENS.line}`,
              cursor: 'pointer', fontFamily: TOKENS.sans,
            }}>{s}</button>
          ))}
        </div>
      </div>

      <div style={row}>
        <span style={lbl}>Theme</span>
        <div style={{ display: 'flex', gap: 4 }}>
          {['dark', 'light'].map(t => (
            <button key={t} onClick={() => setK('theme', t)} style={{
              padding: '3px 8px', fontSize: 10, borderRadius: 4,
              background: tweaks.theme === t ? 'rgba(255,255,255,0.08)' : 'transparent',
              color: tweaks.theme === t ? TOKENS.ink0 : TOKENS.ink3,
              border: `1px solid ${tweaks.theme === t ? TOKENS.lineStrong : TOKENS.line}`,
              cursor: 'pointer', fontFamily: TOKENS.sans,
            }}>{t}</button>
          ))}
        </div>
      </div>

      <div style={{ ...row, borderBottom: 'none' }}>
        <span style={lbl}>View</span>
        <div style={{ display: 'flex', gap: 4 }}>
          {['desktop', 'tablet', 'mobile'].map(v => (
            <button key={v} onClick={() => setK('viewport', v)} style={{
              padding: '3px 8px', fontSize: 10, borderRadius: 4,
              background: tweaks.viewport === v ? 'rgba(255,255,255,0.08)' : 'transparent',
              color: tweaks.viewport === v ? TOKENS.ink0 : TOKENS.ink3,
              border: `1px solid ${tweaks.viewport === v ? TOKENS.lineStrong : TOKENS.line}`,
              cursor: 'pointer', fontFamily: TOKENS.sans,
            }}>{v}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { Sidebar, TopBar, MasterButton, CmdPalette, TweaksPanel });
