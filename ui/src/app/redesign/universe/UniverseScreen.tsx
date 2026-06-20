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
import { InstrumentAvatar, instrumentDisplayName, instrumentSubtitle, type InstrumentVisual } from '../instrumentVisuals';

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
  scored: 'scored this cycle',
  watching: 'watching now',
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
  scored: 'Priority top-N selected from the unique universe and yfinance liquidity-scored in the latest cycle.',
  watching: 'Current core + scan watchlist; this is persisted operating state, not a strict downstream count from the latest scored cycle.',
  promoted: 'Temporary conviction boost from scan/light (filter instruments tab only).',
  active_reps: 'Correlation representatives under engine attention.',
  source: 'Raw broker listings plus curated broker seeds.',
  eligible: 'Normalized symbols selected for scoring.',
  active: 'Correlation representatives under engine attention.',
  banned: 'Excluded or blocked.',
};

type UniTab = 'overview' | 'funnel' | 'instruments' | 'coverage' | 'transitions';

const ROW_STAGE_FILTERS = ['all', 'watching', 'promoted', 'active_reps', 'banned'] as const;
type RowStageFilter = typeof ROW_STAGE_FILTERS[number];

function symbolTitle(row: UniverseSymbolRow): string {
  return (row.name || row.description || row.sym).trim();
}

function symbolSubtitle(row: UniverseSymbolRow): string {
  return instrumentSubtitle(universeVisual(row)) || row.klass;
}

function universeVisual(row: UniverseSymbolRow): InstrumentVisual {
  return {
    sym: row.sym,
    name: row.name || row.sym,
    description: row.description,
    category: row.category || row.klass,
    logoUrl: row.logo_url,
    logoKind: row.logo_kind || row.klass,
    assetClass: row.klass,
    exchange: row.exchange,
    currency: row.currency,
  };
}

function fmtNum(n: number): string {
  return n.toLocaleString();
}

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage.replace(/_/g, ' ');
}

function rowStage(row: UniverseSymbolRow): string {
  return row.stage === 'active' ? 'active_reps' : row.stage;
}

function rowMatchesStage(row: UniverseSymbolRow, stage: string): boolean {
  if (stage === 'all') return true;
  const normalized = rowStage(row);
  if (stage === 'watching') {
    return normalized === 'watching' || normalized === 'promoted' || normalized === 'active_reps';
  }
  return normalized === stage;
}

function instrumentFilterForFunnelStage(stage: string): RowStageFilter {
  if (stage === 'active' || stage === 'active_reps') return 'active_reps';
  if (stage === 'promoted') return 'promoted';
  if (stage === 'watching') return 'watching';
  if (stage === 'banned') return 'banned';
  return 'all';
}

