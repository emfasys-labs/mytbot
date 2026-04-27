// ─── Instruments view — constellation / grid / list ─────────────────

UNI.InstrumentsView = function InstrumentsView({ onSelect, initialStage }) {
  const T = UNI.TOKENS;
  const [view, setView] = useState('constellation');  // constellation | grid | list
  const [stageFilter, setStageFilter] = useState(initialStage || 'all');
  const [classFilter, setClassFilter] = useState('all');
  const [query, setQuery] = useState('');

  useEffect(() => { if (initialStage) setStageFilter(initialStage); }, [initialStage]);

  const filtered = useMemo(() => {
    return UNI.SYMBOLS.filter(s => {
      if (stageFilter !== 'all' && s.stage !== stageFilter) return false;
      if (classFilter !== 'all' && s.klass !== classFilter) return false;
      if (query && !s.sym.toLowerCase().includes(query.toLowerCase())) return false;
      return true;
    });
  }, [stageFilter, classFilter, query]);

  const stageCounts = useMemo(() => {
    const c = { all: UNI.SYMBOLS.length };
    UNI.SYMBOLS.forEach(s => c[s.stage] = (c[s.stage] || 0) + 1);
    return c;
  }, []);

  return <div style={{ display:'flex', flexDirection:'column', gap:14 }}>
    {/* Filter bar */}
    <UNI.Card padding={14}>
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', gap:14, flexWrap:'wrap' }}>
        <div style={{ display:'flex', alignItems:'center', gap:6, flexWrap:'wrap' }}>
          <UNI.StageChip stage={null} count={stageCounts.all} onClick={() => setStageFilter('all')} active={stageFilter === 'all'} style={{ borderColor: stageFilter==='all' ? T.lineStrong : T.line }}>
            <span>all</span>
          </UNI.StageChip>
          {['source','eligible','watching','promoted','active','banned'].map(stg => (
            <UNI.StageChip key={stg} stage={stg} count={stageCounts[stg] || 0}
              onClick={() => setStageFilter(stg)} active={stageFilter === stg}/>
          ))}
        </div>

        <div style={{ display:'flex', alignItems:'center', gap:8 }}>
          <input type="text" placeholder="search symbol…" value={query} onChange={e => setQuery(e.target.value)}
            style={{ padding:'6px 10px', borderRadius:6, background:T.bg2,
              border:`1px solid ${T.line}`, color:T.ink1,
              fontFamily:T.mono, fontSize:11, width:160 }}/>

          <select value={classFilter} onChange={e => setClassFilter(e.target.value)} style={{
            padding:'6px 10px', borderRadius:6, background:T.bg2, color:T.ink1,
            border:`1px solid ${T.line}`, fontFamily:T.sans, fontSize:11,
          }}>
            <option value="all">all classes</option>
            <option value="equity">equity</option>
            <option value="crypto">crypto</option>
            <option value="etf">etf</option>
            <option value="fx">fx</option>
            <option value="bond">bond</option>
          </select>

          <div style={{ display:'flex', borderRadius:6, border:`1px solid ${T.line}`, overflow:'hidden' }}>
            {['constellation','grid','list'].map(v => (
              <button key={v} onClick={() => setView(v)} style={{
                padding:'6px 10px', background: view === v ? T.bg3 : 'transparent',
                border:'none', color: view === v ? T.ink0 : T.ink2,
                fontFamily:T.sans, fontSize:11, cursor:'pointer',
                borderLeft: v === 'grid' || v === 'list' ? `1px solid ${T.line}` : 'none',
              }}>{v}</button>
            ))}
          </div>
        </div>
      </div>
    </UNI.Card>

    <div style={{ fontFamily:T.mono, fontSize:11, color:T.ink3 }}>
      {filtered.length.toLocaleString()} symbols
      {stageFilter !== 'all' && <> · stage <span style={{ color: UNI.STAGE_COLORS[stageFilter] }}>{UNI.STAGE_LABELS[stageFilter]}</span></>}
      {classFilter !== 'all' && <> · class {classFilter}</>}
    </div>

    {filtered.length === 0
      ? <UNI.DataState kind="no-data" message="No symbols match your filters."/>
      : view === 'constellation'
        ? <UNI.Constellation symbols={filtered} onSelect={onSelect}/>
        : view === 'grid'
          ? <UNI.Grid symbols={filtered} onSelect={onSelect}/>
          : <UNI.List symbols={filtered} onSelect={onSelect}/>}
  </div>;
};

