// ─── Dashboard — the main cockpit ────────────────────────────
// Hero NAV, conviction river, book pulse, live event feed.

function DashboardScreen({ state, accent, density, onArm, armed }) {
  const [navVal, setNavVal] = useState(DATA.nav);
  const [milestoneFlash, setMilestoneFlash] = useState(false);
  const [newSignal, setNewSignal] = useState(null);
  const [events, setEvents] = useState(DATA.events);
  const running = state === 'running';
  const accentColor = ACCENTS[accent].main;
  const accentGlow = ACCENTS[accent].glow;

  // Simulated live tick — NAV drifts, new signals arrive
  useEffect(() => {
    if (!running) return;
    const tick = setInterval(() => {
      setNavVal(v => v + (Math.random() - 0.45) * 35);
    }, 2200);
    const signalTimer = setInterval(() => {
      const syms = ['NVDA', 'AAPL', 'MSFT', 'BTC', 'AMZN', 'SPY'];
      const sym = syms[Math.floor(Math.random() * syms.length)];
      const score = 0.55 + Math.random() * 0.35;
      setNewSignal({ sym, score, t: Date.now() });
      setEvents(e => [{ kind: 'signal', text: `${sym} long ${score.toFixed(2)} · approved`, ok: true, t: 0 }, ...e].slice(0, 12));
      setTimeout(() => setNewSignal(null), 2400);
    }, 7000);
    return () => { clearInterval(tick); clearInterval(signalTimer); };
  }, [running]);

  // Milestone flash on NAV peak
  useEffect(() => {
    if (navVal > DATA.navPeak) {
      setMilestoneFlash(true);
      setTimeout(() => setMilestoneFlash(false), 2000);
    }
  }, [navVal]);

  const pad = density === 'compact' ? 12 : 20;
  const gap = density === 'compact' ? 10 : 14;

  const dayChange = navVal - DATA.navOpen;
  const dayPct = (dayChange / DATA.navOpen) * 100;

  return (
    <div style={{ padding: pad, display: 'grid', gap, gridTemplateColumns: 'minmax(0,1fr) 320px', gridTemplateRows: 'minmax(260px, auto) minmax(300px, 1fr) auto', height: '100%', overflow: 'auto' }}>
      {/* HERO row — NAV + exposure gauges + key totals */}
      <Card style={{ gridColumn: '1 / -1', padding: '22px 26px', position: 'relative' }}>
        {milestoneFlash && <div style={{ position: 'absolute', inset: 0, background: `radial-gradient(ellipse at top, ${accentGlow}, transparent 70%)`, opacity: 0.8, pointerEvents: 'none', animation: 'fadeOutSlow 2s ease' }} />}
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 32, flexWrap: 'wrap' }}>
          <div>
            <Label accent={TOKENS.ink3} style={{ marginBottom: 8 }}>Net asset value</Label>
            <NavNumber value={navVal} accent={accentColor} size={density === 'compact' ? 54 : 68} />
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginTop: 10 }}>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 13, color: dayChange >= 0 ? TOKENS.profit : TOKENS.loss, fontVariantNumeric: 'tabular-nums' }}>
                {dayChange >= 0 ? '+' : '−'}£{Math.abs(dayChange).toFixed(2)}
              </span>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3, fontVariantNumeric: 'tabular-nums' }}>
                {dayPct >= 0 ? '+' : ''}{dayPct.toFixed(2)}% today
              </span>
            </div>
          </div>

          <div style={{ flex: 1 }}/>

          <div style={{ display: 'flex', gap: 28 }}>
            <MiniStat label="Week"  value={DATA.pnl.w} accent={accentColor}/>
            <MiniStat label="Month" value={DATA.pnl.m} accent={accentColor}/>
            <MiniStat label="YTD"   value={DATA.pnl.y} accent={accentColor}/>
          </div>

          <div style={{ width: 1, height: 48, background: TOKENS.line }}/>

          <ExposureRing gross={DATA.exposure.gross} net={DATA.exposure.net} accent={accentColor}/>
        </div>

        {/* Equity curve as a full-width subtle backdrop/sparkline at bottom */}
        <div style={{ marginTop: 18 }}>
          <EquityCurve values={DATA.equity} accent={accentColor} width={900} height={48}/>
        </div>
      </Card>

      {/* LEFT — conviction river */}
      <Card style={{ minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label>Conviction river</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            9 tracked · top {DATA.conviction[0].sym}
          </span>
        </div>
        <ConvictionRiver conviction={DATA.conviction} accent={accentColor} newSignal={newSignal} running={running}/>
      </Card>

      {/* RIGHT — live feed + risk */}
      <Card style={{ minHeight: 0, display: 'flex', flexDirection: 'column', padding: 0 }}>
        <div style={{ padding: '14px 16px 10px', borderBottom: `1px solid ${TOKENS.line}` }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <Label>Live feed</Label>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
              <Glyph state={state} accent={accentColor} size={8}/>
              {running ? 'streaming' : 'idle'}
            </span>
          </div>
        </div>
        <LiveFeed events={events} accent={accentColor} running={running}/>
      </Card>

      {/* BOTTOM — book strip */}
      <Card style={{ gridColumn: '1 / -1', padding: '14px 18px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, overflowX: 'auto' }}>
          <Label style={{ flexShrink: 0 }}>Book</Label>
          {DATA.positions.map(p => (
            <PositionChip key={p.sym} pos={p} accent={accentColor}/>
          ))}
          <div style={{ flex: 1 }}/>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3, flexShrink: 0 }}>
            {DATA.positions.length} positions · tradable £68,900
          </span>
        </div>
      </Card>
    </div>
  );
}

function MiniStat({ label, value, accent }) {
  const pos = value >= 0;
  return (
    <div>
      <Label style={{ marginBottom: 4 }}>{label}</Label>
      <div style={{ fontFamily: TOKENS.sans, fontSize: 20, fontWeight: 300, color: pos ? TOKENS.profit : TOKENS.loss, fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.02em' }}>
        {pos ? '+' : '−'}£{Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}
      </div>
    </div>
  );
}

function ExposureRing({ gross, net, accent }) {
  const r = 22, c = 2 * Math.PI * r;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
      <svg width="60" height="60" viewBox="0 0 60 60">
        <circle cx="30" cy="30" r={r} fill="none" stroke={TOKENS.line} strokeWidth="3"/>
        <circle cx="30" cy="30" r={r} fill="none" stroke={accent} strokeWidth="3"
          strokeDasharray={`${c * gross} ${c}`} strokeLinecap="round"
          transform="rotate(-90 30 30)" style={{ transition: `stroke-dasharray 600ms ${TOKENS.ease}` }}/>
        <text x="30" y="33" textAnchor="middle" fontSize="11" fontFamily="Geist Mono" fill={TOKENS.ink0} fontWeight="400">{(gross*100).toFixed(0)}</text>
      </svg>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Label>Gross</Label>
        <span style={{ fontFamily: TOKENS.mono, fontSize: 12, color: TOKENS.ink1, fontVariantNumeric: 'tabular-nums' }}>{(gross*100).toFixed(0)}%</span>
        <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, fontVariantNumeric: 'tabular-nums' }}>net {(net*100).toFixed(0)}%</span>
      </div>
    </div>
  );
}