function brokerListingsMeta(meta: Record<string, unknown> | null | undefined): number | null {
  const value = meta?.broker_listings ?? meta?.broker_listings_count;
  return typeof value === 'number' ? value : value != null ? Number(value) : null;
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
                brokerCoverage={data?.coverage ?? null}
                coverage={data?.asset_class_coverage ?? null}
                symbols={symbols}
              />
            )}
            {tab === 'transitions' && (
              <TransitionsTab
                accentColor={accentColor}
                transitions={data?.transitions ?? []}
                symbols={symbols}
                onSelect={setSelected}
              />
            )}
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
  accentColor, funnel, stream, symbols, clusters, data, onSelect, onJumpTo,
}: {
  accentColor: string;
  funnel: IntelligenceUniverseResponse['funnel'];
  stream: IntelligenceUniverseResponse['stream'];
  symbols: UniverseSymbolRow[];
  clusters: IntelligenceUniverseResponse['clusters'];
  data: IntelligenceUniverseResponse | null;
  onSelect: (s: string) => void;
  onJumpTo: (t: UniTab, stage?: string | null) => void;
}) {
  const uniqueStage = funnel.find((f) => f.stage === 'unique_normalized') ?? funnel.find((f) => f.stage === 'source');
  const scoredStage = funnel.find((f) => f.stage === 'scored') ?? funnel.find((f) => f.stage === 'eligible');
  const watchStage = funnel.find((f) => f.stage === 'watching');
  const activeStage = funnel.find((f) => f.stage === 'active_reps') ?? funnel.find((f) => f.stage === 'active');
  const uniqueMeta = (uniqueStage as { meta?: Record<string, unknown> } | undefined)?.meta;
  const scoredMeta = (scoredStage as { meta?: Record<string, unknown> } | undefined)?.meta;
  const rawListings = brokerListingsMeta(uniqueMeta) ?? data?.coverage?.broker_listing_count ?? 0;
  const uniqueCount = uniqueStage?.count ?? data?.coverage?.unique_normalized_count ?? 0;
  const scoredCount = scoredStage?.count ?? data?.coverage?.scored_candidate_count ?? 0;
  const watchingCount = watchStage?.count ?? data?.coverage?.watched_count ?? symbols.length;
  const activeCount = activeStage?.count ?? clusters.length;
  const budgetN = Number(scoredMeta?.budget_attempted ?? scoredMeta?.target_budget ?? data?.priority_rule?.budget?.target_budget ?? scoredCount);
  const binding = String(scoredMeta?.binding_constraint ?? data?.priority_rule?.budget?.binding_constraint ?? 'adaptive');
  const watchCap = data?.coverage?.caps?.watching ?? data?.adaptive?.resolved?.watching ?? 0;
  const coreCap = data?.coverage?.caps?.core ?? data?.adaptive?.resolved?.core ?? 0;
  const scanCap = data?.coverage?.caps?.scan ?? data?.adaptive?.resolved?.scan ?? 0;
  const candidateCap = data?.coverage?.caps?.candidates ?? data?.adaptive?.resolved?.candidates ?? 0;
  const promotedN = promotedNowCount(funnel, data?.promotions);
  // Decompose the live watchlist into "ranked this rebuild" vs "held from
  // prior cycles (anti-churn grace)". The cap is the per-rebuild target, so
  // anything above it is sticky memory, not an impossible downstream count.
  const heldCount = watchCap > 0 ? Math.max(0, watchingCount - watchCap) : 0;
  const rankedCount = Math.max(0, watchingCount - heldCount);
  const overTarget = watchCap > 0 && watchingCount > watchCap * 1.5;
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
            <Label accent={TOKENS.ink3}>universe · live</Label>
            <h2 style={{
              margin: '6px 0 0',
              fontFamily: TOKENS.sans,
              fontSize: 24,
              fontWeight: 300,
              letterSpacing: 0,
              lineHeight: 1.25,
              color: TOKENS.ink0,
            }}>
              <span style={{ color: STAGE_COLORS.watching, fontWeight: 400 }}>{fmtNum(watchingCount)}</span>
              {' on the watchlist'}
              <span style={{ color: TOKENS.ink3 }}>{' · '}</span>
              <span style={{ color: STAGE_COLORS.active_reps, fontWeight: 400 }}>{fmtNum(activeCount)}</span>
              {' in trading focus'}
            </h2>
            <div style={{ marginTop: 6, fontFamily: TOKENS.sans, fontSize: 13, color: TOKENS.ink2, maxWidth: 560, lineHeight: 1.5 }}>
              Scored {fmtNum(scoredCount)} of {fmtNum(uniqueCount)} unique instruments this refresh.
              {' '}{clusters.length} correlation clusters.
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

        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1.1fr)', gap: 16, alignItems: 'stretch' }}>
          <RefreshFunnelPanel
            rawListings={rawListings}
            uniqueCount={uniqueCount}
            scoredCount={scoredCount}
            budgetN={Number.isFinite(budgetN) ? budgetN : scoredCount}
            binding={binding}
            candidateCap={candidateCap}
            onJumpTo={onJumpTo}
          />
          <WatchlistPanel
            watchingCount={watchingCount}
            rankedCount={rankedCount}
            heldCount={heldCount}
            watchCap={watchCap}
            coreCap={coreCap}
            scanCap={scanCap}
            promotedN={promotedN}
            activeCount={activeCount}
            clusterCount={clusters.length}
            overTarget={overTarget}
            onJumpTo={onJumpTo}
          />
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
        <UniverseSummaryPanel
          data={data}
          symbols={symbols}
          clusters={clusters}
          funnel={funnel}
          priorityRule={data?.priority_rule ?? null}
          accentColor={accentColor}
          promotionCount={stream.length}
        />

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