// ─── Constellation — 2D layout: x by class, y by conviction, size by liquidity
UNI.Constellation = function Constellation({ symbols, onSelect }) {
  const T = UNI.TOKENS;
  const W = 1100, H = 540;
  const PAD = 50;
  const classes = ['equity','etf','fx','bond','crypto'];
  const colW = (W - PAD * 2) / classes.length;
  const lo = Math.min(...symbols.map(s => s.factors.liquidity)) || 0;
  const hi = Math.max(...symbols.map(s => s.factors.liquidity)) || 100;
  const sizeFor = (l) => 4 + ((l - lo) / Math.max(1, hi - lo)) * 8;

  // jitter so symbols within a class spread horizontally
  const jit = (s, i) => {
    const idx = classes.indexOf(s.klass);
    let h = (s.sym.charCodeAt(0) || 0) + (s.sym.charCodeAt(1) || 0) + i;
    const x = PAD + idx * colW + colW * 0.5 + ((h * 9301 + 49297) % colW * 0.7) - colW * 0.35;
    const y = PAD + (1 - s.conviction / 100) * (H - PAD * 2);
    return [x, y];
  };

  const [hover, setHover] = useState(null);

  return <UNI.Card padding={0}>
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width:'100%', height:560, display:'block' }}>
      {/* Y-axis: conviction */}
      {[0, 25, 50, 75, 100].map(c => {
        const y = PAD + (1 - c/100) * (H - PAD * 2);
        return <g key={c}>
          <line x1={PAD - 8} y1={y} x2={W - PAD} y2={y} stroke={T.line} strokeDasharray="2 4"/>
          <text x={PAD - 12} y={y + 3} fill={T.ink3} fontSize="9" fontFamily={T.mono} textAnchor="end">{c}</text>
        </g>;
      })}
      <text x={10} y={H/2} fill={T.ink3} fontSize="9" fontFamily={T.sans} textAnchor="middle"
        transform={`rotate(-90 10 ${H/2})`} style={{ textTransform:'uppercase', letterSpacing:'0.14em' }}>
        conviction
      </text>

      {/* X-axis: classes */}
      {classes.map((c, i) => {
        const x = PAD + i * colW + colW / 2;
        return <g key={c}>
          <line x1={x} y1={H - PAD} x2={x} y2={H - PAD + 6} stroke={T.line}/>
          <text x={x} y={H - PAD + 18} fill={T.ink3} fontSize="9" fontFamily={T.sans} textAnchor="middle"
            style={{ textTransform:'uppercase', letterSpacing:'0.14em' }}>{c}</text>
        </g>;
      })}

      {/* Promotion line */}
      {(() => {
        const y = PAD + (1 - 65/100) * (H - PAD * 2);
        return <g>
          <line x1={PAD} y1={y} x2={W - PAD} y2={y} stroke={UNI.TOKENS.caution} strokeWidth="1" strokeDasharray="4 4" opacity={0.5}/>
          <text x={W - PAD - 8} y={y - 4} fill={UNI.TOKENS.caution} fontSize="9" fontFamily={T.mono} textAnchor="end">promotion threshold · 65</text>
        </g>;
      })()}

      {/* Stars */}
      {symbols.map((s, i) => {
        const [x, y] = jit(s, i);
        const r = sizeFor(s.factors.liquidity);
        const c = UNI.STAGE_COLORS[s.stage];
        const isPin = s.override?.kind === 'pin-core';
        const isExcl = s.override?.kind === 'manual-exclude';
        return <g key={s.sym + i} style={{ cursor:'pointer' }}
          onMouseEnter={() => setHover({ s, x, y })} onMouseLeave={() => setHover(null)}
          onClick={() => onSelect(s.sym)}>
          {isPin && <circle cx={x} cy={y} r={r + 4} fill="none" stroke={UNI.TOKENS.accent} strokeWidth="1.2" opacity="0.7"/>}
          <circle cx={x} cy={y} r={r + 2} fill={c} opacity="0.18"/>
          <circle cx={x} cy={y} r={r} fill={c} style={{ animation: s.stage === 'promoted' ? 'uni-twinkle 2.4s ease-in-out infinite' : 'none' }}/>
          {isExcl && <line x1={x - r - 1} y1={y - r - 1} x2={x + r + 1} y2={y + r + 1} stroke={UNI.TOKENS.danger} strokeWidth="1.5"/>}
        </g>;
      })}

      {/* Hover tooltip */}
      {hover && <g style={{ pointerEvents:'none' }}>
        <rect x={hover.x + 12} y={hover.y - 30} width={120} height={56}
          fill={T.bg2} stroke={T.lineStrong} rx="6"/>
        <text x={hover.x + 22} y={hover.y - 14} fill={T.ink0} fontSize="12" fontFamily={T.sans} fontWeight="500">{hover.s.sym}</text>
        <text x={hover.x + 22} y={hover.y + 0} fill={T.ink2} fontSize="9" fontFamily={T.mono}>conv {hover.s.conviction} · {UNI.STAGE_LABELS[hover.s.stage]}</text>
        <text x={hover.x + 22} y={hover.y + 14} fill={T.ink3} fontSize="9" fontFamily={T.mono}>liq {hover.s.factors.liquidity}</text>
      </g>}
    </svg>

    <div style={{ padding:'12px 18px', borderTop:`1px solid ${T.line}`,
      display:'flex', alignItems:'center', gap:14, fontFamily:T.sans, fontSize:11, color:T.ink3 }}>
      <span style={{ display:'flex', alignItems:'center', gap:5 }}>
        <span style={{ width:5, height:5, borderRadius:999, background:UNI.STAGE_COLORS.watching }}/>watching
      </span>
      <span style={{ display:'flex', alignItems:'center', gap:5 }}>
        <span style={{ width:6, height:6, borderRadius:999, background:UNI.STAGE_COLORS.promoted, animation:'uni-twinkle 2.4s ease-in-out infinite' }}/>promoted (twinkles)
      </span>
      <span style={{ display:'flex', alignItems:'center', gap:5 }}>
        <span style={{ width:7, height:7, borderRadius:999, background:UNI.STAGE_COLORS.active }}/>active
      </span>
      <span style={{ display:'flex', alignItems:'center', gap:5 }}>
        <span style={{ width:7, height:7, borderRadius:'50%', border:`1px solid ${UNI.TOKENS.accent}`, background:'transparent' }}/>pinned
      </span>
      <span style={{ marginLeft:'auto', color:T.ink4, fontFamily:T.mono, fontSize:10 }}>
        x = asset class · y = conviction · size = liquidity
      </span>
    </div>
  </UNI.Card>;
};

