/**
 * Universe Intelligence — dashboard tab (prototype: ui/newui/project/prototypes/universe).
 * Live data from GET /intelligence/universe.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  api,
  type IntelligenceUniverseResponse,
  type UniverseSymbolRow,
} from '../../lib/api';
import { Card, Label, Spark } from '../primitives';
import { ACCENTS, Density, TOKENS, type AccentName } from '../tokens';
import type { LiveData } from '../useLiveSystem';

const STAGE_COLORS: Record<string, string> = {
  source: '#9ca3af',
  eligible: '#93c5fd',
  watching: '#a5b4fc',
  promoted: '#fcd34d',
  active: '#5eead4',
  banned: '#f87171',
};

const STAGE_LABELS: Record<string, string> = {
  source: 'source pool',
  eligible: 'eligible',
  watching: 'watching',
  promoted: 'promoted',
  active: 'active',
  banned: 'banned',
};

const STAGE_DESC: Record<string, string> = {
  source: 'Broker catalogue and configured monitors.',
  eligible: 'Passes liquidity, spread, and data filters.',
  watching: 'Dynamic tier — core + scan from universe_tiers.json.',
  promoted: 'Recent anomaly or promotion signals.',
  active: 'Symbols with elevated engine attention.',
  banned: 'Excluded or blocked.',
};

type UniTab = 'overview' | 'funnel' | 'instruments' | 'config';

function symbolTitle(row: UniverseSymbolRow): string {
  return (row.description || row.name || row.sym).trim();
}

function symbolSubtitle(row: UniverseSymbolRow): string {
  const fallback =
    row.sector && row.sector !== 'general'
      ? `${row.klass} · ${row.sector.replace(/_/g, ' ')}`
      : row.klass;
  return (row.name || row.description || fallback).trim();
}

function fmtNum(n: number): string {
  return n.toLocaleString();
}

function buildState(data: IntelligenceUniverseResponse | null): string {
  const raw = data?.build?.state;
  return typeof raw === 'string' && raw ? raw : data?.fallback ? 'fallback' : 'unknown';
}

function classGlyph(klass: string, size = 12) {
  const c =
    klass === 'crypto' ? TOKENS.profit :
    klass === 'fx' ? TOKENS.info :
    klass === 'etf' ? TOKENS.caution :
    TOKENS.ink2;
  return (
    <span style={{
      width: size,
      height: size,
      borderRadius: 3,
      background: `${c}44`,
      border: `1px solid ${c}88`,
      display: 'inline-block',
    }} />
  );
}

export function UniverseScreen({
  accent,
  density,
  live,
}: {
  accent: AccentName;
  density: Density;
  live: LiveData;
}) {
  const accentColor = ACCENTS[accent].main;
  const pad = density === 'compact' ? 12 : 20;
  const [tab, setTab] = useState<UniTab>('overview');
  const [stageFilter, setStageFilter] = useState<string | null>(null);
  const [data, setData] = useState<IntelligenceUniverseResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setErr(null);
      const r = await api.getIntelligenceUniverse();
      setData(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelected(null);
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  const funnel = data?.funnel ?? [];
  const symbols = data?.symbols ?? [];
  const stream = data?.stream ?? [];
  const clusters = data?.clusters ?? [];

  const heroLine = useMemo(() => {
    const src = funnel.find((f) => f.stage === 'source');
    const watch = funnel.find((f) => f.stage === 'watching');
    const act = funnel.find((f) => f.stage === 'active');
    return `${fmtNum(src?.count ?? 0)} sourced · ${fmtNum(watch?.count ?? 0)} watched · ${fmtNum(act?.count ?? 0)} active`;
  }, [funnel]);

  const onJumpTo = (t: UniTab, stage?: string | null) => {
    setTab(t);
    setStageFilter(stage ?? null);
  };

  return (
    <div style={{
      height: '100%',
      minHeight: 0,
      boxSizing: 'border-box',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
      background: `radial-gradient(ellipse at top left, ${accentColor}08, transparent 50%), ${TOKENS.bg0}`,
      color: TOKENS.ink1,
    }}>
      <style>{`
        @keyframes uni-flow { to { stroke-dashoffset: -12; } }
      `}</style>

      <header style={{
        display: 'flex',
        flexShrink: 0,
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: `${pad}px ${pad + 8}px`,
        borderBottom: `1px solid ${TOKENS.line}`,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{
            width: 8, height: 8, borderRadius: 999, background: accentColor,
            boxShadow: `0 0 10px ${accentColor}88`,
          }} />
          <span style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>
            mytbot
          </span>
          <span style={{ fontFamily: TOKENS.sans, fontSize: 13, color: TOKENS.ink3 }}>/ universe</span>
        </div>
        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          loop #{live.loopIteration || 0} · {live.path || '—'} · {live.wsConnected ? 'ws' : 'ws·off'}
          {data && (
            <>
              {' · '}
              <span style={{ color: data.enabled ? TOKENS.profit : TOKENS.ink3 }}>
                {data.enabled ? 'intelligence on' : 'pipeline fallback'}
              </span>
            </>
          )}
        </div>
      </header>

      <nav style={{
        flexShrink: 0,
        padding: `0 ${pad + 8}px`,
        borderBottom: `1px solid ${TOKENS.line}`,
        display: 'flex',
        gap: 0,
        flexWrap: 'wrap',
      }}>
        {([
          ['overview', 'Overview', 'River at a glance'],
          ['funnel', 'Funnel', 'Why symbols drop'],
          ['instruments', 'Instruments', 'Grid · list'],
          ['config', 'Config', 'Read-only rules'],
        ] as const).map(([k, label, desc]) => (
          <button
            key={k}
            type="button"
            onClick={() => { setTab(k); setStageFilter(null); }}
            style={{
              background: 'transparent',
              border: 'none',
              padding: '14px 16px',
              cursor: 'pointer',
              color: tab === k ? TOKENS.ink0 : TOKENS.ink2,
              fontFamily: TOKENS.sans,
              fontSize: 13,
              fontWeight: tab === k ? 500 : 400,
              position: 'relative',
            }}
          >
            {label}
            <span style={{ marginLeft: 6, fontSize: 11, color: TOKENS.ink3 }}>· {desc}</span>
            {tab === k && (
              <span style={{
                position: 'absolute', bottom: -1, left: 14, right: 14, height: 2,
                background: accentColor, borderRadius: 1,
              }} />
            )}
          </button>
        ))}
      </nav>

      <main style={{
        flex: 1,
        minHeight: 0,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        padding: `${pad}px ${pad + 8}px 0`,
        maxWidth: 1440,
        margin: '0 auto',
        width: '100%',
        boxSizing: 'border-box',
      }}
      >
        {err && (
          <div style={{
            flexShrink: 0,
            marginBottom: 14, padding: 12, borderRadius: 8,
            border: `1px solid ${TOKENS.danger}55`, color: TOKENS.danger,
            fontFamily: TOKENS.mono, fontSize: 12,
          }}>
            {err}
            <button
              type="button"
              onClick={() => void load()}
              style={{
                marginLeft: 12, padding: '4px 10px', borderRadius: 6,
                border: `1px solid ${TOKENS.line}`, background: TOKENS.bg2, color: TOKENS.ink1,
                cursor: 'pointer', fontFamily: TOKENS.sans, fontSize: 11,
              }}
            >
              Retry
            </button>
          </div>
        )}

        {tab === 'instruments' ? (
          <InstrumentsTab
            symbols={symbols}
            accentColor={accentColor}
            onSelect={setSelected}
            initialStage={stageFilter}
            bottomPad={pad}
          />
        ) : (
          <div style={{
            flex: 1,
            minHeight: 0,
            overflow: 'auto',
            paddingBottom: pad + 8,
          }}
          >
            {tab === 'overview' && (
              <OverviewTab
                accentColor={accentColor}
                heroLine={heroLine}
                funnel={funnel}
                stream={stream}
                symbols={symbols}
                clusters={clusters}
                data={data}
                onSelect={setSelected}
                onJumpTo={onJumpTo}
              />
            )}
            {tab === 'funnel' && (
              <FunnelTab accentColor={accentColor} funnel={funnel} clusters={clusters} onJumpTo={onJumpTo} />
            )}
            {tab === 'config' && <ConfigTab data={data} />}
          </div>
        )}
      </main>

      {selected && (
        <Inspector
          sym={selected}
          symbols={symbols}
          clusters={clusters}
          onClose={() => setSelected(null)}
          accentColor={accentColor}
        />
      )}
    </div>
  );
}

function OverviewTab({
  accentColor, heroLine, funnel, stream, symbols, clusters, data, onSelect, onJumpTo,
}: {
  accentColor: string;
  heroLine: string;
  funnel: IntelligenceUniverseResponse['funnel'];
  stream: IntelligenceUniverseResponse['stream'];
  symbols: UniverseSymbolRow[];
  clusters: IntelligenceUniverseResponse['clusters'];
  data: IntelligenceUniverseResponse | null;
  onSelect: (s: string) => void;
  onJumpTo: (t: UniTab, stage?: string | null) => void;
}) {
  const total = funnel[0]?.count ?? 1;
  const banned = funnel.find((f) => f.stage === 'banned');
  const state = buildState(data);
  const statusColor =
    state === 'fresh' && !data?.fallback ? accentColor :
    state === 'stale' ? TOKENS.caution :
    TOKENS.danger;
  const statusLabel = data?.fallback ? 'fallback' : state;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <Card style={{ padding: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 18, flexWrap: 'wrap', gap: 12 }}>
          <div>
            <Label accent={TOKENS.ink3}>discovery funnel · live</Label>
            <h2 style={{
              margin: '6px 0 0',
              fontFamily: TOKENS.sans,
              fontSize: 26,
              fontWeight: 300,
              letterSpacing: 0,
              color: TOKENS.ink0,
            }}>
              {heroLine}
            </h2>
            <div style={{ marginTop: 6, fontFamily: TOKENS.sans, fontSize: 13, color: TOKENS.ink2, maxWidth: 560, lineHeight: 1.5 }}>
              Non-redundant representatives and cold-scan members. Correlation clusters: {clusters.length}.
              {data?.fallback && ` (${data.fallback})`}
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            <div style={{
              fontFamily: TOKENS.mono,
              fontSize: 10,
              color: statusColor,
              padding: '6px 10px',
              border: `1px solid ${statusColor}66`,
              borderRadius: 8,
              height: 'fit-content',
            }}>
              {statusLabel}
            </div>
            <div style={{
              fontFamily: TOKENS.mono,
              fontSize: 10,
              color: TOKENS.ink3,
              padding: '6px 10px',
              border: `1px solid ${TOKENS.line}`,
              borderRadius: 8,
              height: 'fit-content',
            }}>
              {data?.generated_at?.replace('T', ' ').slice(0, 19) ?? '—'} UTC
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'stretch', gap: 0, overflowX: 'auto', paddingBottom: 8 }}>
          {funnel.filter((f) => f.stage !== 'banned').map((f, i, arr) => {
            const c = STAGE_COLORS[f.stage] ?? TOKENS.ink3;
            const pct = Math.max(0.04, f.count / total);
            const w = Math.max(80, 100 * Math.sqrt(pct));
            const next = arr[i + 1];
            const drop = next ? f.count - next.count : null;
            return (
              <div key={f.stage} style={{ display: 'flex', alignItems: 'stretch' }}>
                <button
                  type="button"
                  onClick={() => onJumpTo('instruments', f.stage)}
                  style={{
                    flex: `1 1 ${w}%`,
                    minWidth: 130,
                    padding: '16px 14px',
                    background: `linear-gradient(180deg, ${c}12, transparent 80%)`,
                    border: `1px solid ${TOKENS.line}`,
                    borderTop: `2px solid ${c}88`,
                    borderRadius: 10,
                    cursor: 'pointer',
                    textAlign: 'left',
                  }}
                >
                  <Label accent={c}>{STAGE_LABELS[f.stage] ?? f.stage}</Label>
                  <div style={{
                    fontFamily: TOKENS.sans,
                    fontSize: 28,
                    fontWeight: 300,
                    color: TOKENS.ink0,
                    marginTop: 6,
                  }}>
                    {fmtNum(f.count)}
                  </div>
                  <div style={{ fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink3, marginTop: 4, minHeight: 28 }}>
                    {(STAGE_DESC[f.stage] ?? '').split('.')[0]}.
                  </div>
                </button>
                {next && (
                  <div style={{
                    flex: '0 0 32px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    position: 'relative',
                  }}
                  >
                    <svg width={32} height={48} style={{ overflow: 'visible' }}>
                      <line
                        x1={0} y1={24} x2={32} y2={24}
                        stroke={TOKENS.lineStrong}
                        strokeWidth={1}
                        strokeDasharray="2 4"
                        style={{ animation: 'uni-flow 2s linear infinite' }}
                      />
                      <polygon points="32,24 26,20 26,28" fill={TOKENS.lineStrong} />
                    </svg>
                    {drop != null && drop > 0 && (
                      <span style={{
                        position: 'absolute',
                        top: -2,
                        fontFamily: TOKENS.mono,
                        fontSize: 9,
                        color: TOKENS.ink3,
                      }}
                      >
                        −{fmtNum(drop)}
                      </span>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {banned && (
          <div style={{
            marginTop: 14,
            paddingTop: 14,
            borderTop: `1px dashed ${TOKENS.line}`,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: 8,
          }}
          >
            <button
              type="button"
              onClick={() => onJumpTo('instruments', 'banned')}
              style={{ background: 'none', border: 'none', cursor: 'pointer', color: TOKENS.ink1, fontFamily: TOKENS.sans, fontSize: 12 }}
            >
              <span style={{ color: STAGE_COLORS.banned }}>●</span>
              {' '}{fmtNum(banned.count)} banned / blocked
            </button>
            <button
              type="button"
              onClick={() => onJumpTo('funnel')}
              style={{
                background: 'transparent',
                border: `1px solid ${TOKENS.line}`,
                borderRadius: 6,
                padding: '5px 10px',
                color: TOKENS.ink2,
                fontFamily: TOKENS.sans,
                fontSize: 11,
                cursor: 'pointer',
              }}
            >
              funnel deep dive →
            </button>
          </div>
        )}
      </Card>

      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 18 }}>
        <Card style={{ padding: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
            <Label accent={TOKENS.ink3}>recent promotions · why now</Label>
            <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{stream.length} events</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 480, overflowY: 'auto' }}>
            {stream.length === 0 && (
              <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.sans, fontSize: 13 }}>No recent promotion stream.</div>
            )}
            {stream.map((p, i) => (
              <button
                key={`${p.sym}-${i}`}
                type="button"
                onClick={() => onSelect(p.sym)}
                style={{
                  display: 'block',
                  textAlign: 'left',
                  width: '100%',
                  padding: 12,
                  borderRadius: 10,
                  background: TOKENS.bg2,
                  border: `1px solid ${TOKENS.line}`,
                  cursor: 'pointer',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {classGlyph(p.klass ?? 'equity', 14)}
                  <div style={{ flex: 1 }}>
                    <div style={{ fontFamily: TOKENS.sans, fontSize: 15, fontWeight: 500, color: TOKENS.ink0 }}>{p.sym}</div>
                    <div style={{ fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink3 }}>{p.why ?? '—'}</div>
                    {p.spark && p.spark.length > 1 && (
                      <div style={{ marginTop: 6 }}>
                        <Spark values={p.spark} width={120} height={22} accent={accentColor} />
                      </div>
                    )}
                  </div>
                  <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.caution }}>
                    ρ{p.bookCorr != null && p.bookCorr >= 0 ? '+' : ''}{p.bookCorr?.toFixed(2) ?? '—'}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </Card>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <Card style={{ padding: 16 }}>
            <Label accent={TOKENS.ink3}>composition</Label>
            <CompositionSummary symbols={symbols} accentColor={accentColor} />
          </Card>
          <Card style={{ padding: 16 }}>
            <Label accent={TOKENS.ink3}>correlation clusters</Label>
            <div style={{ marginTop: 8, maxHeight: 200, overflowY: 'auto', fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink2 }}>
              {clusters.length === 0 && 'Run scripts/build_universe_tiers.py with intelligence enabled for clusters.'}
              {clusters.slice(0, 12).map((cl) => (
                <div key={cl.id} style={{ marginBottom: 6, borderBottom: `1px solid ${TOKENS.line}`, paddingBottom: 6 }}>
                  <span style={{ color: accentColor }}>{cl.representative}</span>
                  {' · '}{cl.member_count} members · avg|ρ| {cl.avg_abs_correlation}
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

function CompositionSummary({ symbols, accentColor }: { symbols: UniverseSymbolRow[]; accentColor: string }) {
  const byClass: Record<string, number> = {};
  symbols.filter((s) => s.stage !== 'banned').forEach((s) => {
    byClass[s.klass] = (byClass[s.klass] || 0) + 1;
  });
  const total = Object.values(byClass).reduce((a, b) => a + b, 0) || 1;
  return (
    <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
      {Object.entries(byClass).sort((a, b) => b[1] - a[1]).map(([k, n]) => {
        const pct = (n / total) * 100;
        return (
          <div key={k}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
              <span style={{ fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink1 }}>{k}</span>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>{n} · {pct.toFixed(0)}%</span>
            </div>
            <div style={{ height: 3, borderRadius: 2, background: TOKENS.bg3 }}>
              <div style={{ height: '100%', width: `${pct}%`, borderRadius: 2, background: accentColor, opacity: 0.6 }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function FunnelTab({
  accentColor,
  funnel,
  clusters,
  onJumpTo,
}: {
  accentColor: string;
  funnel: IntelligenceUniverseResponse['funnel'];
  clusters: IntelligenceUniverseResponse['clusters'];
  onJumpTo: (t: UniTab, stage?: string | null) => void;
}) {
  const stages = funnel.filter((f) => f.stage !== 'banned');
  const total = stages[0]?.count || 1;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <Card style={{ padding: 24 }}>
        <Label accent={TOKENS.ink3}>funnel · why symbols drop</Label>
        <h2 style={{ margin: '8px 0 0', fontFamily: TOKENS.sans, fontSize: 22, fontWeight: 300, color: TOKENS.ink0 }}>
          {fmtNum(stages[0]?.count ?? 0)} → {fmtNum(stages[stages.length - 1]?.count ?? 0)} pipeline
        </h2>
        <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {stages.map((f, i) => {
            const c = STAGE_COLORS[f.stage] ?? accentColor;
            const next = stages[i + 1];
            const drop = next ? f.count - next.count : null;
            const widthPct = Math.max(8, (f.count / total) * 100);
            return (
              <div key={f.stage}>
                <button
                  type="button"
                  onClick={() => onJumpTo('instruments', f.stage)}
                  style={{
                    width: '100%',
                    padding: '14px 16px',
                    background: `linear-gradient(90deg, ${c}14, ${c}05)`,
                    border: `1px solid ${c}55`,
                    borderLeft: `3px solid ${c}`,
                    borderRadius: 8,
                    cursor: 'pointer',
                    textAlign: 'left',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: c, textTransform: 'uppercase' }}>
                        {STAGE_LABELS[f.stage]}
                      </div>
                      <div style={{ fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink3, marginTop: 4 }}>
                        {STAGE_DESC[f.stage]}
                      </div>
                    </div>
                    <div style={{ fontFamily: TOKENS.sans, fontSize: 22, fontWeight: 300 }}>{fmtNum(f.count)}</div>
                  </div>
                  <div style={{ marginTop: 8, height: 6, borderRadius: 3, background: TOKENS.bg3 }}>
                    <div style={{ width: `${widthPct}%`, height: '100%', background: c, opacity: 0.7, borderRadius: 3 }} />
                  </div>
                </button>
                {f.drops && next && drop != null && drop > 0 && (
                  <div style={{ margin: '8px 0 8px 24px', paddingLeft: 14, borderLeft: `1px dashed ${TOKENS.line}` }}>
                    <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>− {fmtNum(drop)} dropped</div>
                    {f.drops.map((d) => (
                      <div key={d.reason} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
                        <span style={{ flex: 1, fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink2 }}>{d.reason}</span>
                        <span style={{ fontFamily: TOKENS.mono, fontSize: 10 }}>{fmtNum(d.count)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      <Card style={{ padding: 16 }}>
        <Label accent={TOKENS.ink3}>clusters (representatives)</Label>
        <div style={{ marginTop: 10, fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, lineHeight: 1.5 }}>
          {clusters.length === 0 && 'No cluster file — enable universe_selection and run build script.'}
          {clusters.map((cl) => (
            <div key={cl.id} style={{ marginBottom: 8 }}>
              <span style={{ color: accentColor }}>{cl.representative}</span>
              : {cl.members.slice(0, 8).join(', ')}{cl.members.length > 8 ? '…' : ''}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

function InstrumentsTab({
  symbols,
  accentColor,
  onSelect,
  initialStage,
  bottomPad,
}: {
  symbols: UniverseSymbolRow[];
  accentColor: string;
  onSelect: (s: string) => void;
  initialStage: string | null;
  bottomPad: number;
}) {
  const [view, setView] = useState<'grid' | 'list'>('grid');
  const [stageFilter, setStageFilter] = useState<string>(initialStage ?? 'all');
  const [q, setQ] = useState('');

  useEffect(() => {
    if (initialStage) setStageFilter(initialStage);
  }, [initialStage]);

  const filtered = useMemo(() => symbols.filter((s) => {
    if (stageFilter !== 'all' && s.stage !== stageFilter) return false;
    if (q) {
      const needle = q.toLowerCase();
      const hay = `${s.sym} ${s.name ?? ''} ${s.description ?? ''}`.toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  }), [symbols, stageFilter, q]);

  const stageCounts = useMemo(() => {
    const c: Record<string, number> = { all: symbols.length };
    symbols.forEach((s) => { c[s.stage] = (c[s.stage] || 0) + 1; });
    return c;
  }, [symbols]);

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      gap: 12,
      flex: 1,
      minHeight: 0,
      overflow: 'hidden',
    }}
    >
      <div style={{ flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Card style={{ padding: 14 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
            {(['all', 'source', 'eligible', 'watching', 'promoted', 'active', 'banned'] as const).map((st) => (
              <button
                key={st}
                type="button"
                onClick={() => setStageFilter(st)}
                style={{
                  padding: '6px 10px',
                  borderRadius: 6,
                  border: `1px solid ${stageFilter === st ? TOKENS.lineStrong : TOKENS.line}`,
                  background: stageFilter === st ? TOKENS.bg3 : 'transparent',
                  color: stageFilter === st ? TOKENS.ink0 : TOKENS.ink2,
                  fontFamily: TOKENS.mono,
                  fontSize: 10,
                  cursor: 'pointer',
                }}
              >
                {st} ({stageCounts[st] ?? (st === 'all' ? symbols.length : 0)})
              </button>
            ))}
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="search…"
              style={{
                marginLeft: 'auto',
                padding: '6px 10px',
                borderRadius: 6,
                background: TOKENS.bg2,
                border: `1px solid ${TOKENS.line}`,
                color: TOKENS.ink1,
                fontFamily: TOKENS.mono,
                fontSize: 11,
                width: 160,
              }}
            />
            <div style={{ display: 'flex', borderRadius: 6, border: `1px solid ${TOKENS.line}`, overflow: 'hidden' }}>
              {(['grid', 'list'] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setView(v)}
                  style={{
                    padding: '6px 10px',
                    background: view === v ? TOKENS.bg3 : 'transparent',
                    border: 'none',
                    color: view === v ? TOKENS.ink0 : TOKENS.ink2,
                    fontFamily: TOKENS.sans,
                    fontSize: 11,
                    cursor: 'pointer',
                  }}
                >
                  {v}
                </button>
              ))}
            </div>
          </div>
        </Card>

        <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
          {filtered.length} symbols
        </div>
      </div>

      <div style={{
        flex: 1,
        minHeight: 0,
        overflow: 'auto',
        paddingBottom: bottomPad + 8,
      }}
      >
        {view === 'grid' ? (
          <Card style={{ padding: 14 }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: 8 }}>
              {filtered.slice(0, 240).map((s) => {
                const c = STAGE_COLORS[s.stage] ?? accentColor;
                return (
                  <button
                    key={s.sym}
                    type="button"
                    title={symbolTitle(s)}
                    onClick={() => onSelect(s.sym)}
                    style={{
                      padding: 10,
                      borderRadius: 8,
                      background: TOKENS.bg2,
                      border: `1px solid ${TOKENS.line}`,
                      borderLeft: `2px solid ${c}`,
                      textAlign: 'left',
                      cursor: 'pointer',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      {classGlyph(s.klass, 11)}
                      <span style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500 }}>{s.sym}</span>
                    </div>
                    <div style={{
                      marginTop: 4,
                      fontFamily: TOKENS.sans,
                      fontSize: 10,
                      color: TOKENS.ink3,
                      lineHeight: 1.25,
                      overflow: 'hidden',
                      display: '-webkit-box',
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: 'vertical',
                      wordBreak: 'break-word',
                    }}
                    >
                      {symbolSubtitle(s)}
                    </div>
                    <div style={{ fontFamily: TOKENS.sans, fontSize: 18, fontWeight: 300, marginTop: 4 }}>{s.conviction}</div>
                    {s.spark && <Spark values={s.spark} width={100} height={18} accent={c} />}
                  </button>
                );
              })}
            </div>
          </Card>
        ) : (
          <Card style={{ padding: 0 }}>
            <div style={{ overflow: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead style={{ position: 'sticky', top: 0, background: TOKENS.bg1, zIndex: 1 }}>
                  <tr style={{ fontFamily: TOKENS.sans, fontSize: 10, color: TOKENS.ink3, textAlign: 'left' }}>
                    <th style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>symbol</th>
                    <th style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>what</th>
                    <th style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>stage</th>
                    <th style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>conv</th>
                    <th style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>ρ</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.slice(0, 400).map((s) => (
                    <tr
                      key={s.sym}
                      title={symbolTitle(s)}
                      onClick={() => onSelect(s.sym)}
                      style={{ cursor: 'pointer', fontFamily: TOKENS.sans, fontSize: 12 }}
                    >
                      <td style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>{s.sym}</td>
                      <td style={{
                        padding: 8,
                        borderBottom: `1px solid ${TOKENS.line}`,
                        color: TOKENS.ink2,
                        maxWidth: 220,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                      >
                        {symbolSubtitle(s)}
                      </td>
                      <td style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>{s.stage}</td>
                      <td style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>{s.conviction}</td>
                      <td style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>{s.bookCorr?.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}

function ConfigTab({ data }: { data: IntelligenceUniverseResponse | null }) {
  if (!data) {
    return <div style={{ color: TOKENS.ink3 }}>Loading…</div>;
  }
  return (
    <Card style={{ padding: 16 }}>
      <Label accent={TOKENS.ink3}>config mirror (read-only)</Label>
      <pre style={{
        marginTop: 12,
        padding: 12,
        background: TOKENS.bg2,
        borderRadius: 8,
        border: `1px solid ${TOKENS.line}`,
        fontFamily: TOKENS.mono,
        fontSize: 10,
        color: TOKENS.ink2,
        overflow: 'auto',
        maxHeight: 640,
      }}
      >
        {JSON.stringify(data.config_mirror, null, 2)}
      </pre>
    </Card>
  );
}

function Inspector({
  sym,
  symbols,
  clusters,
  onClose,
  accentColor,
}: {
  sym: string;
  symbols: UniverseSymbolRow[];
  clusters: IntelligenceUniverseResponse['clusters'];
  onClose: () => void;
  accentColor: string;
}) {
  const s = symbols.find((x) => x.sym === sym);
  const cluster = clusters.find((c) => c.members.some((m) => m.toUpperCase() === sym.toUpperCase()));
  if (!s) {
    return (
      <div style={{
        position: 'fixed', inset: 0, zIndex: 50, background: 'rgba(0,0,0,0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
      >
        <button type="button" onClick={onClose} style={{ padding: 12 }}>Close</button>
      </div>
    );
  }
  const c = STAGE_COLORS[s.stage] ?? accentColor;
  return (
    <div
      role="presentation"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 50,
        display: 'flex',
        justifyContent: 'flex-end',
        background: 'rgba(0,0,0,0.55)',
        backdropFilter: 'blur(4px)',
      }}
      onClick={onClose}
    >
      <div
        role="dialog"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 480,
          maxWidth: '94vw',
          height: '100vh',
          background: TOKENS.bg1,
          borderLeft: `1px solid ${TOKENS.line}`,
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        <div style={{
          padding: '20px 22px',
          borderBottom: `1px solid ${TOKENS.line}`,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
        }}
        >
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {classGlyph(s.klass, 14)}
              <h2 style={{ margin: 0, fontFamily: TOKENS.sans, fontSize: 24, fontWeight: 300, color: TOKENS.ink0 }}>{s.sym}</h2>
            </div>
            <div style={{ marginTop: 6, fontSize: 12, color: TOKENS.ink3 }}>{s.klass} · {s.stage}</div>
            {(s.description || s.name) && (
              <div style={{
                marginTop: 10,
                fontSize: 13,
                color: TOKENS.ink2,
                lineHeight: 1.45,
                maxWidth: 360,
              }}
              >
                {s.description || s.name}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            style={{
              background: 'transparent',
              border: `1px solid ${TOKENS.line}`,
              color: TOKENS.ink2,
              borderRadius: 6,
              padding: '4px 10px',
              cursor: 'pointer',
              fontFamily: TOKENS.sans,
              fontSize: 11,
            }}
          >
            esc
          </button>
        </div>
        <div style={{ padding: 22, display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div>
            <Label accent={TOKENS.ink3}>conviction</Label>
            <div style={{ fontFamily: TOKENS.sans, fontSize: 40, fontWeight: 200, color: TOKENS.ink0 }}>{s.conviction}</div>
            {s.spark && s.spark.length > 1 && <Spark values={s.spark} width={260} height={40} accent={c} />}
          </div>
          {s.factors && (
            <Card style={{ padding: 14 }}>
              <Label accent={TOKENS.ink3}>factors</Label>
              {Object.entries(s.factors).map(([k, v]) => (
                <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
                  <span style={{ flex: 1, fontSize: 11, color: TOKENS.ink2 }}>{k}</span>
                  <div style={{ width: 80, height: 3, borderRadius: 2, background: TOKENS.bg3 }}>
                    <div style={{ width: `${v}%`, height: '100%', background: c, borderRadius: 2 }} />
                  </div>
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 10, width: 24 }}>{v}</span>
                </div>
              ))}
            </Card>
          )}
          {cluster && (
            <Card style={{ padding: 14 }}>
              <Label accent={TOKENS.ink3}>cluster</Label>
              <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, marginTop: 8, lineHeight: 1.5 }}>
                Representative: <span style={{ color: accentColor }}>{cluster.representative}</span>
                <br />
                Members: {cluster.members.join(', ')}
                <br />
                Pair watch: {s.pairWatch ? 'yes (correlated sleeve)' : 'no'}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