// "Last refresh" — the part that genuinely IS a funnel and resets every
// pipeline cycle. Cool blue/grey so it reads as transient, not live state.
function RefreshFunnelPanel({
  rawListings,
  uniqueCount,
  scoredCount,
  budgetN,
  binding,
  candidateCap,
  onJumpTo,
}: {
  rawListings: number;
  uniqueCount: number;
  scoredCount: number;
  budgetN: number;
  binding: string;
  candidateCap: number;
  onJumpTo: (t: UniTab, stage?: string | null) => void;
}) {
  const steps: Array<{
    label: string;
    meaning: string;
    value: number;
    color: string;
    onClick: () => void;
  }> = [
    {
      label: 'all broker symbols',
      meaning: 'Every row the connected brokers + registry list.',
      value: rawListings,
      color: STAGE_COLORS.source,
      onClick: () => onJumpTo('coverage'),
    },
    {
      label: 'unique instruments',
      meaning: 'Same ticker on two venues counts once.',
      value: uniqueCount,
      color: STAGE_COLORS.unique_normalized,
      onClick: () => onJumpTo('coverage'),
    },
    {
      label: 'scored just now',
      meaning: 'Top names liquidity-scored this rebuild (time-limited).',
      value: scoredCount,
      color: STAGE_COLORS.scored,
      onClick: () => onJumpTo('instruments', 'all'),
    },
  ];
  return (
    <div style={{ padding: 16, borderRadius: 10, border: `1px solid ${TOKENS.line}`, background: TOKENS.bg1, display: 'flex', flexDirection: 'column' }}>
      <Label accent={STAGE_COLORS.scored}>last refresh · this cycle only</Label>
      <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column' }}>
        {steps.map((s, i) => (
          <div key={s.label}>
            <button
              type="button"
              onClick={s.onClick}
              title={s.meaning}
              style={{
                width: '100%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                padding: '11px 13px',
                borderRadius: 8,
                border: `1px solid ${s.color}44`,
                borderLeft: `3px solid ${s.color}`,
                background: `linear-gradient(165deg, ${s.color}12, ${TOKENS.bg2} 75%)`,
                cursor: 'pointer',
                textAlign: 'left',
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: s.color, textTransform: 'uppercase' }}>{s.label}</div>
                <div style={{ marginTop: 3, fontFamily: TOKENS.sans, fontSize: 10.5, color: TOKENS.ink3, lineHeight: 1.35 }}>{s.meaning}</div>
              </div>
              <div style={{ fontFamily: TOKENS.sans, fontSize: 24, fontWeight: 300, color: TOKENS.ink0, whiteSpace: 'nowrap' }}>{fmtNum(s.value)}</div>
            </button>
            {i < steps.length - 1 && (
              <div style={{ display: 'flex', justifyContent: 'center', color: TOKENS.ink4, fontSize: 12, lineHeight: 1, padding: '3px 0' }}>↓</div>
            )}
          </div>
        ))}
      </div>
      <div style={{ marginTop: 10, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.6 }}>
        Only the top {fmtNum(budgetN)} of {fmtNum(uniqueCount)} were scored this cycle (budget limit · {binding}). Candidate cap {fmtNum(candidateCap)}.
      </div>
    </div>
  );
}