// ─── Grid view — dense, sortable, score-coloured cells
UNI.Grid = function Grid({ symbols, onSelect }) {
  const T = UNI.TOKENS;
  return <UNI.Card padding={14}>
    <div style={{ display:'grid', gridTemplateColumns:'repeat(auto-fill, minmax(140px, 1fr))', gap:8 }}>
      {symbols.slice(0, 240).map((s, i) => {
        const c = UNI.STAGE_COLORS[s.stage];
        return <button key={s.sym + i} onClick={() => onSelect(s.sym)} style={{
          padding:10, borderRadius:8, background: T.bg2,
          border: `1px solid ${T.line}`, borderLeft: `2px solid ${c}`,
          textAlign:'left', cursor:'pointer', position:'relative',
          display:'flex', flexDirection:'column', gap:6,
        }}
        onMouseEnter={e => e.currentTarget.style.borderColor = T.lineStrong}
        onMouseLeave={e => e.currentTarget.style.borderColor = T.line}>
          <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
            <div style={{ display:'flex', alignItems:'center', gap:6 }}>
              <UNI.ClassGlyph klass={s.klass} size={11}/>
              <span style={{ fontFamily:T.sans, fontSize:13, fontWeight:500, color:T.ink0 }}>{s.sym}</span>
            </div>
            {s.override && <UNI.OverrideBadge kind={s.override.kind} compact/>}
          </div>
          <div style={{ display:'flex', alignItems:'baseline', gap:6 }}>
            <span style={{ fontFamily:T.sans, fontSize:18, fontWeight:300, color:T.ink0, letterSpacing:'-0.02em' }}>{s.conviction}</span>
            <UNI.Trend trend={s.trend} size={9}/>
            <UNI.Spark data={s.spark} w={48} h={14} tone={c}/>
          </div>
          <div style={{ fontFamily:T.mono, fontSize:9, color:T.ink3 }}>
            liq {s.factors.liquidity} · spr {s.spread}bp
          </div>
        </button>;
      })}
    </div>
    {symbols.length > 240 && <div style={{ marginTop:14, textAlign:'center', fontFamily:T.mono, fontSize:11, color:T.ink3 }}>
      Showing first 240 of {symbols.length}. Refine filters to narrow.
    </div>}
  </UNI.Card>;
};

