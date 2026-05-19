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

// D118 — 4-stage funnel; promotions are an overlay on watching, not a stage.
// ``broker_listings`` is a debug tooltip on ``unique_normalized`` only.
const STAGE_COLORS: Record<string, string> = {
  unique_normalized: '#9ca3af',
  priority_ranked: '#7dd3fc', // legacy API payloads only
  scored: '#93c5fd',
  watching: '#a5b4fc',
  promoted: '#fcd34d',
  active_reps: '#5eead4',
  // legacy stages kept for back-compat
  source: '#9ca3af',
  eligible: '#93c5fd',
  active: '#5eead4',
  banned: '#f87171',
};

const STAGE_LABELS: Record<string, string> = {
  unique_normalized: 'unique normalized',
  priority_ranked: 'priority ranked',
  scored: 'scored',
  watching: 'watching',
  promoted: 'promoted now',
  active_reps: 'active reps',
  source: 'broker listings',
  eligible: 'scored',
  active: 'active reps',
  banned: 'banned',
};

const STAGE_DESC: Record<string, string> = {
  unique_normalized: 'Every unique normalized symbol from connected brokers + registry.',
  priority_ranked: 'Legacy stage — same pass as scored.',
  scored: 'Priority top-N picked and yfinance liquidity-scored this cycle (budget N in subtitle).',
  watching: 'Core + scan watchlist; anomaly boosts (promoted now) are a subset overlay, not a separate filter.',
  promoted: 'Temporary conviction boost from scan/light (filter instruments tab only).',
  active_reps: 'Correlation representatives under engine attention.',
  source: 'Raw broker listings plus curated broker seeds.',
  eligible: 'Normalized symbols selected for scoring.',
  active: 'Correlation representatives under engine attention.',
  banned: 'Excluded or blocked.',
};

type UniTab = 'overview' | 'funnel' | 'instruments' | 'coverage' | 'transitions' | 'config';

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

/** D118 funnel stages shown in the UI (drops legacy / non-funnel stages). */
function displayFunnelStages(funnel: IntelligenceUniverseResponse['funnel']) {
  return funnel.filter(
    (f) => f.stage !== 'banned' && f.stage !== 'priority_ranked' && f.stage !== 'promoted',
  );
}