// "Live watchlist" — sticky operating state, not a funnel step. One hero
// number decomposed into ranked-this-rebuild + held-from-memory, with
// boosted / trading-focus shown as layers ON the watchlist, not stages
// after it. Teal/purple/yellow so it reads as live, distinct from refresh.
function WatchlistPanel({
  watchingCount,
  rankedCount,
  heldCount,
  watchCap,
  coreCap,
  scanCap,
  promotedN,
  activeCount,
  clusterCount,
  overTarget,
  onJumpTo,
}: {
  watchingCount: number;
  rankedCount: number;
  heldCount: number;
  watchCap: number;
  coreCap: number;
  scanCap: number;
  promotedN: number;
  activeCount: number;
  clusterCount: number;
  overTarget: boolean;
  onJumpTo: (t: UniTab, stage?: string | null) => void;
}) {
  const total = Math.max(1, watchingCount);
  const rankedPct = (rankedCount / total) * 100;
  const heldPct = (heldCount / total) * 100;
  const rankedColor = STAGE_COLORS.watching;
  const heldColor = STAGE_COLORS.active_reps;
  return (
    <button
      type="button"
      onClick={() => onJumpTo('instruments', 'watching')}
      style={{
        padding: 16,
        borderRadius: 10,
        border: `1px solid ${rankedColor}44`,
        background: `linear-gradient(150deg, ${rankedColor}12, ${TOKENS.bg1} 65%)`,
        display: 'flex',
        flexDirection: 'column',
        textAlign: 'left',
        cursor: 'pointer',
        color: TOKENS.ink1,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, width: '100%' }}>
        <div>
          <Label accent={rankedColor}>live watchlist · what we track now</Label>
          <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            names we keep price + features for, and may trade
          </div>
        </div>
        <div style={{ fontFamily: TOKENS.sans, fontSize: 40, fontWeight: 300, lineHeight: 1, color: TOKENS.ink0 }}>{fmtNum(watchingCount)}</div>
      </div>

      {/* ranked + held decomposition — explains why total can exceed the cap */}
      <div style={{ marginTop: 14, display: 'flex', height: 10, borderRadius: 5, overflow: 'hidden', background: TOKENS.bg3 }}>
        <div style={{ width: `${rankedPct}%`, background: rankedColor, opacity: 0.8 }} title={`${fmtNum(rankedCount)} ranked this rebuild`} />
        {heldCount > 0 && (
          <div style={{ width: `${heldPct}%`, background: heldColor, opacity: 0.5 }} title={`${fmtNum(heldCount)} held from prior cycles (grace)`} />
        )}
      </div>
      <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: '4px 14px', fontFamily: TOKENS.mono, fontSize: 10.5, color: TOKENS.ink2 }}>
        <span title="Newly ranked into the watchlist this rebuild (core + scan).">
          <span style={{ color: rankedColor }}>●</span>{' '}{fmtNum(rankedCount)} ranked
          {watchCap > 0 && <span style={{ color: TOKENS.ink3 }}>{` (core ${fmtNum(coreCap)} + scan ${fmtNum(scanCap)})`}</span>}
        </span>
        {heldCount > 0 && (
          <span title="Fell out of the top-ranked set but kept a few cycles so the list does not flicker.">
            <span style={{ color: heldColor }}>●</span>{' '}{fmtNum(heldCount)} held (grace)
          </span>
        )}
      </div>

      {/* layers ON the watchlist — not downstream funnel stages */}
      <div style={{ marginTop: 12, paddingTop: 12, borderTop: `1px solid ${TOKENS.line}`, display: 'flex', gap: 18, flexWrap: 'wrap' }}>
        <span
          onClick={(e) => { e.stopPropagation(); onJumpTo('instruments', 'promoted'); }}
          title="Short-term 'pay extra attention' flag on a subset of the watchlist."
          style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink2 }}
        >
          <span style={{ color: STAGE_COLORS.promoted, fontSize: 16, fontWeight: 400 }}>{fmtNum(promotedN)}</span>{' boosted'}
        </span>
        <span
          onClick={(e) => { e.stopPropagation(); onJumpTo('instruments', 'active_reps'); }}
          title="One name per correlated group — the distinct bets strategies actually lean on."
          style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink2 }}
        >
          <span style={{ color: STAGE_COLORS.active_reps, fontSize: 16, fontWeight: 400 }}>{fmtNum(activeCount)}</span>{' in trading focus'}
          <span style={{ color: TOKENS.ink3 }}>{` · ${fmtNum(clusterCount)} clusters`}</span>
        </span>
      </div>

      {overTarget && (
        <div style={{ marginTop: 10, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.caution, lineHeight: 1.5 }}>
          Watchlist is {(watchingCount / Math.max(1, watchCap)).toFixed(1)}× the {fmtNum(watchCap)} per-rebuild target — large rotation recently.
        </div>
      )}
    </button>
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
  const scoredN = stages.find((f) => f.stage === 'scored')?.count ?? 0;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      <PriorityRuleCard accentColor={accentColor} rule={priorityRule ?? null} />

      <Card style={{ padding: 24 }}>
        <Label accent={TOKENS.ink3}>selection model · stateful</Label>
        <h2 style={{ margin: '8px 0 0', fontFamily: TOKENS.sans, fontSize: 22, fontWeight: 300, color: TOKENS.ink0 }}>
          Latest scoring pass and current watchlist
        </h2>
        <div style={{ marginTop: 6, maxWidth: 720, fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink3, lineHeight: 1.45 }}>
          Scored is the latest budgeted cycle. Watching is retained core + scan state, so it can be higher than the latest scored count without implying new symbols appeared from nowhere.
        </div>
        <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
          {stages.map((f, i) => {
            const c = STAGE_COLORS[f.stage] ?? accentColor;
            const next = stages[i + 1];
            const drop = next ? f.count - next.count : null;
            const widthPct = Math.max(8, (f.count / total) * 100);
            const meta = (f as { meta?: Record<string, unknown> | null }).meta ?? null;
            const listingCount = brokerListingsMeta(meta);
            return (
              <div key={f.stage}>
                <button
                  type="button"
                  onClick={() => onJumpTo('instruments', instrumentFilterForFunnelStage(f.stage))}
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
                    f.stage === 'unique_normalized' && listingCount != null
                      ? `${fmtNum(listingCount)} raw broker listings dedup → ${fmtNum(f.count)} unique normalized`
                      : undefined
                  }
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: c, textTransform: 'uppercase' }}>
                        {stageLabel(f.stage)}
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
                      {f.stage === 'unique_normalized' && listingCount != null && (
                        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginTop: 4 }}>
                          dedup from {fmtNum(listingCount)} broker listings
                        </div>
                      )}
                      {f.stage === 'watching' && (
                        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: promotedN > 0 ? STAGE_COLORS.promoted : TOKENS.ink3, marginTop: 4 }}>
                          {promotedN > 0
                            ? `${fmtNum(promotedN)} promoted now (overlay)`
                            : 'no anomaly boosts this cycle'}
                          {f.count > scoredN && scoredN > 0
                            ? ` · retained state exceeds latest scored by ${fmtNum(f.count - scoredN)}`
                            : ''}
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
    <Card style={{ padding: 20, overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Label accent={TOKENS.ink3}>self-tuning priority rule · {rule.weights_cycle_count} cycles</Label>
        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          binding: <span style={{ color: accentColor }}>{rule.budget.binding_constraint}</span>
          {' · '}budget {fmtNum(rule.budget.target_budget)}
        </div>
      </div>

      <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 24, alignItems: 'start' }}>
        <div style={{ minWidth: 0 }}>
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
                  style={{ display: 'grid', gridTemplateColumns: 'minmax(116px, 0.9fr) minmax(72px, 1fr) 44px 54px', gap: 8, alignItems: 'center', minWidth: 0, overflow: 'hidden' }}
                >
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{w.key}</span>
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
                    <span style={{ width: 54, height: 14, overflow: 'hidden', display: 'block' }}>
                      <Spark values={series} width={52} height={14} accent={accentColor} area={false} />
                    </span>
                  ) : (
                    <span style={{ fontFamily: TOKENS.mono, fontSize: 9, color: TOKENS.ink3 }}>—</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div style={{ minWidth: 0 }}>
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
  brokerCoverage,
  coverage,
  symbols,
}: {
  accentColor: string;
  brokerCoverage: IntelligenceUniverseResponse['coverage'] | null;
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
          {fmtNum(fallback.total)} symbol rows in the current snapshot, by class
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

      <Card style={{ padding: 20 }}>
        <Label accent={TOKENS.ink3}>broker + registry coverage</Label>
        <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3, marginTop: 6 }}>
          {fmtNum(brokerCoverage?.broker_listing_count ?? 0)} raw listings · {fmtNum(brokerCoverage?.unique_normalized_count ?? 0)} unique normalized
          {brokerCoverage?.registry_active_count != null
            ? ` · ${fmtNum(Number(brokerCoverage.registry_active_count))} active registry rows`
            : ''}
        </div>
        <div
          style={{
            marginTop: 14,
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
            gap: 10,
          }}
        >
          {Object.entries(brokerCoverage?.by_broker ?? {}).map(([name, info]) => {
            const raw = info?.raw ?? 0;
            const normalized = info?.normalized ?? 0;
            const known = info?.registry_known_count ?? 0;
            const covered = info?.registry_covered_count ?? 0;
            const pct = known > 0 ? Math.min(100, Math.max(2, (covered / known) * 100)) : 0;
            const c =
              known === 0 ? TOKENS.ink3 :
              covered === 0 ? TOKENS.caution :
              covered >= known ? TOKENS.profit :
              TOKENS.info;
            return (
              <div
                key={name}
                title={info?.note ?? undefined}
                style={{
                  padding: 12,
                  borderRadius: 8,
                  background: TOKENS.bg2,
                  border: `1px solid ${TOKENS.line}`,
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
                  <span style={{ fontFamily: TOKENS.sans, fontSize: 13, color: TOKENS.ink1, fontWeight: 500 }}>{name}</span>
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: c }}>{info?.source ?? 'broker_catalog'}</span>
                </div>
                <div style={{ marginTop: 8, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.6 }}>
                  {fmtNum(raw)} listed · {fmtNum(normalized)} normalized
                  <br />
                  {fmtNum(covered)}/{fmtNum(known)} registry available
                </div>
                <div style={{ marginTop: 8, height: 5, borderRadius: 3, background: TOKENS.bg3 }}>
                  <div
                    style={{
                      width: `${pct}%`,
                      height: '100%',
                      background: c,
                      opacity: 0.7,
                      borderRadius: 3,
                    }}
                  />
                </div>
              </div>
            );
          })}
          {Object.keys(brokerCoverage?.by_broker ?? {}).length === 0 && (
            <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
              Broker coverage has not been published yet.
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function UniverseSummaryPanel({
  data,
  symbols,
  clusters,
  funnel,
  priorityRule,
  accentColor,
  promotionCount,
}: {
  data: IntelligenceUniverseResponse | null;
  symbols: UniverseSymbolRow[];
  clusters: IntelligenceUniverseResponse['clusters'];
  funnel: IntelligenceUniverseResponse['funnel'];
  priorityRule: IntelligenceUniverseResponse['priority_rule'];
  accentColor: string;
  promotionCount: number;
}) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const active = useMemo(() => symbols.filter((s) => rowStage(s) === 'active_reps'), [symbols]);
  const watching = funnel.find((f) => f.stage === 'watching')?.count ?? symbols.length;
  const activeCount = funnel.find((f) => f.stage === 'active_reps')?.count ?? active.length;
  const activeRowsCount = active.length;
  const avgConviction = activeRowsCount > 0
    ? active.reduce((acc, s) => acc + (Number(s.conviction) || 0), 0) / activeRowsCount
    : 0;
  const representedMembers = clusters.reduce((acc, c) => acc + (Number(c.member_count) || 0), 0);
  const largestCluster = clusters.slice().sort((a, b) => b.member_count - a.member_count)[0];
  const classCounts = useMemo(() => {
    const rows: Array<{ klass: string; count: number; share: number }> = [];
    const counts: Record<string, number> = {};
    for (const s of symbols) counts[s.klass] = (counts[s.klass] || 0) + 1;
    for (const [klass, count] of Object.entries(counts)) {
      rows.push({ klass, count, share: watching > 0 ? count / watching : 0 });
    }
    return rows.sort((a, b) => b.count - a.count);
  }, [symbols, watching]);
  const scored = funnel.find((f) => f.stage === 'scored')?.count ?? 0;
  const unique = funnel.find((f) => f.stage === 'unique_normalized')?.count ?? 0;
  const attentionRatio = watching > 0 ? activeCount / watching : 0;
  const compressionRatio = unique > 0 ? activeCount / unique : 0;
  const budget = priorityRule?.budget.target_budget ?? null;
  const binding = priorityRule?.budget.binding_constraint ?? null;
  const intervalSecRaw = data?.build?.intervalSec ?? data?.config_mirror?.rebuild?.interval_sec;
  const intervalSec = typeof intervalSecRaw === 'number' ? intervalSecRaw : Number(intervalSecRaw || 0);
  const nextBuildMs = typeof data?.build?.nextBuildAt === 'string' ? new Date(data.build.nextBuildAt).getTime() : NaN;
  const generatedMs = data?.generated_at ? new Date(data.generated_at).getTime() : NaN;
  const nextMs = Number.isFinite(nextBuildMs)
    ? nextBuildMs
    : Number.isFinite(generatedMs) && intervalSec > 0
      ? generatedMs + intervalSec * 1000
      : NaN;
  const remainingSec = Number.isFinite(nextMs) ? Math.max(0, Math.ceil((nextMs - nowMs) / 1000)) : null;
  const refreshText = remainingSec == null
    ? '—'
    : remainingSec >= 3600
      ? `${Math.floor(remainingSec / 3600)}h ${Math.floor((remainingSec % 3600) / 60)}m`
      : remainingSec >= 60
        ? `${Math.floor(remainingSec / 60)}m ${remainingSec % 60}s`
        : `${remainingSec}s`;

  const summaryTiles = [
    { label: 'attention set', value: `${fmtNum(activeCount)} / ${fmtNum(watching)}`, sub: `${(attentionRatio * 100).toFixed(1)}% active reps` },
    { label: 'next refresh in', value: refreshText, sub: intervalSec > 0 ? `cycle every ${fmtNum(intervalSec)}s` : 'waiting for scheduler' },
    { label: 'selection pressure', value: `${(compressionRatio * 100).toFixed(2)}%`, sub: `${fmtNum(unique)} unique → ${fmtNum(activeCount)} reps` },
    { label: 'priority budget', value: budget == null ? '—' : fmtNum(budget), sub: binding ? `binding: ${binding}` : `${fmtNum(scored)} scored` },
  ];

  return (
    <Card style={{ padding: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10, gap: 10, alignItems: 'center' }}>
        <Label accent={TOKENS.ink3}>universe summary · live</Label>
        <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          {fmtNum(clusters.length)} clusters · {fmtNum(promotionCount)} boosts
        </span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 10 }}>
        {summaryTiles.map((tile) => (
          <div key={tile.label} style={{ padding: 12, borderRadius: 8, background: TOKENS.bg2, border: `1px solid ${TOKENS.line}`, minWidth: 0 }}>
            <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{tile.label}</div>
            <div style={{ marginTop: 5, fontFamily: TOKENS.sans, fontSize: 22, fontWeight: 300, color: TOKENS.ink0, lineHeight: 1.1 }}>{tile.value}</div>
            <div style={{ marginTop: 5, fontFamily: TOKENS.sans, fontSize: 10, color: TOKENS.ink3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{tile.sub}</div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 14, display: 'grid', gridTemplateColumns: '1.15fr 0.85fr', gap: 16 }}>
        <div>
          <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginBottom: 8 }}>asset mix in watching set</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
            {classCounts.map((row) => (
              <div key={row.klass}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink2 }}>
                  <span>{row.klass}</span>
                  <span>{fmtNum(row.count)} · {(row.share * 100).toFixed(0)}%</span>
                </div>
                <div style={{ marginTop: 3, height: 4, borderRadius: 2, background: TOKENS.bg3 }}>
                  <div style={{ width: `${Math.max(2, row.share * 100)}%`, height: '100%', borderRadius: 2, background: accentColor, opacity: 0.65 }} />
                </div>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginBottom: 8 }}>cluster concentration</div>
          <div style={{ padding: 12, borderRadius: 8, background: TOKENS.bg2, border: `1px solid ${TOKENS.line}` }}>
            <div style={{ fontFamily: TOKENS.sans, fontSize: 13, color: TOKENS.ink1 }}>
              {largestCluster
                ? `${largestCluster.representative} represents ${fmtNum(largestCluster.member_count)} names`
                : 'No cluster representatives yet'}
            </div>
            <div style={{ marginTop: 8, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.6 }}>
              represented members {fmtNum(representedMembers)}
              <br />
              avg active conviction {avgConviction.toFixed(1)}
              <br />
              largest avg|ρ| {largestCluster ? Number(largestCluster.avg_abs_correlation).toFixed(2) : '—'}
            </div>
          </div>
        </div>
      </div>
    </Card>
  );
}