function EquityCurve({ values, accent, width = 900, height = 48 }) {
  const min = Math.min(...values), max = Math.max(...values);
  const rng = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * width;
    const y = height - ((v - min) / rng) * (height - 6) - 3;
    return [x, y];
  });
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0]},${p[1]}`).join(' ');
  const area = line + ` L${width},${height} L0,${height} Z`;
  return (
    <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: 'block' }}>
      <defs>
        <linearGradient id="eq-area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={accent} stopOpacity="0.15"/>
          <stop offset="100%" stopColor={accent} stopOpacity="0"/>
        </linearGradient>
      </defs>
      <path d={area} fill="url(#eq-area)"/>
      <path d={line} stroke={accent} strokeWidth="1.2" fill="none" opacity="0.85"/>
      <circle cx={pts[pts.length-1][0]} cy={pts[pts.length-1][1]} r="3" fill={accent}>
        <animate attributeName="r" from="3" to="7" dur="1.8s" repeatCount="indefinite"/>
        <animate attributeName="opacity" from="1" to="0" dur="1.8s" repeatCount="indefinite"/>
      </circle>
      <circle cx={pts[pts.length-1][0]} cy={pts[pts.length-1][1]} r="2.5" fill={accent}/>
    </svg>
  );
}

// ConvictionRiver — horizontal flow of symbols where X = score, Y stratified by side
function ConvictionRiver({ conviction, accent, newSignal, running }) {
  const [flashId, setFlashId] = useState(null);
  useEffect(() => {
    if (newSignal) {
      setFlashId(newSignal.sym + newSignal.t);
      setTimeout(() => setFlashId(null), 2000);
    }
  }, [newSignal]);

  const sorted = [...conviction].sort((a, b) => b.score - a.score);
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 2, minHeight: 0, overflowY: 'auto' }}>
      {sorted.map((c, i) => {
        const pct = c.score * 100;
        const isFresh = flashId === (c.sym + (newSignal?.t || 0));
        const barColor = c.side === 'short' ? TOKENS.loss : accent;
        return (
          <div key={c.sym} style={{
            position: 'relative', display: 'flex', alignItems: 'center',
            padding: '10px 4px', borderBottom: `1px solid ${TOKENS.line}`,
            animation: `slideIn 320ms ${TOKENS.ease} ${i * 0.03}s both`,
          }}>
            {isFresh && (
              <div style={{
                position: 'absolute', left: -16, top: 0, bottom: 0, width: 3,
                background: accent, animation: 'flashBar 2s ease', borderRadius: 2,
              }}/>
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, width: 130, flexShrink: 0 }}>
              <span style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0, letterSpacing: '-0.02em', width: 48 }}>
                {c.sym}
              </span>
              <Pill size="sm" tone={c.side === 'short' ? 'loss' : 'neutral'}>
                {c.side}
              </Pill>
            </div>
            <div style={{ flex: 1, height: 6, background: 'rgba(255,255,255,0.04)', borderRadius: 3, overflow: 'hidden', position: 'relative' }}>
              <div style={{
                position: 'absolute', left: 0, top: 0, bottom: 0,
                width: `${pct}%`, background: `linear-gradient(90deg, ${barColor}40, ${barColor})`,
                borderRadius: 3,
                transition: `width 600ms ${TOKENS.ease}`,
              }}/>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: 140, flexShrink: 0, justifyContent: 'flex-end' }}>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{c.strat}</span>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 13, color: c.side === 'short' ? TOKENS.loss : accent, fontVariantNumeric: 'tabular-nums', width: 40, textAlign: 'right' }}>
                {c.score.toFixed(2)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function LiveFeed({ events, accent, running }) {
  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '8px 16px' }}>
      {events.map((e, i) => (
        <div key={i} style={{
          padding: '8px 0', borderBottom: `1px solid ${TOKENS.line}`,
          fontFamily: TOKENS.mono, fontSize: 11,
          animation: i === 0 ? `slideIn 320ms ${TOKENS.ease}` : 'none',
          opacity: Math.max(0.4, 1 - i * 0.06),
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{
              width: 5, height: 5, borderRadius: 999,
              background: e.ok === true ? accent : e.ok === false ? TOKENS.danger : TOKENS.ink3,
              flexShrink: 0,
              boxShadow: e.ok === true && i === 0 ? `0 0 6px ${accent}` : 'none',
            }}/>
            <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', letterSpacing: '0.08em', fontSize: 9, width: 42, flexShrink: 0 }}>
              {e.kind}
            </span>
            <span style={{ color: e.ok === false ? TOKENS.loss : TOKENS.ink1, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {e.text}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

function PositionChip({ pos, accent }) {
  const pos_pnl = pos.pnl >= 0;
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 10, flexShrink: 0,
      padding: '8px 12px', borderRadius: 10,
      background: TOKENS.bg2, border: `1px solid ${TOKENS.line}`,
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
        <span style={{ fontFamily: TOKENS.sans, fontSize: 12, fontWeight: 500, color: TOKENS.ink0, letterSpacing: '-0.02em' }}>{pos.sym}</span>
        <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, fontVariantNumeric: 'tabular-nums' }}>{pos.qty}</span>
      </div>
      <Spark values={[pos.avg, pos.avg * 1.01, pos.avg * 0.995, pos.last]} width={32} height={18} accent={accent}/>
      <Signed value={pos.pnl} size={12}/>
    </div>
  );
}

Object.assign(window, { DashboardScreen });