// ─── List view — high-density table with sortable columns
UNI.List = function List({ symbols, onSelect }) {
  const T = UNI.TOKENS;
  const [sort, setSort] = useState({ key:'conviction', dir:'desc' });

  const sorted = useMemo(() => {
    const arr = [...symbols];
    arr.sort((a, b) => {
      const av = sort.key === 'sym' ? a.sym
               : sort.key === 'conviction' ? a.conviction
               : sort.key === 'liquidity' ? a.factors.liquidity
               : sort.key === 'spread' ? a.spread
               : sort.key === 'corr' ? Math.abs(a.bookCorr) : 0;
      const bv = sort.key === 'sym' ? b.sym
               : sort.key === 'conviction' ? b.conviction
               : sort.key === 'liquidity' ? b.factors.liquidity
               : sort.key === 'spread' ? b.spread
               : sort.key === 'corr' ? Math.abs(b.bookCorr) : 0;
      const r = av < bv ? -1 : av > bv ? 1 : 0;
      return sort.dir === 'asc' ? r : -r;
    });
    return arr;
  }, [symbols, sort]);

  const Th = ({ k, label, align = 'left', width }) => (
    <th onClick={() => setSort(s => ({ key:k, dir: s.key === k && s.dir === 'desc' ? 'asc' : 'desc' }))} style={{
      padding:'8px 10px', textAlign:align,
      fontFamily:T.sans, fontSize:10, color:T.ink3,
      textTransform:'uppercase', letterSpacing:'0.1em', fontWeight:500,
      borderBottom:`1px solid ${T.line}`, cursor:'pointer', userSelect:'none', width,
    }}>
      {label}
      {sort.key === k && <span style={{ marginLeft:4, color:UNI.TOKENS.accent }}>{sort.dir === 'desc' ? '↓' : '↑'}</span>}
    </th>
  );

  return <UNI.Card padding={0}>
    <div style={{ overflowX:'auto', maxHeight: 620 }}>
      <table style={{ width:'100%', borderCollapse:'collapse' }}>
        <thead><tr>
          <Th k="sym" label="symbol" width={140}/>
          <Th k="stage" label="stage"/>
          <Th k="conviction" label="conv" align="right"/>
          <Th k="liquidity" label="liq" align="right"/>
          <Th k="spread" label="spread" align="right"/>
          <Th k="corr" label="ρ" align="right"/>
          <th style={{ padding:'8px 10px', textAlign:'left',
            fontFamily:T.sans, fontSize:10, color:T.ink3,
            textTransform:'uppercase', letterSpacing:'0.1em', fontWeight:500,
            borderBottom:`1px solid ${T.line}` }}>spark</th>
        </tr></thead>
        <tbody>
          {sorted.slice(0, 400).map((s, i) => {
            const c = UNI.STAGE_COLORS[s.stage];
            return <tr key={s.sym + i} onClick={() => onSelect(s.sym)} style={{
              cursor:'pointer', transition:`background 100ms`,
            }}
            onMouseEnter={e => e.currentTarget.style.background = T.bg2}
            onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
              <td style={{ padding:'7px 10px', borderBottom:`1px solid ${T.line}` }}>
                <span style={{ display:'flex', alignItems:'center', gap:8 }}>
                  <UNI.ClassGlyph klass={s.klass} size={10}/>
                  <span style={{ fontFamily:T.sans, fontSize:12, fontWeight:500, color:T.ink0 }}>{s.sym}</span>
                  {s.override && <UNI.OverrideBadge kind={s.override.kind} compact/>}
                </span>
              </td>
              <td style={{ padding:'7px 10px', borderBottom:`1px solid ${T.line}` }}>
                <span style={{ display:'inline-flex', alignItems:'center', gap:5, fontFamily:T.sans, fontSize:11, color:T.ink2 }}>
                  <span style={{ width:5, height:5, borderRadius:999, background:c }}/>
                  {UNI.STAGE_LABELS[s.stage]}
                </span>
              </td>
              <td style={{ padding:'7px 10px', textAlign:'right', borderBottom:`1px solid ${T.line}` }}>
                <UNI.Mono size={11} tone={s.conviction >= 65 ? UNI.TOKENS.accent : T.ink1}>{s.conviction}</UNI.Mono>
              </td>
              <td style={{ padding:'7px 10px', textAlign:'right', borderBottom:`1px solid ${T.line}` }}>
                <UNI.Mono size={11} tone={T.ink2}>{s.factors.liquidity}</UNI.Mono>
              </td>
              <td style={{ padding:'7px 10px', textAlign:'right', borderBottom:`1px solid ${T.line}` }}>
                <UNI.Mono size={11} tone={s.spread > 15 ? UNI.TOKENS.caution : T.ink2}>{s.spread.toFixed(1)}bp</UNI.Mono>
              </td>
              <td style={{ padding:'7px 10px', textAlign:'right', borderBottom:`1px solid ${T.line}` }}>
                <UNI.Mono size={11} tone={Math.abs(s.bookCorr) > 0.5 ? UNI.TOKENS.caution : T.ink2}>
                  {s.bookCorr >= 0 ? '+' : ''}{s.bookCorr.toFixed(2)}
                </UNI.Mono>
              </td>
              <td style={{ padding:'7px 10px', borderBottom:`1px solid ${T.line}` }}>
                <UNI.Spark data={s.spark} w={70} h={14} tone={c}/>
              </td>
            </tr>;
          })}
        </tbody>
      </table>
    </div>
    {sorted.length > 400 && <div style={{ padding:14, textAlign:'center', fontFamily:T.mono, fontSize:11, color:T.ink3, borderTop:`1px solid ${T.line}` }}>
      Showing first 400 of {sorted.length}. Refine filters.
    </div>}
  </UNI.Card>;
};