// D118 — tier-transitions activity stream. Reads the ring buffer of
// ``data/runtime/universe_transitions.json`` (sourced from the snapshot
// service). Filterable by reason; click a row to jump to that symbol.
function TransitionsTab({
  accentColor,
  transitions,
  symbols,
  onSelect,
}: {
  accentColor: string;
  transitions: NonNullable<IntelligenceUniverseResponse['transitions']>;
  symbols: UniverseSymbolRow[];
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
                gridTemplateColumns: '80px minmax(180px, 1.2fr) 1fr 1fr 80px',
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
              <span style={{ color: accentColor }}>
                {(() => {
                  const row = symbols.find((x) => x.sym.toUpperCase() === t.symbol.toUpperCase());
                  return row ? instrumentDisplayName(universeVisual(row)) : t.symbol;
                })()}
              </span>
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
  const [stageFilter, setStageFilter] = useState<RowStageFilter>(
    initialStage ? instrumentFilterForFunnelStage(initialStage) : 'all',
  );
  const [q, setQ] = useState('');

  useEffect(() => {
    if (initialStage) setStageFilter(instrumentFilterForFunnelStage(initialStage));
  }, [initialStage]);

  const filtered = useMemo(() => symbols.filter((s) => {
    if (!rowMatchesStage(s, stageFilter)) return false;
    if (q) {
      const needle = q.toLowerCase();
      const hay = `${s.sym} ${s.name ?? ''} ${s.description ?? ''}`.toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  }), [symbols, stageFilter, q]);

  const stageCounts = useMemo(() => {
    const c: Record<string, number> = { all: symbols.length };
    symbols.forEach((s) => {
      const st = rowStage(s);
      c[st] = (c[st] || 0) + 1;
      if (st === 'promoted' || st === 'active_reps') c.watching = (c.watching || 0) + 1;
    });
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
            {ROW_STAGE_FILTERS.map((st) => (
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
                {stageLabel(st)} ({stageCounts[st] ?? (st === 'all' ? symbols.length : 0)})
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
          {fmtNum(filtered.length)} symbols
          {view === 'grid' && filtered.length > 240 ? ` · showing first ${fmtNum(240)}` : ''}
          {view === 'list' && filtered.length > 400 ? ` · showing first ${fmtNum(400)}` : ''}
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
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 8 }}>
              {filtered.slice(0, 240).map((s) => {
                const normalizedStage = rowStage(s);
                const c = STAGE_COLORS[normalizedStage] ?? accentColor;
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
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                      <InstrumentAvatar pos={universeVisual(s)} size={30} />
                      <div style={{ minWidth: 0 }}>
                        <span
                          style={{
                            display: 'block',
                            fontFamily: TOKENS.sans,
                            fontSize: 13,
                            fontWeight: 600,
                            color: TOKENS.ink0,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {instrumentDisplayName(universeVisual(s))}
                        </span>
                        <span style={{ display: 'block', fontFamily: TOKENS.mono, fontSize: 9, color: TOKENS.ink3 }}>
                          {s.sym}
                        </span>
                      </div>
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
                    <th style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>instrument</th>
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
                        <td style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 9, minWidth: 0 }}>
                            <InstrumentAvatar pos={universeVisual(s)} size={30} />
                            <div style={{ minWidth: 0 }}>
                              <div style={{
                                color: TOKENS.ink0,
                                fontWeight: 600,
                                overflow: 'hidden',
                                textOverflow: 'ellipsis',
                                whiteSpace: 'nowrap',
                              }}>
                                {instrumentDisplayName(universeVisual(s))}
                              </div>
                              <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                                {s.sym}
                              </div>
                            </div>
                          </div>
                        </td>
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
                        <td style={{ padding: 8, borderBottom: `1px solid ${TOKENS.line}` }}>{stageLabel(rowStage(s))}</td>
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
  const c = STAGE_COLORS[rowStage(s)] ?? accentColor;
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
              <InstrumentAvatar pos={universeVisual(s)} size={42} />
              <div>
                <h2 style={{ margin: 0, fontFamily: TOKENS.sans, fontSize: 24, fontWeight: 300, color: TOKENS.ink0 }}>
                  {instrumentDisplayName(universeVisual(s))}
                </h2>
                <div style={{ marginTop: 3, fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
                  {s.sym}
                </div>
              </div>
            </div>
            <div style={{ marginTop: 8, fontSize: 12, color: TOKENS.ink3 }}>
              {symbolSubtitle(s)} · {stageLabel(rowStage(s))}
            </div>
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