function promotedNowCount(
  funnel: IntelligenceUniverseResponse['funnel'],
  promotions?: IntelligenceUniverseResponse['promotions'],
): number {
  const watch = funnel.find((f) => f.stage === 'watching');
  const meta = (watch as { meta?: { promoted_now?: number } } | undefined)?.meta;
  if (meta?.promoted_now != null) return Number(meta.promoted_now);
  const legacy = funnel.find((f) => f.stage === 'promoted');
  if (legacy) return legacy.count;
  return promotions?.length ?? 0;
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
  // Seed with the prefetched snapshot from useLiveSystem so the tab renders
  // instantly on switch; subsequent refreshes flow through the same setter.
  const [data, setData] = useState<IntelligenceUniverseResponse | null>(
    live.universeIntel ?? null,
  );
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  // Keep local state in sync with the shared prefetched copy so periodic
  // background refreshes in useLiveSystem propagate even while we're idle.
  useEffect(() => {
    if (live.universeIntel) setData(live.universeIntel);
  }, [live.universeIntel]);

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
    // If we already have a warm snapshot from useLiveSystem, skip the
    // immediate refetch — the shared background loop will keep it fresh.
    if (!live.universeIntel) void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
    // D118 headline — always read the new stage ids; legacy ids are
    // fallbacks only when an old API payload is still in cache.
    const unique =
      funnel.find((f) => f.stage === 'unique_normalized') ??
      funnel.find((f) => f.stage === 'source');
    const scored =
      funnel.find((f) => f.stage === 'scored') ??
      funnel.find((f) => f.stage === 'eligible');
    const watch = funnel.find((f) => f.stage === 'watching');
    const promotedN = promotedNowCount(funnel, data?.promotions);
    const act =
      funnel.find((f) => f.stage === 'active_reps') ??
      funnel.find((f) => f.stage === 'active');
    const meta = (unique as { meta?: { broker_listings?: number } } | undefined)?.meta;
    const brokerListings =
      meta?.broker_listings ??
      data?.coverage?.broker_listing_count ??
      0;
    const uniqueCount = unique?.count ?? data?.coverage?.unique_normalized_count ?? 0;
    const scoredMeta = (scored as { meta?: { budget_attempted?: number; target_budget?: number } } | undefined)?.meta;
    const budgetN = scoredMeta?.budget_attempted ?? scoredMeta?.target_budget;
    return [
      `${fmtNum(brokerListings)} listings → ${fmtNum(uniqueCount)} unique`,
      budgetN != null && budgetN !== scored?.count
        ? `${fmtNum(scored?.count ?? 0)} scored (budget ${fmtNum(budgetN)})`
        : `${fmtNum(scored?.count ?? 0)} scored`,
      `${fmtNum(watch?.count ?? 0)} watching`,
      promotedN > 0 ? `${fmtNum(promotedN)} promoted now` : null,
      `${fmtNum(act?.count ?? 0)} active reps`,
    ].filter(Boolean).join(' · ');
  }, [funnel, data?.coverage?.broker_listing_count, data?.coverage?.unique_normalized_count, data?.promotions]);

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
          ['funnel', 'Funnel', 'Self-tuning rule'],
          ['coverage', 'Coverage', 'By asset class'],
          ['transitions', 'Transitions', 'Tier movement'],
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
              <FunnelTab
                accentColor={accentColor}
                funnel={funnel}
                clusters={clusters}
                onJumpTo={onJumpTo}
                priorityRule={data?.priority_rule ?? null}
                promotions={data?.promotions}
              />
            )}
            {tab === 'coverage' && (
              <CoverageTab
                accentColor={accentColor}
                coverage={data?.asset_class_coverage ?? null}
                symbols={symbols}
              />
            )}
            {tab === 'transitions' && (
              <TransitionsTab
                accentColor={accentColor}
                transitions={data?.transitions ?? []}
                onSelect={setSelected}
              />
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
  const promotedN = promotedNowCount(funnel, data?.promotions);
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
            {data?.coverage && (
              <div style={{ marginTop: 8, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.6 }}>
                unique normalized {fmtNum(data.coverage.unique_normalized_count ?? 0)}
                {' · '}candidate cap {fmtNum(data.coverage.caps?.candidates ?? 0)}
                {(() => {
                  const baseCand = data.adaptive?.enabled
                    ? data.adaptive?.resolved?.base?.candidates
                    : undefined;
                  if (baseCand != null && baseCand !== data.coverage?.caps?.candidates) {
                    return ` (base ${fmtNum(baseCand)})`;
                  }
                  return '';
                })()}
                {' · '}watch cap {fmtNum(data.coverage.caps?.watching ?? 0)}
                {(() => {
                  const baseWatch = data.adaptive?.enabled
                    ? data.adaptive?.resolved?.base?.watching
                    : undefined;
                  if (baseWatch != null && baseWatch !== data.coverage?.caps?.watching) {
                    return ` (base ${fmtNum(baseWatch)})`;
                  }
                  return '';
                })()}
                {data.coverage.by_broker?.ibkr && (() => {
                  const ib = data.coverage!.by_broker!.ibkr!;
                  const src = ib.source ?? 'curated_seed';
                  const tag = src === 'curated_seed+registry' ? 'curated+registry' : 'curated';
                  return ` · ibkr ${fmtNum(ib.raw ?? 0)} ${tag}`;
                })()}
                {(() => {
                  const brokers = data.coverage?.by_broker ?? {};
                  let known = 0;
                  let covered = 0;
                  Object.values(brokers).forEach((b) => {
                    known = Math.max(known, b?.registry_known_count ?? 0);
                    covered += b?.registry_covered_count ?? 0;
                  });
                  if (!known) return '';
                  return ` · registry ${fmtNum(known)} known / ${fmtNum(covered)} covered`;
                })()}
              </div>
            )}
            {data?.coverage?.by_broker && (() => {
              const brokers = Object.entries(data.coverage!.by_broker!);
              if (brokers.length === 0) return null;
              const anyRegistry = brokers.some(([, b]) => (b?.registry_known_count ?? 0) > 0);
              if (!anyRegistry) return null;
              return (
                <div
                  style={{
                    marginTop: 10,
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: 6,
                    fontFamily: TOKENS.mono,
                    fontSize: 10,
                    color: TOKENS.ink3,
                  }}
                >
                  {brokers.map(([name, info]) => {
                    const raw = info?.raw ?? 0;
                    const known = info?.registry_known_count ?? 0;
                    const covered = info?.registry_covered_count ?? 0;
                    const accent =
                      known === 0 ? TOKENS.ink3 :
                      covered === 0 ? TOKENS.caution :
                      covered >= known ? TOKENS.profit :
                      TOKENS.info;
                    return (
                      <span
                        key={name}
                        title={info?.note ?? `${name}: ${raw} listed · ${known} in registry · ${covered} resolved as available`}
                        style={{
                          padding: '3px 8px',
                          borderRadius: 999,
                          border: `1px solid ${accent}55`,
                          background: `${accent}10`,
                          color: TOKENS.ink2,
                          whiteSpace: 'nowrap',
                        }}
                      >
                        <span style={{ color: TOKENS.ink1, fontWeight: 500 }}>{name}</span>
                        {' · '}
                        {fmtNum(raw)} listed
                        {' · '}
                        <span style={{ color: accent }}>
                          {fmtNum(covered)}/{fmtNum(known)}
                        </span>
                        {' registry'}
                      </span>
                    );
                  })}
                </div>
              );
            })()}
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
            {data?.adaptive?.enabled && data.adaptive.resolved && (() => {
              const res = data.adaptive.resolved;
              const mult = typeof res.multiplier === 'number' ? res.multiplier : 1;
              const color =
                mult >= 1.10 ? TOKENS.profit :
                mult <= 0.90 ? TOKENS.caution :
                TOKENS.info;
              const ctx = data.adaptive.context ?? {};
              const reasons = res.reasons ?? [];
              const tip = [
                `multiplier ${mult.toFixed(2)}`,
                `regime=${ctx.regime_label ?? 'unknown'}`,
                ctx.signal_pressure != null ? `signal_pressure=${ctx.signal_pressure}` : 'signal_pressure=unknown',
                ctx.active_cluster_count != null ? `clusters=${ctx.active_cluster_count}` : 'clusters=unknown',
                ...reasons,
              ].join(' · ');
              const direction = mult >= 1.05 ? '↑ widen' : mult <= 0.95 ? '↓ focus' : '· neutral';
              return (
                <div
                  title={tip}
                  style={{
                    fontFamily: TOKENS.mono,
                    fontSize: 10,
                    color,
                    padding: '6px 10px',
                    border: `1px solid ${color}66`,
                    background: `${color}10`,
                    borderRadius: 8,
                    height: 'fit-content',
                    cursor: 'help',
                  }}
                >
                  adaptive {mult.toFixed(2)}x {direction}
                </div>
              );
            })()}
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
          {displayFunnelStages(funnel).map((f, i, arr) => {
            const c = STAGE_COLORS[f.stage] ?? accentColor;
            const pct = Math.max(0.04, f.count / total);
            const w = Math.max(80, 100 * Math.sqrt(pct));
            const next = arr[i + 1];
            const drop = next && f.count > next.count ? f.count - next.count : 0;
            const stageMeta = (f as { meta?: Record<string, unknown> }).meta;
            return (
              <div key={f.stage} style={{ display: 'flex', alignItems: 'stretch' }}>
                <button
                  type="button"
                  onClick={() => onJumpTo('instruments', f.stage)}
                  style={{
                    flex: `1 1 ${w}%`,
                    minWidth: 130,
                    padding: '16px 14px',
                    background: `linear-gradient(165deg, ${c}28 0%, ${c}10 45%, transparent 100%)`,
                    border: `1px solid ${c}55`,
                    borderLeft: `3px solid ${c}`,
                    borderTop: `2px solid ${c}`,
                    borderRadius: 10,
                    cursor: 'pointer',
                    textAlign: 'left',
                    boxShadow: `0 4px 24px ${c}18`,
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
                  <div style={{ fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink3, marginTop: 4, minHeight: 36, lineHeight: 1.35 }}>
                    {f.stage === 'unique_normalized' && stageMeta?.broker_listings != null && (
                      <span style={{ display: 'block', color: TOKENS.ink2 }}>
                        dedup from {fmtNum(Number(stageMeta.broker_listings))} listings
                      </span>
                    )}
                    {f.stage === 'scored' && stageMeta && (
                      <span style={{ display: 'block', color: c }}>
                        budget {fmtNum(Number(stageMeta.budget_attempted ?? stageMeta.target_budget ?? f.count))}
                        {stageMeta.binding_constraint != null
                          ? ` · ${String(stageMeta.binding_constraint)}`
                          : ''}
                        {stageMeta.score_failures != null && Number(stageMeta.score_failures) > 0
                          ? ` · ${fmtNum(Number(stageMeta.score_failures))} timeouts`
                          : ''}
                      </span>
                    )}
                    {f.stage === 'watching' && (
                      <span style={{ display: 'block', color: promotedN > 0 ? STAGE_COLORS.promoted : TOKENS.ink3 }}>
                        {promotedN > 0
                          ? `${fmtNum(promotedN)} promoted now (anomaly boost)`
                          : 'no anomaly boosts this cycle'}
                      </span>
                    )}
                    <span>{(STAGE_DESC[f.stage] ?? '').split('.')[0]}.</span>
                  </div>
                </button>
                {next && (
                  <div style={{
                    flex: '0 0 40px',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 4,
                  }}
                  >
                    {drop > 0 && (
                      <span style={{
                        fontFamily: TOKENS.mono,
                        fontSize: 9,
                        color: TOKENS.caution,
                        whiteSpace: 'nowrap',
                      }}
                      >
                        −{fmtNum(drop)}
                      </span>
                    )}
                    <svg width={32} height={32} style={{ overflow: 'visible' }}>
                      <line
                        x1={0} y1={16} x2={32} y2={16}
                        stroke={drop > 0 ? TOKENS.caution : c}
                        strokeWidth={1.5}
                        strokeDasharray="2 4"
                        style={{ animation: 'uni-flow 2s linear infinite' }}
                      />
                      <polygon points="32,16 26,12 26,20" fill={drop > 0 ? TOKENS.caution : c} />
                    </svg>
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
  priorityRule,
  promotions,
}: {
  accentColor: string;
  funnel: IntelligenceUniverseResponse['funnel'];
  clusters: IntelligenceUniverseResponse['clusters'];
  onJumpTo: (t: UniTab, stage?: string | null) => void;
  priorityRule: IntelligenceUniverseResponse['priority_rule'];
  promotions?: IntelligenceUniverseResponse['promotions'];
}) {
  const stages = displayFunnelStages(funnel);
  const total = stages[0]?.count || 1;
  const promotedN = promotedNowCount(funnel, promotions);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <PriorityRuleCard accentColor={accentColor} rule={priorityRule ?? null} />

      <Card style={{ padding: 24 }}>
        <Label accent={TOKENS.ink3}>funnel · 4-stage selection</Label>
        <h2 style={{ margin: '8px 0 0', fontFamily: TOKENS.sans, fontSize: 22, fontWeight: 300, color: TOKENS.ink0 }}>
          {fmtNum(stages[0]?.count ?? 0)} → {fmtNum(stages[stages.length - 1]?.count ?? 0)} pipeline
        </h2>
        <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {stages.map((f, i) => {
            const c = STAGE_COLORS[f.stage] ?? accentColor;
            const next = stages[i + 1];
            const drop = next ? f.count - next.count : null;
            const widthPct = Math.max(8, (f.count / total) * 100);
            const meta = (f as { meta?: Record<string, unknown> | null }).meta ?? null;
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
                  title={
                    f.stage === 'unique_normalized' && meta?.broker_listings_count
                      ? `${fmtNum(Number(meta.broker_listings_count))} raw broker listings dedup → ${fmtNum(f.count)} unique normalized`
                      : undefined
                  }
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: c, textTransform: 'uppercase' }}>
                        {STAGE_LABELS[f.stage] ?? f.stage}
                      </div>
                      <div style={{ fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink3, marginTop: 4 }}>
                        {STAGE_DESC[f.stage] ?? ''}
                      </div>
                      {f.stage === 'scored' && meta && (
                        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginTop: 4 }}>
                          budget {fmtNum(Number(meta.budget_attempted ?? meta.target_budget ?? f.count))}
                          {meta.binding_constraint
                            ? ` · binding: ${String(meta.binding_constraint)}`
                            : ''}
                          {meta.score_failures != null && Number(meta.score_failures) > 0
                            ? ` · ${fmtNum(Number(meta.score_failures))} timeouts`
                            : ''}
                        </div>
                      )}
                      {f.stage === 'unique_normalized' && meta?.broker_listings_count != null && (
                        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginTop: 4 }}>
                          dedup from {fmtNum(Number(meta.broker_listings_count))} broker listings
                        </div>
                      )}
                      {f.stage === 'watching' && (
                        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: promotedN > 0 ? STAGE_COLORS.promoted : TOKENS.ink3, marginTop: 4 }}>
                          {promotedN > 0
                            ? `${fmtNum(promotedN)} promoted now (overlay)`
                            : 'no anomaly boosts this cycle'}
                        </div>
                      )}
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

// D118 — self-tuning priority rule panel. Renders current learned weights,
// recent weight history sparklines, score-age summary and the current
// budget controller state with its binding constraint label.
function PriorityRuleCard({
  accentColor,
  rule,
}: {
  accentColor: string;
  rule: NonNullable<IntelligenceUniverseResponse['priority_rule']> | null;
}) {
  const weights = rule?.weights ?? {};
  const history = rule?.weights_history ?? [];
  const weightKeysJoined = Object.keys(weights).sort().join('|');
  const histogramByComponent = useMemo(() => {
    const keys = Object.keys(weights);
    const map: Record<string, number[]> = {};
    for (const k of keys) map[k] = [];
    for (const snap of history) {
      for (const k of keys) {
        const v = snap.weights?.[k];
        if (typeof v === 'number') map[k].push(v);
      }
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history, weightKeysJoined]);
  if (!rule) {
    return (
      <Card style={{ padding: 20 }}>
        <Label accent={TOKENS.ink3}>self-tuning priority rule</Label>
        <div style={{ marginTop: 10, fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
          Waiting for first pipeline cycle…
        </div>
      </Card>
    );
  }
  const sortedWeights = Object.keys(rule.weights)
    .map((k) => ({ key: k, value: rule.weights[k] }))
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
  const maxWeight = Math.max(0.001, ...sortedWeights.map((w) => w.value ?? 0));

  return (
    <Card style={{ padding: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Label accent={TOKENS.ink3}>self-tuning priority rule · {rule.weights_cycle_count} cycles</Label>
        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          binding: <span style={{ color: accentColor }}>{rule.budget.binding_constraint}</span>
          {' · '}budget {fmtNum(rule.budget.target_budget)}
        </div>
      </div>

      <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 18 }}>
        <div>
          <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginBottom: 8 }}>
            learned weights (sum = 1.0)
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {sortedWeights.map((w) => {
              const pct = ((w.value ?? 0) / maxWeight) * 100;
              const series = histogramByComponent[w.key] ?? [];
              return (
                <div
                  key={w.key}
                  style={{ display: 'grid', gridTemplateColumns: '120px 1fr 50px 60px', gap: 8, alignItems: 'center' }}
                >
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>{w.key}</span>
                  <div style={{ height: 6, borderRadius: 3, background: TOKENS.bg3, overflow: 'hidden' }}>
                    <div
                      style={{
                        width: `${pct}%`,
                        height: '100%',
                        background: accentColor,
                        opacity: 0.7,
                        borderRadius: 3,
                      }}
                    />
                  </div>
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink1, textAlign: 'right' }}>
                    {(w.value ?? 0).toFixed(3)}
                  </span>
                  {series.length > 1 ? (
                    <Spark values={series} color={accentColor} height={14} />
                  ) : (
                    <span style={{ fontFamily: TOKENS.mono, fontSize: 9, color: TOKENS.ink3 }}>—</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div>
          <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginBottom: 8 }}>
            budget meter
          </div>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: 12,
              fontFamily: TOKENS.mono,
              fontSize: 11,
              color: TOKENS.ink2,
            }}
          >
            <div>
              target budget
              <div style={{ color: TOKENS.ink0, fontSize: 18, marginTop: 2 }}>
                {fmtNum(rule.budget.target_budget)}
              </div>
            </div>
            <div>
              cycle count
              <div style={{ color: TOKENS.ink0, fontSize: 18, marginTop: 2 }}>
                {fmtNum(rule.budget.cycle_count)}
              </div>
            </div>
            <div>
              never scored
              <div style={{ color: TOKENS.ink0, fontSize: 18, marginTop: 2 }}>
                {fmtNum(rule.score_age_summary.never_scored)}
              </div>
            </div>
            <div>
              median age
              <div style={{ color: TOKENS.ink0, fontSize: 18, marginTop: 2 }}>
                {rule.score_age_summary.median_age_sec > 0
                  ? `${(rule.score_age_summary.median_age_sec / 60).toFixed(1)} min`
                  : '—'}
              </div>
            </div>
          </div>
          <div style={{ marginTop: 12, fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink3, lineHeight: 1.5 }}>
            The budget self-tunes by AIMD throughput + utility saturation.
            Component weights self-tune by online logistic regression with
            AdaGrad. There are no operator-tunable numbers.
          </div>
        </div>
      </div>
    </Card>
  );
}

// D118 — asset-class coverage tab. Read-only mosaic that aggregates the
// scored set by ``klass`` so the operator can see whether the
// self-tuning priority rule is keeping reasonable cross-asset balance.
function CoverageTab({
  accentColor,
  coverage,
  symbols,
}: {
  accentColor: string;
  coverage: IntelligenceUniverseResponse['asset_class_coverage'];
  symbols: UniverseSymbolRow[];
}) {
  const fallback = useMemo(() => {
    if (coverage && coverage.by_asset_class.length > 0) return coverage;
    const counts: Record<string, number> = {};
    for (const s of symbols) counts[s.klass] = (counts[s.klass] || 0) + 1;
    const total = symbols.length || 1;
    const rows = Object.entries(counts)
      .map(([klass, count]) => ({ klass, count, share: count / total }))
      .sort((a, b) => b.count - a.count);
    return { total: symbols.length, by_asset_class: rows };
  }, [coverage, symbols]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <Card style={{ padding: 20 }}>
        <Label accent={TOKENS.ink3}>asset-class coverage</Label>
        <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3, marginTop: 6 }}>
          {fmtNum(fallback.total)} symbols selected by the priority rule, by class
        </div>
        <div
          style={{
            marginTop: 14,
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
            gap: 12,
          }}
        >
          {fallback.by_asset_class.map((row) => {
            const pct = Math.max(2, row.share * 100);
            return (
              <div
                key={row.klass}
                style={{
                  padding: 12,
                  borderRadius: 8,
                  background: TOKENS.bg2,
                  border: `1px solid ${TOKENS.line}`,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  {classGlyph(row.klass, 10)}
                  <span style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink1, fontWeight: 500 }}>
                    {row.klass}
                  </span>
                  <span style={{ marginLeft: 'auto', fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>
                    {fmtNum(row.count)}
                  </span>
                </div>
                <div style={{ marginTop: 8, height: 6, borderRadius: 3, background: TOKENS.bg3 }}>
                  <div
                    style={{
                      width: `${pct}%`,
                      height: '100%',
                      background: accentColor,
                      opacity: 0.65,
                      borderRadius: 3,
                    }}
                  />
                </div>
                <div style={{ marginTop: 6, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                  {(row.share * 100).toFixed(1)}% share
                </div>
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

// D118 — tier-transitions activity stream. Reads the ring buffer of
// ``data/runtime/universe_transitions.json`` (sourced from the snapshot
// service). Filterable by reason; click a row to jump to that symbol.
function TransitionsTab({
  accentColor,
  transitions,
  onSelect,
}: {
  accentColor: string;
  transitions: NonNullable<IntelligenceUniverseResponse['transitions']>;
  onSelect: (sym: string) => void;
}) {
  const [reasonFilter, setReasonFilter] = useState<string>('all');
  const reasons = useMemo(() => {
    const s = new Set<string>();
    for (const t of transitions) if (t.reason) s.add(t.reason);
    return ['all', ...Array.from(s).sort()];
  }, [transitions]);
  const filtered = useMemo(
    () => (reasonFilter === 'all' ? transitions : transitions.filter((t) => t.reason === reasonFilter)),
    [transitions, reasonFilter],
  );
  return (
    <Card style={{ padding: 20 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Label accent={TOKENS.ink3}>tier transitions · last {fmtNum(transitions.length)}</Label>
        <select
          value={reasonFilter}
          onChange={(e) => setReasonFilter(e.target.value)}
          style={{
            background: TOKENS.bg2,
            color: TOKENS.ink1,
            border: `1px solid ${TOKENS.line}`,
            borderRadius: 6,
            padding: '4px 8px',
            fontFamily: TOKENS.mono,
            fontSize: 11,
          }}
        >
          {reasons.map((r) => (
            <option key={r} value={r}>{r}</option>
          ))}
        </select>
      </div>
      <div
        style={{
          marginTop: 12,
          maxHeight: 460,
          overflow: 'auto',
          border: `1px solid ${TOKENS.line}`,
          borderRadius: 8,
        }}
      >
        {filtered.length === 0 && (
          <div
            style={{
              padding: 24,
              fontFamily: TOKENS.mono,
              fontSize: 11,
              color: TOKENS.ink3,
              textAlign: 'center',
            }}
          >
            No transitions recorded yet.
          </div>
        )}
        {filtered.map((t, idx) => {
          const ts = t.ts ? new Date(t.ts).toISOString().slice(11, 19) : '—';
          const delta = t.score_delta;
          const deltaColor = delta == null ? TOKENS.ink3 : delta >= 0 ? TOKENS.profit : TOKENS.danger;
          return (
            <button
              key={`${t.ts}-${t.symbol}-${idx}`}
              type="button"
              onClick={() => onSelect(t.symbol)}
              style={{
                display: 'grid',
                gridTemplateColumns: '80px 100px 1fr 1fr 80px',
                gap: 8,
                width: '100%',
                padding: '8px 12px',
                border: 'none',
                borderBottom: `1px solid ${TOKENS.line}`,
                background: 'transparent',
                color: TOKENS.ink2,
                fontFamily: TOKENS.mono,
                fontSize: 11,
                cursor: 'pointer',
                textAlign: 'left',
                alignItems: 'center',
              }}
            >
              <span style={{ color: TOKENS.ink3 }}>{ts}</span>
              <span style={{ color: accentColor }}>{t.symbol}</span>
              <span>
                {t.from_tier} → {t.to_tier}
              </span>
              <span style={{ color: TOKENS.ink3 }}>{t.reason}</span>
              <span style={{ color: deltaColor, textAlign: 'right' }}>
                {delta == null ? '—' : (delta > 0 ? '+' : '') + delta.toFixed(3)}
              </span>
            </button>
          );
        })}
      </div>
    </Card>
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
            {/* D118 — 6-stage funnel plus the legacy ``banned`` chip. */}
            {(['all', 'unique_normalized', 'priority_ranked', 'scored', 'watching', 'promoted', 'active_reps', 'banned'] as const).map((st) => (
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
                {STAGE_LABELS[st] ?? st} ({stageCounts[st] ?? (st === 'all' ? symbols.length : 0)})
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
                // D118 — surface priority breakdown + score age in
                // the native tooltip so the operator can see why a
                // symbol was picked without leaving the grid.
                const ageLine = s.last_scored_at
                  ? `\nlast scored: ${s.last_scored_at}`
                  : '\nlast scored: —';
                const breakdown = s.priority_breakdown
                  ? `\npriority: ${s.priority_breakdown.priority_score.toFixed(4)}\n` +
                    Object.entries(s.priority_breakdown.components)
                      .map(([k, v]) => `  ${k}: ${v.toFixed(3)}`)
                      .join('\n')
                  : '';
                const titleText = `${symbolTitle(s)}${ageLine}${breakdown}`;
                return (
                  <button
                    key={s.sym}
                    type="button"
                    title={titleText}
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
                    <th style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>last scored</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.slice(0, 400).map((s) => {
                    // D118 — show a compact age stripe + ISO when the
                    // priority pre-filter has touched this symbol.
                    let ageLabel = '—';
                    let ageColor = TOKENS.ink3;
                    if (s.last_scored_at) {
                      const t = new Date(s.last_scored_at).getTime();
                      if (!Number.isNaN(t)) {
                        const ageSec = Math.max(0, (Date.now() - t) / 1000);
                        if (ageSec < 60) ageLabel = `${ageSec.toFixed(0)}s`;
                        else if (ageSec < 3600) ageLabel = `${(ageSec / 60).toFixed(1)}m`;
                        else if (ageSec < 86400) ageLabel = `${(ageSec / 3600).toFixed(1)}h`;
                        else ageLabel = `${(ageSec / 86400).toFixed(1)}d`;
                        ageColor = ageSec < 600 ? TOKENS.profit : ageSec < 3600 ? TOKENS.caution : TOKENS.ink3;
                      }
                    }
                    const breakdown = s.priority_breakdown
                      ? `priority: ${s.priority_breakdown.priority_score.toFixed(4)}\n` +
                        Object.entries(s.priority_breakdown.components)
                          .map(([k, v]) => `  ${k}: ${v.toFixed(3)}`)
                          .join('\n')
                      : '';
                    const titleText = breakdown ? `${symbolTitle(s)}\n${breakdown}` : symbolTitle(s);
                    return (
                      <tr
                        key={s.sym}
                        title={titleText}
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
                        <td
                          style={{
                            padding: 8,
                            borderBottom: `1px solid ${TOKENS.line}`,
                            color: ageColor,
                            fontFamily: TOKENS.mono,
                            fontSize: 11,
                          }}
                        >
                          {ageLabel}
                        </td>
                      </tr>
                    );
                  })}
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
