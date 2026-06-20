/**
 * Secondary screens: Signals, Book, Risk, Strategies, TradeLog.
 * All screens are wired to live system data via `LiveData`.
 */

import { Fragment, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { Card, Label, Pill, Signed, Spark } from './primitives';
import { ACCENTS, AccentName, CURRENCY_SYMBOL, TOKENS } from './tokens';
import type { LiveData } from './useLiveSystem';
import { api, setApiControlToken, type AiPipelineStage, type AiPipelineView, type ConnectHubConnector, type DeploymentCheck, type DeploymentStage, type RoutingBrokerRow } from '../lib/api';
import { capitalAtWork, mapOrdersToTradeLog, normalizeSide, prettySymbol } from './mapping';
import { formatStrategyDisplayName } from './strategyLabels';
import { InstrumentAvatar, instrumentDisplayName, instrumentSubtitle } from './instrumentVisuals';

export function SignalsScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const cols = '80px 60px 120px 1fr 80px 80px 1.3fr 100px';

  const rows = useMemo(() => {
    const sigs = live.intelligence?.signals ?? [];
    return sigs.map((s) => {
      const verdict = (s.verdict ?? '').toLowerCase();
      const side = normalizeSide(s.side);
      const score = typeof s.confidence === 'number' ? Math.max(0, Math.min(1, s.confidence)) : 0;
      const urg: 'high' | 'med' | 'low' = score >= 0.7 ? 'high' : score >= 0.45 ? 'med' : 'low';
      const ts = s.timestamp ? Date.parse(s.timestamp) : 0;
      const age = ts > 0 ? minutesAgo(ts) : '—';
      const attr = Array.isArray(s.news_attribution) ? s.news_attribution : [];
      const top = attr[0];
      const attributionImpact =
        typeof top?.score === 'number' && Number.isFinite(top.score) ? top.score : null;
      const source = String(s.news_impact_source ?? '').toLowerCase();
      const aiImpact =
        typeof s.ai_news_score === 'number' && Number.isFinite(s.ai_news_score) ? s.ai_news_score : null;
      const accumulatorImpact =
        typeof s.accumulator_score === 'number' && Number.isFinite(s.accumulator_score) ? s.accumulator_score : null;
      const signalImpact =
        typeof s.news_score === 'number' && Number.isFinite(s.news_score) ? s.news_score : null;
      const fallbackImpact =
        source === 'ai_news'
          ? aiImpact ?? signalImpact
          : source === 'accumulator'
            ? accumulatorImpact ?? signalImpact
            : source === 'signal'
              ? signalImpact
              : null;
      const impact = attributionImpact ?? fallbackImpact;
      const mode = top?.match_mode ? String(top.match_mode).toLowerCase() : '';
      const evt = top?.event_type ? String(top.event_type).toLowerCase() : '';
      const headline = top?.headline ? String(top.headline) : '';
      const impactMode = attributionImpact != null ? mode : source;
      const conciseTopic = evt
        ? evt.replace(/_/g, ' ')
        : headline
            .replace(/\s+/g, ' ')
            .trim()
            .split(' ')
            .slice(0, 3)
            .join(' ')
            .toLowerCase();
      return {
        sym: (s.symbol ?? '').toUpperCase(),
        side,
        score,
        strat: s.strategy ?? '—',
        urg,
        verdict: verdict === 'approved' ? 'ok' : 'blocked',
        age,
        newsHeadline: top?.headline ? String(top.headline) : '',
        newsSource: top?.source ? String(top.source) : '',
        newsImpact: impact,
        newsMatchMode: impactMode,
        newsTopic: conciseTopic || (source === 'accumulator' ? 'accumulated conviction' : source === 'ai_news' ? 'AI news score' : source === 'signal' ? 'signal score' : ''),
        newsTitle: top?.headline
          ? String(top.headline)
          : source === 'accumulator'
            ? 'No linked headline; this value is accumulated conviction from the signal accumulator.'
            : source === 'ai_news'
              ? 'AI news score is present, but no source headline was linked for this signal.'
              : source === 'signal'
                ? 'Signal-level news score is present, but no linked headline was found.'
                : 'No linked news attribution',
        hasNewsImpact: impact != null,
      };
    });
  }, [live.intelligence]);

  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>
        All signals · {rows.length ? `last ${rows.length}` : 'awaiting pipeline'}
      </Label>
      <Card noPad>
        <div style={{
          padding: '12px 18px', borderBottom: `1px solid ${TOKENS.line}`,
          display: 'grid', gridTemplateColumns: cols, gap: 16,
          fontFamily: TOKENS.sans, fontSize: 10, color: TOKENS.ink3,
          textTransform: 'uppercase', letterSpacing: '0.1em',
        }}>
          <span>Symbol</span><span>Side</span><span>Score</span><span>Strategy</span>
          <span>Urgency</span><span>Verdict</span><span>News impact</span><span>Time</span>
        </div>
        {rows.length === 0 ? (
          <div style={{ padding: 40, textAlign: 'center', color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No intelligence signals yet · start the system to begin streaming
          </div>
        ) : rows.map((r, i) => (
          <div key={`${r.sym}-${i}`} style={{
            padding: '12px 18px', borderBottom: `1px solid ${TOKENS.line}`,
            display: 'grid', gridTemplateColumns: cols, gap: 16, alignItems: 'center',
          }}>
            <span
              title={r.sym}
              style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, color: TOKENS.ink0 }}>
              {prettySymbol(r.sym) || '—'}
            </span>
            <span style={{
              fontFamily: TOKENS.mono, fontSize: 11,
              color: r.side === 'short' ? TOKENS.loss : TOKENS.ink2,
            }}>{r.side}</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ flex: 1, height: 3, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                <div style={{
                  width: `${r.score * 100}%`, height: '100%',
                  background: r.side === 'short' ? TOKENS.loss : accentColor,
                }} />
              </div>
              <span style={{
                fontFamily: TOKENS.mono, fontSize: 11,
                color: r.side === 'short' ? TOKENS.loss : accentColor,
                width: 30, textAlign: 'right',
              }}>
                {r.score.toFixed(2)}
              </span>
            </div>
            <span style={{
              fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{r.strat}</span>
            <Pill size="sm" tone={r.urg === 'high' ? 'caution' : 'neutral'}>{r.urg}</Pill>
            <Pill size="sm" tone={r.verdict === 'blocked' ? 'danger' : 'profit'}>{r.verdict}</Pill>
            <span
              title={r.newsTitle}
              style={{
                fontFamily: TOKENS.mono,
                fontSize: 10,
                color: r.hasNewsImpact ? TOKENS.ink2 : TOKENS.ink3,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {r.hasNewsImpact ? (
                <>
                  <span style={{ color: TOKENS.ink3 }}>
                    {r.newsMatchMode === 'direct'
                      ? 'D'
                      : r.newsMatchMode === 'alias'
                        ? 'A'
                        : r.newsMatchMode === 'market'
                          ? 'M'
                          : r.newsMatchMode === 'ai_news'
                            ? 'AI'
                            : r.newsMatchMode === 'accumulator'
                              ? 'C'
                              : 'S'}
                  </span>
                  {' · '}
                  <span style={{ color: r.newsImpact != null && r.newsImpact >= 0 ? TOKENS.profit : TOKENS.loss }}>
                    {r.newsImpact != null ? `${r.newsImpact >= 0 ? '+' : ''}${r.newsImpact.toFixed(2)}` : 'n/a'}
                  </span>
                  {' · '}
                  <span style={{ color: TOKENS.ink2 }}>
                    {(r.newsTopic || 'market news').slice(0, 36)}
                  </span>
                </>
              ) : '—'}
            </span>
            <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{r.age}</span>
          </div>
        ))}
      </Card>
    </div>
  );
}

function minutesAgo(ts: number): string {
  const secs = Math.max(0, (Date.now() - ts) / 1000);
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

const STAGE_LABEL: Record<DeploymentStage, string> = {
  paper: 'Paper',
  micro_live: 'Micro-live',
  live: 'Live',
};

const STAGE_COPY: Record<DeploymentStage, string> = {
  paper: 'No real orders. The loop runs against live data and builds evidence.',
  micro_live: 'Real orders are allowed only through tiny M8 whitelists and caps.',
  live: 'Full operator capital path. Risk engine authority remains mandatory.',
};

function fmtTarget(v: unknown): string {
  if (v == null || v === '') return '--';
  if (typeof v === 'number') return Number.isInteger(v) ? `${v}` : v.toFixed(2);
  return String(v);
}

function evidenceMetric(
  evidence: Record<string, Record<string, unknown>> | undefined,
  stage: DeploymentStage,
  key: string,
): string {
  const raw = evidence?.[stage]?.[key];
  if (typeof raw === 'number') return Number.isInteger(raw) ? `${raw}` : raw.toFixed(2);
  if (typeof raw === 'string') return raw || '--';
  return '--';
}

function CheckRow({ check }: { check: DeploymentCheck }) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '22px minmax(190px, 1fr) 86px 110px minmax(220px, 1.2fr)',
      gap: 12,
      alignItems: 'center',
      padding: '10px 12px',
      borderBottom: `1px solid ${TOKENS.line}`,
      minHeight: 44,
    }}>
      <input
        type="checkbox"
        checked={!!check.passed}
        readOnly
        aria-label={check.label}
        style={{ width: 14, height: 14, accentColor: check.passed ? TOKENS.profit : TOKENS.caution }}
      />
      <span style={{ fontFamily: TOKENS.sans, fontSize: 13, color: TOKENS.ink1, minWidth: 0 }}>
        {check.label}
      </span>
      <Pill size="sm" tone={check.passed ? 'profit' : 'caution'}>
        {check.passed ? 'complete' : 'pending'}
      </Pill>
      <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink2 }}>
        {fmtTarget(check.current)} / {fmtTarget(check.target)}
      </span>
      <span
        title={check.detail || check.label}
        style={{
          fontFamily: TOKENS.mono,
          fontSize: 10,
          color: TOKENS.ink3,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {check.detail || '--'}
      </span>
    </div>
  );
}

export function ActivationScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const deployment = live.deployment;
  const [busy, setBusy] = useState<'promote' | 'paper' | 'micro_live' | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const stage: DeploymentStage = deployment?.stage ?? 'paper';
  const readinessLoaded = !!deployment;
  const ordered: DeploymentStage[] = ['paper', 'micro_live', 'live'];
  const currentIdx = ordered.indexOf(stage);
  const checks = deployment?.checks ?? [];
  const blockers = deployment?.blockers ?? [];
  const actionBlockers = deployment?.promotion_action_blockers ?? blockers;
  const canPromote = readinessLoaded && !!deployment?.next_stage && !!deployment?.promotion_action_ready && !busy;
  const progress = deployment?.checks_total
    ? Math.round((deployment.checks_passed / deployment.checks_total) * 100)
    : 0;

  const doPromote = async () => {
    setBusy('promote');
    setMessage(null);
    try {
      await api.promoteDeployment();
      setMessage('Promotion accepted. Restart the backend if the stage changes paper/live runtime semantics.');
      live.refresh();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setMessage(msg);
    } finally {
      setBusy(null);
    }
  };

  const doDemote = async (target: DeploymentStage) => {
    setBusy(target);
    setMessage(null);
    try {
      await api.demoteDeployment(target);
      setMessage(`Demoted to ${STAGE_LABEL[target]}. Restart if the runtime environment also needs to change.`);
      live.refresh();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setMessage(msg);
    } finally {
      setBusy(null);
    }
  };

  const onPhaseClick = (target: DeploymentStage) => {
    if (!readinessLoaded || busy) return;
    const targetIdx = ordered.indexOf(target);
    if (targetIdx === currentIdx) return;
    if (targetIdx < currentIdx) {
      void doDemote(target);
      return;
    }
    if (target === deployment?.next_stage && canPromote) {
      void doPromote();
    }
  };

  const phaseMeta = (target: DeploymentStage, idx: number) => {
    const active = target === stage;
    const complete = idx < currentIdx;
    const isNext = target === deployment?.next_stage;
    const canSelect = readinessLoaded && !busy && (complete || (isNext && canPromote));
    const tone: 'info' | 'profit' | 'caution' | 'neutral' =
      active ? 'info' : complete ? 'profit' : isNext && canPromote ? 'profit' : isNext ? 'caution' : 'neutral';
    const label =
      active ? 'current' :
      complete ? 'click to demote' :
      isNext && canPromote ? 'click to promote' :
      isNext ? `${actionBlockers.length} blockers` :
      'locked';
    const note =
      active ? 'You are here now.' :
      complete ? `Click to demote back to ${STAGE_LABEL[target]}.` :
      isNext && canPromote ? `All required selection checks are complete. Click to select ${STAGE_LABEL[target]}.` :
      isNext ? `${actionBlockers.length} required check${actionBlockers.length === 1 ? '' : 's'} still pending before ${STAGE_LABEL[target]} can be selected.` :
      `Complete ${STAGE_LABEL[ordered[idx - 1]]} before ${STAGE_LABEL[target]} can unlock.`;
    return { active, complete, canSelect, tone, label, note };
  };

  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 18, marginBottom: 18 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Label accent={accentColor}>Deployment stage</Label>
          <h1 style={{
            margin: '8px 0 6px',
            fontFamily: TOKENS.sans,
            fontSize: 30,
            lineHeight: 1.05,
            fontWeight: 520,
            color: TOKENS.ink0,
            letterSpacing: 0,
          }}>
            {STAGE_LABEL[stage]}
          </h1>
          <div style={{ fontFamily: TOKENS.mono, fontSize: 12, color: TOKENS.ink2 }}>
            {readinessLoaded
              ? <>runtime {deployment.runtime_env} · {deployment.paper_mode ? 'paper-safe' : 'real-order capable'}</>
              : <>deployment readiness not loaded · backend restart or API check required</>}
          </div>
        </div>
        <div style={{
          minWidth: 210,
          border: `1px solid ${TOKENS.line}`,
          borderRadius: 8,
          padding: 12,
          background: TOKENS.bg1,
        }}>
          <Label>Promotion progress</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginTop: 8 }}>
            <span style={{ fontFamily: TOKENS.sans, fontSize: 30, color: accentColor, fontVariantNumeric: 'tabular-nums' }}>
              {progress}%
            </span>
            <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
              {readinessLoaded ? `${deployment.checks_passed}/${deployment.checks_total} checks` : 'waiting for API'}
            </span>
          </div>
          <div style={{ height: 4, background: TOKENS.bg3, borderRadius: 4, overflow: 'hidden', marginTop: 8 }}>
            <div style={{ width: `${progress}%`, height: '100%', background: accentColor }} />
          </div>
        </div>
        <div style={{
          minWidth: 180,
          border: `1px solid ${TOKENS.line}`,
          borderRadius: 8,
          padding: 12,
          background: TOKENS.bg1,
        }}>
          <Label>Days left</Label>
          <div style={{ marginTop: 8, fontFamily: TOKENS.sans, fontSize: 30, color: deployment?.days_left ? TOKENS.caution : TOKENS.profit }}>
            {deployment?.days_left ?? '--'}
          </div>
          <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            before next stage evidence target
          </div>
        </div>
      </div>

      {!readinessLoaded && (
        <div style={{
          border: `1px solid ${TOKENS.caution}55`,
          borderRadius: 8,
          background: 'rgba(252,211,77,0.08)',
          padding: '12px 14px',
          marginBottom: 18,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 14,
        }}>
          <div>
            <Label accent={TOKENS.caution}>Readiness API unavailable</Label>
            <div style={{ marginTop: 5, fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, lineHeight: 1.5 }}>
              The UI is loaded, but `/deployment/readiness` has not returned. Restart the backend after this update, then refresh the dashboard.
            </div>
          </div>
          <Pill tone="caution" size="sm">not verified</Pill>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 12, marginBottom: 18 }}>
        {ordered.map((s, i) => {
          const meta = phaseMeta(s, i);
          return (
            <div
              key={s}
              role="button"
              tabIndex={meta.canSelect ? 0 : -1}
              title={meta.note}
              onClick={() => onPhaseClick(s)}
              onKeyDown={(e) => {
                if (!meta.canSelect) return;
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onPhaseClick(s);
                }
              }}
              style={{
                border: `1px solid ${meta.active ? accentColor : meta.canSelect ? `${TOKENS.profit}66` : TOKENS.line}`,
                borderRadius: 8,
                background: meta.active ? `${accentColor}10` : meta.canSelect ? 'rgba(94,234,212,0.06)' : TOKENS.bg1,
                padding: 16,
                minHeight: 166,
                opacity: meta.active || meta.canSelect ? 1 : 0.58,
                cursor: meta.canSelect ? 'pointer' : 'default',
                transition: `border-color ${TOKENS.fast}ms ${TOKENS.ease}, background ${TOKENS.fast}ms ${TOKENS.ease}, opacity ${TOKENS.fast}ms ${TOKENS.ease}`,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
                <Label accent={meta.active ? accentColor : meta.canSelect ? TOKENS.profit : undefined}>Phase 0{i + 1}</Label>
                <Pill size="sm" tone={meta.tone}>
                  {meta.label}
                </Pill>
              </div>
              <div style={{ marginTop: 18, fontFamily: TOKENS.sans, fontSize: 18, color: TOKENS.ink0, fontWeight: 560 }}>
                {STAGE_LABEL[s]}
              </div>
              <div style={{ marginTop: 10, fontFamily: TOKENS.mono, fontSize: 11, lineHeight: 1.55, color: TOKENS.ink2 }}>
                {STAGE_COPY[s]}
              </div>
              <div style={{
                marginTop: 12,
                paddingTop: 10,
                borderTop: `1px solid ${TOKENS.line}`,
                fontFamily: TOKENS.mono,
                fontSize: 10,
                lineHeight: 1.45,
                color: meta.canSelect ? TOKENS.profit : meta.active ? accentColor : TOKENS.ink3,
              }}>
                {meta.note}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.35fr', gap: 14, alignItems: 'start' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ border: `1px solid ${TOKENS.line}`, borderRadius: 8, background: TOKENS.bg1, padding: 16 }}>
            <Label>Evidence</Label>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10, marginTop: 14 }}>
              {[
                ['Paper days', evidenceMetric(deployment?.evidence, 'paper', 'days_observed')],
                ['Paper signals', evidenceMetric(deployment?.evidence, 'paper', 'signals')],
                ['Paper risk checks', evidenceMetric(deployment?.evidence, 'paper', 'risk_decisions')],
                ['Paper drawdown', `${evidenceMetric(deployment?.evidence, 'paper', 'max_drawdown_pct')}%`],
                ['Micro-live days', evidenceMetric(deployment?.evidence, 'micro_live', 'days_observed')],
                ['Micro-live fills', evidenceMetric(deployment?.evidence, 'micro_live', 'fills')],
              ].map(([k, v]) => (
                <div key={k} style={{ border: `1px solid ${TOKENS.line}`, borderRadius: 8, padding: 10, background: TOKENS.bg0 }}>
                  <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{k}</div>
                  <div style={{ marginTop: 5, fontFamily: TOKENS.sans, fontSize: 18, color: TOKENS.ink1, fontVariantNumeric: 'tabular-nums' }}>
                    {v}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {message && (
            <div style={{
              border: `1px solid ${message.includes('blocked') || message.includes('failed') ? `${TOKENS.caution}55` : TOKENS.line}`,
              borderRadius: 8,
              background: message.includes('blocked') || message.includes('failed') ? 'rgba(252,211,77,0.08)' : TOKENS.bg1,
              padding: 14,
              fontFamily: TOKENS.mono,
              fontSize: 10,
              color: message.includes('blocked') || message.includes('failed') ? TOKENS.caution : TOKENS.ink2,
              lineHeight: 1.5,
            }}>
              {message}
            </div>
          )}
        </div>

        <div style={{ border: `1px solid ${TOKENS.line}`, borderRadius: 8, background: TOKENS.bg1, overflow: 'hidden' }}>
          <div style={{ padding: '14px 16px', borderBottom: `1px solid ${TOKENS.line}`, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
            <Label>{deployment?.next_stage ? `Checks for ${STAGE_LABEL[deployment.next_stage]}` : 'Promotion checks'}</Label>
            <Pill size="sm" tone={!readinessLoaded ? 'caution' : blockers.length ? 'caution' : 'profit'}>
              {!readinessLoaded ? 'not loaded' : blockers.length ? 'blocked' : 'ready'}
            </Pill>
          </div>
          {checks.length ? checks.map((c) => <CheckRow key={c.key} check={c} />) : (
            <div style={{ padding: 28, fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3, textAlign: 'center' }}>
              Deployment readiness has not loaded yet.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const CONNECT_CATEGORY_LABELS: Record<string, string> = {
  brokers: 'Trading platforms',
  information_feeds: 'News & information',
  ai_providers: 'AI pipeline',
  treasury_accounts: 'Treasury accounts',
};

const CONNECT_CATEGORY_HINTS: Record<string, string> = {
  brokers: 'Routes orders only through connected venues with usable permissions.',
  information_feeds: 'Feeds standard news, macro, and event context into scoring.',
  ai_providers: 'Layered intelligence stack: core rules, sentiment, local reasoning, then paid escalation.',
  treasury_accounts: 'Funding inventory only; automatic cash movement is disabled until governed approval exists.',
};

function connectorTone(row: ConnectHubConnector): 'profit' | 'caution' | 'danger' | 'neutral' | 'info' {
  if (row.connected && row.healthy) return 'profit';
  if (!row.enabled) return 'neutral';
  if (!row.configured) return 'caution';
  if (row.connected) return 'info';
  return 'danger';
}

function connectorStatusLabel(row: ConnectHubConnector): string {
  if (!row.enabled) return 'off';
  if (row.connected && row.healthy) return 'connected';
  if (!row.configured) return 'needs keys';
  return row.state || 'ready';
}

function capabilityList(row: ConnectHubConnector): string[] {
  return Object.entries(row.capabilities ?? {})
    .filter(([, v]) => !!v)
    .filter(([k]) => !['non_disableable', 'core_required'].includes(k))
    .map(([k]) => k.replace(/^can_/, '').replace(/^supports_/, '').replace(/_/g, ' '))
    .slice(0, 6);
}

function isCoreConnector(row: ConnectHubConnector): boolean {
  return !!(row.capabilities?.core_required || row.capabilities?.non_disableable);
}

function ConnectorCard({
  row,
  onConfigure,
  onToggle,
  onDelete,
  busy,
}: {
  row: ConnectHubConnector;
  onConfigure: (row: ConnectHubConnector) => void;
  onToggle: (row: ConnectHubConnector) => void;
  onDelete: (row: ConnectHubConnector) => void;
  busy?: boolean;
}) {
  const caps = capabilityList(row);
  const missing = (row.required_secrets ?? []).filter((s) => s.required && !s.configured);
  const core = isCoreConnector(row);
  // D127 P1/P2/P3 — AI pipeline stages are never deletable. Broker
  // certification remains a backend execution gate, not a per-card UI badge.
  const showDelete = row.category !== 'ai_providers';
  return (
    <div style={{
      border: `1px solid ${TOKENS.line}`,
      borderRadius: 8,
      background: TOKENS.bg1,
      padding: 14,
      display: 'grid',
      gap: 10,
      minHeight: 146,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{
            fontFamily: TOKENS.sans,
            fontSize: 14,
            fontWeight: 550,
            color: TOKENS.ink0,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}>
            {row.label}
          </div>
          <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            {row.auth_type || 'api'} · {row.adapter || row.id}
          </div>
        </div>
        <div style={{ display: 'grid', gap: 4, justifyItems: 'end' }}>
          <Pill size="sm" tone={connectorTone(row)}>{connectorStatusLabel(row)}</Pill>
        </div>
      </div>
      <div style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 6,
        minHeight: 22,
        alignItems: 'flex-start',
      }}>
        {caps.length > 0 ? caps.map((c) => (
          <Pill key={c} size="sm" tone="neutral">{c}</Pill>
        )) : (
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4 }}>no capabilities declared</span>
        )}
      </div>
      {row.roles?.length ? (
        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.5 }}>
          {row.roles.join(' · ')}
        </div>
      ) : null}
      <div style={{
        marginTop: 'auto',
        borderTop: `1px solid ${TOKENS.line}`,
        paddingTop: 9,
        fontFamily: TOKENS.mono,
        fontSize: 10,
        color: missing.length ? TOKENS.caution : TOKENS.ink3,
        lineHeight: 1.45,
      }}>
        {missing.length
          ? `missing ${missing.map((s) => s.env).join(', ')}`
          : row.required_secrets?.length
            ? 'credentials present'
            : 'no secret required'}
      </div>
      {row.next_actions?.[0]?.label ? (
        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.45 }}>
          next: {row.next_actions[0].label}
        </div>
      ) : null}
      {core ? (
        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          core pipeline component · always on
        </div>
      ) : (
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        <button type="button" onClick={() => onConfigure(row)} style={{ ...cardButtonStyle(), flex: '1 1 70px' }}>
          Configure
        </button>
        <button type="button" disabled={busy} onClick={() => onToggle(row)} style={{ ...cardButtonStyle(row.enabled ? TOKENS.caution : TOKENS.profit), flex: '1 1 70px' }}>
          {row.enabled ? 'Disable' : 'Enable'}
        </button>
        {showDelete ? (
          <button type="button" disabled={busy} onClick={() => onDelete(row)} title="Delete connector" style={{ ...cardButtonStyle(TOKENS.danger), flex: '0 0 34px' }}>
            ×
          </button>
        ) : null}
      </div>
      )}
    </div>
  );
}

// ── D127 P3 — AI pipeline rendered as four fixed, ordered stages ─────────────

function aiModelLine(stage: AiPipelineStage): string {
  const m = (stage.model ?? {}) as Record<string, unknown>;
  const parts: string[] = [];
  if (m.model_name) parts.push(String(m.model_name));
  else if (m.provider) parts.push(String(m.provider));
  if (m.version) parts.push(`v${m.version}`);
  if (m.fallback_model) parts.push(`fallback ${m.fallback_model}`);
  return parts.join(' · ');
}

function AiStageCard({
  stage,
  connector,
  onConfigure,
  onToggle,
  busy,
}: {
  stage: AiPipelineStage;
  connector?: ConnectHubConnector;
  onConfigure: (stage: AiPipelineStage, connector?: ConnectHubConnector) => void;
  onToggle: (stage: AiPipelineStage) => void;
  busy?: boolean;
}) {
  const modelLine = aiModelLine(stage);
  // Local LLM opens the model catalogue; Premium opens the provider
  // picker; other stages open the env wizard.
  const isLocal = stage.id === 'local_reasoning';
  const isPremium = stage.id === 'premium_fallback';
  const canConfigure = isLocal || isPremium || !!connector;
  const configureLabel = isLocal ? 'Models' : isPremium ? 'Providers' : 'Configure';
  // Disable is blocked for the core (rules) and for FinBERT while it is
  // the only sentiment provider (P3). The backend also enforces this.
  const disableBlocked = stage.enabled && !stage.can_disable;
  return (
    <div style={{
      border: `1px solid ${TOKENS.line}`,
      borderRadius: 8,
      background: TOKENS.bg1,
      padding: 14,
      display: 'grid',
      gap: 10,
      minHeight: 158,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <div style={{
          flex: '0 0 22px',
          height: 22,
          borderRadius: 6,
          background: `${TOKENS.info}1f`,
          color: TOKENS.info,
          fontFamily: TOKENS.mono,
          fontSize: 11,
          fontWeight: 600,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}>
          {stage.order}
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{
            fontFamily: TOKENS.sans,
            fontSize: 14,
            fontWeight: 550,
            color: TOKENS.ink0,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}>
            {stage.label}
          </div>
          <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            {stage.role}
          </div>
        </div>
        <Pill size="sm" tone={stage.enabled ? 'profit' : 'neutral'}>
          {stage.enabled ? 'enabled' : 'disabled'}
        </Pill>
      </div>
      <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.5 }}>
        {stage.summary}
      </div>
      {modelLine ? (
        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink2 }}>
          {modelLine}
        </div>
      ) : null}
      <div style={{
        marginTop: 'auto',
        borderTop: `1px solid ${TOKENS.line}`,
        paddingTop: 9,
        fontFamily: TOKENS.mono,
        fontSize: 10,
        color: disableBlocked ? TOKENS.caution : TOKENS.ink3,
        lineHeight: 1.45,
      }}>
        {stage.core
          ? 'core pipeline component · always on'
          : disableBlocked
            ? stage.disable_blocked_reason
            : 'stage · cannot be deleted'}
      </div>
      {stage.core ? null : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          <button
            type="button"
            disabled={!canConfigure}
            onClick={() => canConfigure && onConfigure(stage, connector)}
            style={{ ...cardButtonStyle(), flex: '1 1 70px', opacity: canConfigure ? 1 : 0.5 }}
          >
            {configureLabel}
          </button>
          <button
            type="button"
            disabled={busy || disableBlocked}
            onClick={() => onToggle(stage)}
            title={disableBlocked ? stage.disable_blocked_reason : undefined}
            style={{
              ...cardButtonStyle(stage.enabled ? TOKENS.caution : TOKENS.profit),
              flex: '1 1 70px',
              opacity: disableBlocked ? 0.5 : 1,
            }}
          >
            {stage.enabled ? 'Disable' : 'Enable'}
          </button>
        </div>
      )}
    </div>
  );
}

function AiPipelineGrid({
  aiConnectors,
  refreshKey,
  onConfigure,
  onToggle,
  busyKey,
}: {
  aiConnectors: ConnectHubConnector[];
  refreshKey: number;
  onConfigure: (stage: AiPipelineStage, connector?: ConnectHubConnector) => void;
  onToggle: (stage: AiPipelineStage) => void;
  busyKey: string | null;
}) {
  const [view, setView] = useState<AiPipelineView | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.getAiPipeline()
      .then((v) => { if (!cancelled) { setView(v); setError(null); } })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [refreshKey]);

  if (error) {
    return (
      <div style={{
        border: `1px dashed ${TOKENS.lineStrong}`,
        borderRadius: 8,
        padding: 18,
        color: TOKENS.caution,
        fontFamily: TOKENS.mono,
        fontSize: 11,
      }}>
        AI pipeline unavailable: {error}
      </div>
    );
  }
  if (!view) {
    return (
      <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
        loading AI pipeline…
      </div>
    );
  }
  const byId = new Map(aiConnectors.map((c) => [c.id, c]));
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 260px))',
      gap: 12,
      alignItems: 'stretch',
    }}>
      {view.stages.map((stage) => (
        <AiStageCard
          key={stage.id}
          stage={stage}
          connector={byId.get(stage.id)}
          onConfigure={onConfigure}
          onToggle={onToggle}
          busy={busyKey === `ai_providers:${stage.id}`}
        />
      ))}
    </div>
  );
}

// ── D127 P4 — Local LLM catalogue modal ──────────────────────────────────────

function localFitnessTone(fitness: string): 'profit' | 'info' | 'caution' | 'neutral' {
  if (fitness === 'recommended') return 'profit';
  if (fitness === 'available') return 'info';
  if (fitness === 'too_slow') return 'caution';
  return 'neutral';
}

function LocalLlmCatalogueModal({
  accentColor,
  onClose,
  onChanged,
}: {
  accentColor: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [cat, setCat] = useState<import('../lib/api').LocalLlmCatalogue | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busyModel, setBusyModel] = useState<string | null>(null);

  const refetch = () => {
    setLoading(true);
    api.getLocalLlmCatalogue()
      .then((c) => { setCat(c); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };
  useEffect(() => { refetch(); }, []);

  async function runMutation<T>(fn: () => Promise<T>): Promise<T | null> {
    try {
      return await fn();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('(401)')) {
        const token = window.prompt('Control token required');
        if (token?.trim()) {
          setApiControlToken(token.trim());
          try { return await fn(); }
          catch (retry) { setError(retry instanceof Error ? retry.message : String(retry)); return null; }
        }
      }
      setError(msg);
      return null;
    }
  }

  const install = async (id: string) => {
    setBusyModel(id);
    setError(null);
    setMessage(`Installing ${id} — the model download and certification can take several minutes…`);
    const res = await runMutation(() => api.installLocalLlm({ model_id: id }));
    setBusyModel(null);
    if (res) {
      setMessage(res.ok ? `${id} installed and certified.` : `${id}: install or certification failed.`);
      refetch();
    }
  };
  const activate = async (id: string) => {
    setBusyModel(id);
    setError(null);
    setMessage(null);
    const res = await runMutation(() => api.activateLocalLlm({ model_id: id }));
    setBusyModel(null);
    if (res) {
      setMessage(res.next_step || `${id} is now the active Local LLM.`);
      refetch();
      onChanged();
    }
  };

  const probe = cat?.machine_probe;
  const probeCells: Array<[string, string]> = probe ? [
    ['CPU', `${probe.cpu_count} cores`],
    ['RAM', `${probe.ram_gb} GB`],
    ['GPU', probe.gpu_present ? `${probe.gpu_name ?? 'present'} · ${probe.vram_gb} GB` : 'none'],
    ['Disk free', `${probe.disk_free_gb} GB`],
    ['Ollama', probe.ollama_available ? 'available' : 'not found'],
  ] : [];

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 92, background: 'rgba(0,0,0,0.58)',
        backdropFilter: 'blur(10px)', display: 'flex', alignItems: 'center',
        justifyContent: 'center', padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 660, maxWidth: 'min(660px, 94vw)', maxHeight: '88vh', overflow: 'auto',
          borderRadius: 10, border: `1px solid ${TOKENS.lineStrong}`, background: TOKENS.bg1,
          boxShadow: '0 24px 80px rgba(0,0,0,0.65)', padding: 18,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 14 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Label accent={accentColor}>Local LLM catalogue</Label>
            <div style={{ marginTop: 6, fontFamily: TOKENS.sans, fontSize: 18, fontWeight: 560, color: TOKENS.ink0 }}>
              Supported local models
            </div>
            <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
              Curated, tested models only — install via Ollama, then activate.
            </div>
          </div>
          <button type="button" onClick={onClose} style={cardButtonStyle()}>Close</button>
        </div>

        {probe ? (
          <div style={{
            display: 'grid', gridTemplateColumns: `repeat(${probeCells.length}, 1fr)`,
            gap: 8, marginBottom: 12,
          }}>
            {probeCells.map(([k, v]) => (
              <div key={k} style={{
                border: `1px solid ${TOKENS.line}`, borderRadius: 6, padding: '8px 10px', background: TOKENS.bg0,
              }}>
                <div style={{ fontFamily: TOKENS.mono, fontSize: 9, color: TOKENS.ink3, textTransform: 'uppercase' }}>{k}</div>
                <div style={{ marginTop: 3, fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink1 }}>{v}</div>
              </div>
            ))}
          </div>
        ) : null}

        {cat && !cat.local_llm_available ? (
          <div style={{ marginBottom: 12 }}>
            <Pill tone="caution">Local LLM unavailable — {cat.unavailable_reason}</Pill>
          </div>
        ) : null}
        {message ? <div style={{ marginBottom: 10 }}><Pill tone="info">{message}</Pill></div> : null}
        {error ? <div style={{ marginBottom: 10 }}><Pill tone="danger">{error}</Pill></div> : null}
        {loading && !cat ? (
          <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>loading catalogue…</div>
        ) : null}

        <div style={{ display: 'grid', gap: 8 }}>
          {(cat?.models ?? []).map((m) => {
            const busy = busyModel === m.id;
            const unsupported = m.fitness === 'unsupported';
            return (
              <div key={m.id} style={{
                border: `1px solid ${TOKENS.line}`, borderRadius: 8, background: TOKENS.bg0,
                padding: 12, display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
              }}>
                <div style={{ flex: '1 1 200px', minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 550, color: TOKENS.ink0 }}>
                      {m.label}
                    </span>
                    <Pill size="sm" tone={localFitnessTone(m.fitness)}>{m.fitness}</Pill>
                    {m.installed ? <Pill size="sm" tone="profit">installed</Pill> : null}
                  </div>
                  <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                    {m.id} · {m.params} · {m.disk_gb} GB · needs {m.min_ram_gb} GB RAM / {m.min_vram_gb} GB VRAM
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  {m.installed ? (
                    <button
                      type="button" disabled={busy}
                      onClick={() => activate(m.id)}
                      style={{ ...cardButtonStyle(accentColor), minWidth: 78 }}
                    >
                      {busy ? '…' : 'Activate'}
                    </button>
                  ) : unsupported ? (
                    <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4 }}>
                      not supported here
                    </span>
                  ) : (
                    <button
                      type="button" disabled={busy || busyModel !== null}
                      onClick={() => install(m.id)}
                      style={{ ...cardButtonStyle(TOKENS.info), minWidth: 78 }}
                    >
                      {busy ? 'installing…' : 'Install'}
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── D127 P5 — Premium LLM provider picker modal ──────────────────────────────

const premiumInputStyle: CSSProperties = {
  width: '100%',
  height: 30,
  borderRadius: 6,
  border: `1px solid ${TOKENS.line}`,
  background: TOKENS.bg0,
  color: TOKENS.ink0,
  fontFamily: TOKENS.mono,
  fontSize: 11,
  padding: '0 9px',
  boxSizing: 'border-box',
};

function PremiumLlmPickerModal({
  accentColor,
  onClose,
  onChanged,
}: {
  accentColor: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [view, setView] = useState<import('../lib/api').PremiumLlmView | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [keyValues, setKeyValues] = useState<Record<string, string>>({});
  const [modelInput, setModelInput] = useState('');
  const [busy, setBusy] = useState<'save' | 'test' | 'activate' | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<import('../lib/api').PremiumTestResponse | null>(null);

  const refetch = () => {
    api.getPremiumLlmCatalogue()
      .then((v) => { setView(v); setError(null); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  };
  useEffect(() => { refetch(); }, []);

  const selected = view?.providers.find((p) => p.id === selectedId) ?? null;

  const selectProvider = (id: string) => {
    setSelectedId(id);
    setKeyValues({});
    setTestResult(null);
    setMessage(null);
    setError(null);
    const p = view?.providers.find((x) => x.id === id);
    setModelInput(
      p?.active && view?.active_model ? view.active_model : (p?.suggested_models?.[0] ?? ''),
    );
  };

  async function runMutation<T>(fn: () => Promise<T>): Promise<T | null> {
    try {
      return await fn();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('(401)')) {
        const token = window.prompt('Control token required');
        if (token?.trim()) {
          setApiControlToken(token.trim());
          try { return await fn(); }
          catch (retry) { setError(retry instanceof Error ? retry.message : String(retry)); return null; }
        }
      }
      setError(msg);
      return null;
    }
  }

  const saveCredentials = async () => {
    if (!selected) return;
    const secrets: Record<string, string> = {};
    for (const [env, val] of Object.entries(keyValues)) {
      if (val.trim()) secrets[env] = val.trim();
    }
    if (!Object.keys(secrets).length) {
      setError('Enter at least one credential value to save.');
      return;
    }
    setBusy('save'); setError(null); setMessage(null);
    const res = await runMutation(() => api.configureConnector({
      category: 'ai_providers', connector_id: 'premium_fallback', secrets, enable: true,
    }));
    setBusy(null);
    if (res) { setMessage('Credentials saved to .env.'); setKeyValues({}); refetch(); }
  };

  const runTest = async () => {
    if (!selected || !modelInput.trim()) { setError('Pick a provider and enter a model id.'); return; }
    setBusy('test'); setError(null); setMessage(null); setTestResult(null);
    const res = await runMutation(() => api.testPremiumLlm({
      provider_id: selected.id, model: modelInput.trim(),
    }));
    setBusy(null);
    if (res) {
      setTestResult(res);
      setMessage(res.ok ? 'Compatibility test passed.' : `Test failed: ${res.cert.reason}`);
    }
  };

  const activate = async () => {
    if (!selected || !modelInput.trim()) { setError('Pick a provider and enter a model id.'); return; }
    setBusy('activate'); setError(null); setMessage(null);
    const res = await runMutation(() => api.activatePremiumLlm({
      provider_id: selected.id, model: modelInput.trim(),
    }));
    setBusy(null);
    if (res) { setMessage(res.next_step || `${selected.label} activated.`); refetch(); onChanged(); }
  };

  const credEnvs = selected
    ? [selected.auth_env, ...(selected.requires_base_url && selected.base_url_env ? [selected.base_url_env] : [])]
    : [];

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 92, background: 'rgba(0,0,0,0.58)',
        backdropFilter: 'blur(10px)', display: 'flex', alignItems: 'center',
        justifyContent: 'center', padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 640, maxWidth: 'min(640px, 94vw)', maxHeight: '88vh', overflow: 'auto',
          borderRadius: 10, border: `1px solid ${TOKENS.lineStrong}`, background: TOKENS.bg1,
          boxShadow: '0 24px 80px rgba(0,0,0,0.65)', padding: 18,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 14 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Label accent={accentColor}>Premium LLM</Label>
            <div style={{ marginTop: 6, fontFamily: TOKENS.sans, fontSize: 18, fontWeight: 560, color: TOKENS.ink0 }}>
              Choose a provider
            </div>
            <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
              Paid escalation/arbiter model — advises only, never executes.
            </div>
          </div>
          <button type="button" onClick={onClose} style={cardButtonStyle()}>Close</button>
        </div>

        {message ? <div style={{ marginBottom: 10 }}><Pill tone="info">{message}</Pill></div> : null}
        {error ? <div style={{ marginBottom: 10 }}><Pill tone="danger">{error}</Pill></div> : null}
        {!view ? (
          <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>loading providers…</div>
        ) : null}

        <div style={{ display: 'grid', gap: 6, marginBottom: 12 }}>
          {(view?.providers ?? []).map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => selectProvider(p.id)}
              style={{
                textAlign: 'left',
                border: `1px solid ${p.id === selectedId ? accentColor : TOKENS.line}`,
                borderRadius: 8,
                background: p.id === selectedId ? `${accentColor}14` : TOKENS.bg0,
                padding: '9px 11px',
                cursor: 'pointer',
                display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
              }}
            >
              <span style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 550, color: TOKENS.ink0, flex: 1 }}>
                {p.label}
              </span>
              {p.active ? <Pill size="sm" tone="profit">active</Pill> : null}
              <Pill size="sm" tone={p.configured ? 'info' : 'neutral'}>
                {p.configured ? 'configured' : 'needs key'}
              </Pill>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 9, color: TOKENS.ink3 }}>
                {p.endpoint_type}
              </span>
            </button>
          ))}
        </div>

        {selected ? (
          <div style={{
            border: `1px solid ${TOKENS.line}`, borderRadius: 8, padding: 12,
            background: TOKENS.bg0, display: 'grid', gap: 10,
          }}>
            <div style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 550, color: TOKENS.ink0 }}>
              {selected.label}
            </div>
            {credEnvs.map((env) => (
              <div key={env} style={{ display: 'grid', gap: 4 }}>
                <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{env}</span>
                <input
                  type="password"
                  placeholder={`set ${env}`}
                  value={keyValues[env] ?? ''}
                  onChange={(e) => setKeyValues((prev) => ({ ...prev, [env]: e.target.value }))}
                  style={premiumInputStyle}
                />
              </div>
            ))}
            <button
              type="button" disabled={busy !== null}
              onClick={saveCredentials}
              style={{ ...cardButtonStyle(), justifySelf: 'start', minWidth: 130 }}
            >
              {busy === 'save' ? 'saving…' : 'Save credentials'}
            </button>

            <div style={{ display: 'grid', gap: 4 }}>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>model id</span>
              <input
                type="text"
                placeholder="model id"
                value={modelInput}
                onChange={(e) => setModelInput(e.target.value)}
                style={premiumInputStyle}
              />
              {selected.suggested_models.length ? (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 2 }}>
                  {selected.suggested_models.map((sm) => (
                    <button
                      key={sm}
                      type="button"
                      onClick={() => setModelInput(sm)}
                      style={{ ...cardButtonStyle(), height: 22, fontSize: 10, padding: '0 8px' }}
                    >
                      {sm}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>

            {testResult ? (
              <div style={{
                fontFamily: TOKENS.mono, fontSize: 10, lineHeight: 1.5,
                color: testResult.ok ? TOKENS.profit : TOKENS.danger,
              }}>
                cert: {testResult.cert.reason}
                {testResult.cert.latency_ms != null ? ` · ${testResult.cert.latency_ms}ms` : ''}
              </div>
            ) : null}

            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button
                type="button" disabled={busy !== null}
                onClick={runTest}
                style={{ ...cardButtonStyle(TOKENS.info), minWidth: 90 }}
              >
                {busy === 'test' ? 'testing…' : 'Test'}
              </button>
              <button
                type="button" disabled={busy !== null}
                onClick={activate}
                style={{ ...cardButtonStyle(accentColor), minWidth: 90 }}
              >
                {busy === 'activate' ? 'activating…' : 'Activate'}
              </button>
            </div>
          </div>
        ) : (
          <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
            Select a provider above to configure, test, and activate it.
          </div>
        )}
      </div>
    </div>
  );
}

// ── D127 P6 — first-run onboarding wizard banner ─────────────────────────────

function onboardingStepTone(status: string): 'profit' | 'caution' | 'neutral' {
  if (status === 'done') return 'profit';
  if (status === 'attention') return 'caution';
  return 'neutral';
}

function OnboardingPanel({
  accentColor,
  refreshKey,
}: {
  accentColor: string;
  refreshKey: number;
}) {
  const [view, setView] = useState<import('../lib/api').OnboardingView | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.getOnboarding()
      .then((v) => { if (!cancelled) setView(v); })
      .catch(() => { /* onboarding is best-effort — silent on failure */ });
    return () => { cancelled = true; };
  }, [refreshKey]);

  if (dismissed || !view || !view.show_wizard) return null;

  const finish = async () => {
    setBusy(true);
    setError(null);
    const run = () => api.completeOnboarding();
    try {
      await run();
      setDismissed(true);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('(401)')) {
        const token = window.prompt('Control token required');
        if (token?.trim()) {
          setApiControlToken(token.trim());
          try { await run(); setDismissed(true); return; }
          catch (retry) { setError(retry instanceof Error ? retry.message : String(retry)); return; }
          finally { setBusy(false); }
        }
      }
      setError(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{
      border: `1px solid ${accentColor}55`,
      borderRadius: 10,
      background: `${accentColor}0e`,
      padding: 14,
      marginBottom: 16,
      display: 'grid',
      gap: 10,
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Label accent={accentColor}>First-run setup</Label>
          <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            {view.can_launch
              ? 'Minimum setup complete — the system can run. Finish or keep configuring.'
              : 'Connect at least one trading platform to launch the system.'}
          </div>
        </div>
        <Pill tone={view.can_launch ? 'profit' : 'caution'}>
          {view.can_launch ? 'launchable' : 'needs a broker'}
        </Pill>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {view.steps.map((s) => (
          <div
            key={s.id}
            title={s.summary}
            style={{
              border: `1px solid ${TOKENS.line}`,
              borderRadius: 7,
              background: TOKENS.bg1,
              padding: '7px 10px',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            <span style={{
              fontFamily: TOKENS.mono,
              fontSize: 12,
              color: s.status === 'done' ? TOKENS.profit
                : s.status === 'attention' ? TOKENS.caution : TOKENS.ink4,
            }}>
              {s.status === 'done' ? '✓' : s.status === 'attention' ? '!' : '○'}
            </span>
            <span style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink1 }}>{s.label}</span>
            <Pill size="sm" tone={onboardingStepTone(s.status)}>
              {s.required ? s.status : `${s.status} · optional`}
            </Pill>
          </div>
        ))}
      </div>
      {error ? <Pill tone="danger">{error}</Pill> : null}
      <div style={{ display: 'flex', gap: 8 }}>
        <button
          type="button"
          disabled={busy || !view.ready_to_finish}
          onClick={finish}
          title={view.ready_to_finish ? undefined : 'Complete the required steps first'}
          style={{
            ...cardButtonStyle(accentColor),
            minWidth: 120,
            opacity: view.ready_to_finish ? 1 : 0.5,
          }}
        >
          {busy ? 'finishing…' : 'Finish setup'}
        </button>
        <button type="button" onClick={() => setDismissed(true)} style={cardButtonStyle()}>
          Hide for now
        </button>
      </div>
    </div>
  );
}

export function ConnectScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const hub = live.connectHub;
  const categories = hub?.categories ?? {};
  const flags = hub?.capability_flags;
  const categoryOrder = ['brokers', 'information_feeds', 'ai_providers', 'treasury_accounts'];
  const [selected, setSelected] = useState<ConnectHubConnector | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [aiRefreshKey, setAiRefreshKey] = useState(0);
  const [localCatalogueOpen, setLocalCatalogueOpen] = useState(false);
  const [premiumPickerOpen, setPremiumPickerOpen] = useState(false);
  const handleAiConfigure = (stage: AiPipelineStage, connector?: ConnectHubConnector) => {
    if (stage.id === 'local_reasoning') {
      setLocalCatalogueOpen(true);
      return;
    }
    if (stage.id === 'premium_fallback') {
      setPremiumPickerOpen(true);
      return;
    }
    if (connector) setSelected(connector);
  };
  const runControl = async (
    row: ConnectHubConnector,
    fn: () => Promise<{ next_step?: string }>,
  ) => {
    setActionBusy(`${row.category}:${row.id}`);
    setActionError(null);
    setActionMessage(null);
    try {
      const res = await fn();
      setActionMessage(res.next_step || 'Saved.');
      live.refresh();
      setAiRefreshKey((k) => k + 1);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes('(401)')) {
        const token = window.prompt('Control token required');
        if (token?.trim()) {
          setApiControlToken(token.trim());
          try {
            const res = await fn();
            setActionMessage(res.next_step || 'Saved.');
            live.refresh();
            setAiRefreshKey((k) => k + 1);
            return;
          } catch (retryErr) {
            setActionError(retryErr instanceof Error ? retryErr.message : String(retryErr));
            return;
          }
        }
      }
      setActionError(msg);
    } finally {
      setActionBusy(null);
    }
  };
  const toggleAiStage = (stage: AiPipelineStage) => {
    const row = (categories['ai_providers'] ?? []).find((c) => c.id === stage.id);
    if (!row) return;
    void runControl(row, () => api.setConnectorEnabled({
      category: 'ai_providers',
      connector_id: stage.id,
      enabled: !stage.enabled,
    })).then(() => setAiRefreshKey((k) => k + 1));
  };
  const toggleConnector = (row: ConnectHubConnector) => {
    const exposureAction =
      row.category === 'brokers' && row.enabled
        ? 'block_new_only' as const
        : undefined;
    if (exposureAction) {
      const ok = window.confirm(
        `Disable ${row.label}?\n\nThis immediately blocks new routing to the broker. ` +
        `If it has open positions, use a dedicated flatten/disconnect workflow to close them before final removal.`,
      );
      if (!ok) return;
    }
    void runControl(row, () => api.setConnectorEnabled({
      category: row.category,
      connector_id: row.id,
      enabled: !row.enabled,
      exposure_action: exposureAction,
    }));
  };
  const deleteConnector = (row: ConnectHubConnector) => {
    const ok = window.confirm(
      `Delete ${row.label} from Connect Hub?\n\nCredentials in .env will be left untouched. ` +
      `For active brokers, new routing is blocked immediately and restart may be needed to fully disconnect.`,
    );
    if (!ok) return;
    void runControl(row, () => api.deleteConnector({
      category: row.category,
      connector_id: row.id,
    }));
  };
  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <OnboardingPanel accentColor={accentColor} refreshKey={aiRefreshKey} />
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <button
          type="button"
          onClick={() => setAddOpen(true)}
          style={{
            height: 32,
            borderRadius: 8,
            border: `1px solid ${accentColor}66`,
            background: `${accentColor}14`,
            color: accentColor,
            fontFamily: TOKENS.sans,
            fontSize: 12,
            fontWeight: 600,
            padding: '0 12px',
            cursor: 'pointer',
          }}
        >
          Add connector
        </button>
      </div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(4, minmax(150px, 1fr))',
        gap: 12,
        marginBottom: 16,
      }}>
        {[
          ['Trade', flags?.can_trade],
          ['Feeds', flags?.has_information_feed],
          ['AI', flags?.has_ai_provider],
          ['Treasury auto-transfer', flags?.can_auto_transfer],
        ].map(([label, ok]) => (
          <Card key={String(label)} style={{ borderRadius: 8, padding: 14 }}>
            <Label>{String(label)}</Label>
            <div style={{
              marginTop: 10,
              fontFamily: TOKENS.sans,
              fontSize: 18,
              fontWeight: 560,
              color: ok ? accentColor : TOKENS.ink3,
            }}>
              {ok ? 'available' : 'not active'}
            </div>
          </Card>
        ))}
      </div>
      {actionMessage ? <div style={{ marginBottom: 12 }}><Pill tone="profit">{actionMessage}</Pill></div> : null}
      {actionError ? <div style={{ marginBottom: 12 }}><Pill tone="danger">{actionError}</Pill></div> : null}

      {categoryOrder.map((category) => {
        const rows = (categories[category] ?? []).filter((row) => {
          if (category !== 'treasury_accounts') return true;
          return row.enabled || row.configured || row.connected;
        });
        const summary = hub?.summary?.[category];
        return (
          <section key={category} style={{ marginBottom: 18 }}>
            <div style={{
              display: 'flex',
              alignItems: 'baseline',
              justifyContent: 'space-between',
              marginBottom: 10,
              gap: 12,
            }}>
              <div>
                <Label accent={accentColor}>{CONNECT_CATEGORY_LABELS[category] ?? category}</Label>
                <div style={{ marginTop: 5, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                  {CONNECT_CATEGORY_HINTS[category] ?? 'Connector capability inventory.'}
                </div>
              </div>
              <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, whiteSpace: 'nowrap' }}>
                {summary ? `${summary.connected}/${summary.enabled} connected · ${summary.configured} configured` : 'awaiting status'}
              </div>
            </div>
            {category === 'ai_providers' ? (
              <AiPipelineGrid
                aiConnectors={categories['ai_providers'] ?? []}
                refreshKey={aiRefreshKey}
                onConfigure={handleAiConfigure}
                onToggle={toggleAiStage}
                busyKey={actionBusy}
              />
            ) : (
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 260px))',
              gap: 12,
              alignItems: 'stretch',
            }}>
              {rows.length ? rows.map((row) => (
                <ConnectorCard
                  key={`${category}-${row.id}`}
                  row={row}
                  onConfigure={setSelected}
                  onToggle={toggleConnector}
                  onDelete={deleteConnector}
                  busy={actionBusy === `${row.category}:${row.id}`}
                />
              )) : (
                <div style={{
                  border: `1px dashed ${TOKENS.lineStrong}`,
                  borderRadius: 8,
                  padding: 18,
                  color: TOKENS.ink3,
                  fontFamily: TOKENS.mono,
                  fontSize: 11,
                }}>
                  No connectors declared for this category.
                </div>
              )}
            </div>
            )}
          </section>
        );
      })}
      {selected && (
        <ConnectWizard
          connector={selected}
          accentColor={accentColor}
          onClose={() => setSelected(null)}
          onSaved={() => {
            setSelected(null);
            live.refresh();
          }}
        />
      )}
      {addOpen && (
        <AddConnectorWizard
          accentColor={accentColor}
          onClose={() => setAddOpen(false)}
          onSaved={() => {
            setAddOpen(false);
            live.refresh();
          }}
        />
      )}
      {localCatalogueOpen && (
        <LocalLlmCatalogueModal
          accentColor={accentColor}
          onClose={() => setLocalCatalogueOpen(false)}
          onChanged={() => {
            setAiRefreshKey((k) => k + 1);
            live.refresh();
          }}
        />
      )}
      {premiumPickerOpen && (
        <PremiumLlmPickerModal
          accentColor={accentColor}
          onClose={() => setPremiumPickerOpen(false)}
          onChanged={() => {
            setAiRefreshKey((k) => k + 1);
            live.refresh();
          }}
        />
      )}
    </div>
  );
}

function AddConnectorWizard({
  accentColor,
  onClose,
  onSaved,
}: {
  accentColor: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [category, setCategory] = useState('brokers');
  const [label, setLabel] = useState('');
  const [connectorId, setConnectorId] = useState('');
  const [authType, setAuthType] = useState('api_key');
  const [requiredEnv, setRequiredEnv] = useState('API_KEY,API_SECRET');
  const [controlToken, setControlToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const defaultCaps =
    category === 'brokers'
      ? { can_trade: true, can_read_balance: true }
      : category === 'information_feeds'
        ? { can_ingest_news: true }
        : category === 'ai_providers'
          ? { advisory_only: true }
          : { can_read_balance: true, can_initiate_transfer: false };
  const save = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      if (controlToken.trim()) setApiControlToken(controlToken.trim());
      const envRows = requiredEnv
        .split(',')
        .map((x) => x.trim().toUpperCase())
        .filter(Boolean);
      const res = await api.addConnector({
        category,
        connector_id: connectorId,
        label,
        auth_type: authType,
        required_env: envRows,
        capabilities: defaultCaps,
        scaffold_adapter: category === 'brokers',
      });
      setMessage(res.next_step);
      setTimeout(onSaved, 1100);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div onClick={onClose} style={{
      position: 'fixed', inset: 0, zIndex: 91, background: 'rgba(0,0,0,0.58)',
      backdropFilter: 'blur(10px)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
    }}>
      <div onClick={(e) => e.stopPropagation()} style={{
        width: 560, maxWidth: 'min(560px, 92vw)', borderRadius: 10,
        border: `1px solid ${TOKENS.lineStrong}`, background: TOKENS.bg1,
        boxShadow: '0 24px 80px rgba(0,0,0,0.65)', padding: 18,
      }}>
        <Label accent={accentColor}>Add connector</Label>
        <div style={{ marginTop: 8, marginBottom: 14, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, lineHeight: 1.5 }}>
          Adds the connector to Connect Hub. New brokers get a scaffolded adapter template; trading starts only after that adapter is implemented.
        </div>
        <div style={{ display: 'grid', gap: 10 }}>
          <select value={category} onChange={(e) => setCategory(e.target.value)} style={inputStyle()}>
            <option value="brokers">Trading platform / broker</option>
            <option value="information_feeds">News or information feed</option>
            <option value="ai_providers">AI provider</option>
            <option value="treasury_accounts">Treasury account</option>
          </select>
          <input style={inputStyle()} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Display name, e.g. Deribit" />
          <input style={inputStyle()} value={connectorId} onChange={(e) => setConnectorId(e.target.value)} placeholder="Connector id, e.g. deribit" />
          <input style={inputStyle()} value={authType} onChange={(e) => setAuthType(e.target.value)} placeholder="Auth type, e.g. api_key or oauth" />
          <input style={inputStyle()} value={requiredEnv} onChange={(e) => setRequiredEnv(e.target.value)} placeholder="Env vars, comma-separated" />
          <input style={inputStyle()} type="password" value={controlToken} onChange={(e) => setControlToken(e.target.value)} placeholder="Control token if required" />
          {message ? <Pill tone="profit">{message}</Pill> : null}
          {error ? <Pill tone="danger">{error}</Pill> : null}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <button type="button" onClick={onClose} style={secondaryButtonStyle()}>Cancel</button>
            <button type="button" onClick={save} disabled={busy || !label.trim() || !connectorId.trim()} style={primaryButtonStyle(accentColor, busy)}>
              {busy ? 'Adding...' : 'Add'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function inputStyle(): CSSProperties {
  return {
    height: 36, borderRadius: 8, border: `1px solid ${TOKENS.line}`,
    background: TOKENS.bg0, color: TOKENS.ink0, padding: '0 10px',
    fontFamily: TOKENS.mono, fontSize: 12,
  };
}

function secondaryButtonStyle(): CSSProperties {
  return {
    height: 34, borderRadius: 8, border: `1px solid ${TOKENS.line}`,
    background: 'transparent', color: TOKENS.ink2, padding: '0 12px', cursor: 'pointer',
  };
}

function primaryButtonStyle(accentColor: string, busy: boolean): CSSProperties {
  return {
    height: 34, borderRadius: 8, border: `1px solid ${accentColor}66`,
    background: `${accentColor}18`, color: accentColor, padding: '0 14px',
    cursor: busy ? 'wait' : 'pointer', fontWeight: 600,
  };
}

function cardButtonStyle(color = TOKENS.ink1): CSSProperties {
  return {
    height: 28,
    borderRadius: 8,
    border: `1px solid ${TOKENS.lineStrong}`,
    background: TOKENS.bg2,
    color,
    fontFamily: TOKENS.sans,
    fontSize: 11,
    cursor: 'pointer',
  };
}

function ConnectWizard({
  connector,
  accentColor,
  onClose,
  onSaved,
}: {
  connector: ConnectHubConnector;
  accentColor: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [controlToken, setControlToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const secretRows = connector.required_secrets ?? [];
  const hasSecrets = secretRows.length > 0;
  const save = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      if (controlToken.trim()) setApiControlToken(controlToken.trim());
      const res = await api.configureConnector({
        category: connector.category,
        connector_id: connector.id,
        secrets: values,
        enable: true,
      });
      setMessage(res.next_step || 'Saved.');
      setTimeout(onSaved, 900);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 90,
        background: 'rgba(0,0,0,0.58)',
        backdropFilter: 'blur(10px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 520,
          maxWidth: 'min(520px, 92vw)',
          maxHeight: '86vh',
          overflow: 'auto',
          borderRadius: 10,
          border: `1px solid ${TOKENS.lineStrong}`,
          background: TOKENS.bg1,
          boxShadow: '0 24px 80px rgba(0,0,0,0.65)',
          padding: 18,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 16 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Label accent={accentColor}>Connect wizard</Label>
            <div style={{ marginTop: 6, fontFamily: TOKENS.sans, fontSize: 18, fontWeight: 560, color: TOKENS.ink0 }}>
              {connector.label}
            </div>
            <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
              {connector.category} · {connector.auth_type}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            style={{
              border: `1px solid ${TOKENS.line}`,
              background: TOKENS.bg2,
              color: TOKENS.ink2,
              borderRadius: 8,
              width: 30,
              height: 30,
              cursor: 'pointer',
            }}
          >
            ×
          </button>
        </div>

        <div style={{ display: 'grid', gap: 10 }}>
          {hasSecrets ? secretRows.map((s) => (
            <label key={s.env} style={{ display: 'grid', gap: 6 }}>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                {s.label || s.env}{s.required ? '' : ' · optional'}
              </span>
              <input
                type="password"
                autoComplete="off"
                placeholder={s.configured ? 'already configured - enter to replace' : s.env}
                value={values[s.env] ?? ''}
                onChange={(e) => setValues((v) => ({ ...v, [s.env]: e.target.value }))}
                style={{
                  height: 36,
                  borderRadius: 8,
                  border: `1px solid ${TOKENS.line}`,
                  background: TOKENS.bg0,
                  color: TOKENS.ink0,
                  padding: '0 10px',
                  fontFamily: TOKENS.mono,
                  fontSize: 12,
                }}
              />
            </label>
          )) : (
            <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
              This connector does not need a secret. Saving will enable its manifest where applicable.
            </div>
          )}

          <label style={{ display: 'grid', gap: 6 }}>
            <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
              control token · only needed if API_CONTROL_TOKEN is set
            </span>
            <input
              type="password"
              autoComplete="off"
              placeholder="X-Control-Token"
              value={controlToken}
              onChange={(e) => setControlToken(e.target.value)}
              style={{
                height: 36,
                borderRadius: 8,
                border: `1px solid ${TOKENS.line}`,
                background: TOKENS.bg0,
                color: TOKENS.ink0,
                padding: '0 10px',
                fontFamily: TOKENS.mono,
                fontSize: 12,
              }}
            />
          </label>

          {connector.notes ? (
            <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 10, lineHeight: 1.5 }}>
              {connector.notes}
            </div>
          ) : null}
          {message ? <Pill tone="profit">{message}</Pill> : null}
          {error ? <Pill tone="danger">{error}</Pill> : null}

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
            <button
              type="button"
              onClick={onClose}
              style={{
                height: 34,
                borderRadius: 8,
                border: `1px solid ${TOKENS.line}`,
                background: 'transparent',
                color: TOKENS.ink2,
                padding: '0 12px',
                cursor: 'pointer',
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={save}
              style={{
                height: 34,
                borderRadius: 8,
                border: `1px solid ${accentColor}66`,
                background: `${accentColor}18`,
                color: accentColor,
                padding: '0 14px',
                cursor: busy ? 'wait' : 'pointer',
                fontWeight: 600,
              }}
            >
              {busy ? 'Saving...' : 'Save connection'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function BookScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const totalPnl = live.positions.reduce((s, p) => s + p.pnl, 0);
  const recentFills = live.orders
    .filter((o) => {
      const status = String(o.status ?? '').toLowerCase();
      const filled = Number(o.filled_quantity ?? 0);
      return status === 'filled' && Number.isFinite(filled) && Math.abs(filled) > 0;
    })
    .slice(0, 8);
  const nav = live.nav > 0 ? live.nav : 0;
  // Shared helper — same computation the dashboard's capital-allocation
  // slider uses, so the two surfaces can never disagree. Sums filled
  // positions plus reserved notional of still-open orders (see D026/D044).
  const { deployed: deployedCapital, pending: pendingCapital, working: capitalAtWorkValue } = capitalAtWork(
    live.positions,
    live.orders,
  );
  const capitalAtWorkPct = nav > 0 ? Math.max(0, capitalAtWorkValue / nav) : 0;

  const bookGridCols = 'minmax(210px, 1.5fr) 110px 80px 80px 95px minmax(100px, 1fr) 80px';

  return (
    <div style={{
      padding: 20, display: 'grid', gap: 14,
      gridTemplateColumns: '1fr 320px', height: '100%', minHeight: 0, overflow: 'hidden',
    }}>
      <div style={{
        display: 'grid',
        gridTemplateRows: 'minmax(0, 1fr) 300px',
        gap: 14,
        minHeight: 0,
        overflow: 'hidden',
      }}>
        <Card style={{
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}>
          <Label style={{ marginBottom: 12, flexShrink: 0 }}>Open positions</Label>
          {live.positions.length === 0 ? (
            <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
              No open positions
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', minHeight: 0, flex: 1, gap: 0 }}>
              <div style={{
                display: 'grid',
                gridTemplateColumns: bookGridCols,
                gap: 12, padding: '0 0 6px 0',
                fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3,
                textTransform: 'uppercase', letterSpacing: '0.06em',
                borderBottom: `1px solid ${TOKENS.line}`,
                flexShrink: 0,
              }}>
                <span>Symbol</span>
                <span>Size</span>
                <span>Avg</span>
                <span>Last</span>
                <span>P&amp;L</span>
                <span>Weight</span>
                <span style={{ textAlign: 'right' }}>Trend</span>
              </div>
              <div style={{
                flex: 1,
                minHeight: 0,
                overflowY: 'auto',
                overflowX: 'hidden',
                WebkitOverflowScrolling: 'touch',
              }}>
                <div style={{ display: 'grid', gap: 2 }}>
                  {live.positions.map((p) => (
                    <div key={`${p.broker ?? 'broker'}:${p.sym}`} style={{
                      display: 'grid',
                      gridTemplateColumns: bookGridCols,
                      gap: 12, alignItems: 'center', padding: '10px 0',
                      borderBottom: `1px solid ${TOKENS.line}`,
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                        <InstrumentAvatar pos={p} size={36} />
                        <div style={{ minWidth: 0 }}>
                          <div
                            title={p.description || instrumentDisplayName(p)}
                            style={{
                              fontFamily: TOKENS.sans,
                              fontSize: 14,
                              fontWeight: 600,
                              color: TOKENS.ink0,
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {instrumentDisplayName(p)}
                          </div>
                          <div
                            title={instrumentSubtitle(p)}
                            style={{
                              fontFamily: TOKENS.mono,
                              fontSize: 10,
                              color: TOKENS.ink3,
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {instrumentSubtitle(p)}{p.broker ? ` · ${p.broker}` : ''}
                          </div>
                        </div>
                      </div>
                      <div>
                        <div style={{ fontFamily: TOKENS.mono, fontSize: 13, color: TOKENS.ink0 }}>
                          {fmtNotional(p.notional)}
                        </div>
                        <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>notional</div>
                      </div>
                      <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>{fmtPrice(p.avg)}</span>
                      <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink1 }}>{fmtPrice(p.last)}</span>
                      <Signed value={p.pnl} size={12} />
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <div style={{ flex: 1, height: 3, background: 'rgba(255,255,255,0.04)', borderRadius: 2, overflow: 'hidden' }}>
                          <div style={{ width: `${Math.min(100, p.w * 100 * 4)}%`, height: '100%', background: accentColor }} />
                        </div>
                        <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, width: 36, textAlign: 'right' }}>
                          {(p.w * 100).toFixed(1)}%
                        </span>
                      </div>
                      <Spark values={[p.avg * 0.99 || 0, p.avg || 0, p.avg * 1.01 || 0, p.last || p.avg || 0]} width={72} height={24} accent={accentColor} />
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </Card>
        <Card style={{
          minHeight: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}>
          <Label style={{ marginBottom: 10 }}>Recent position changes</Label>
          {recentFills.length === 0 ? (
            <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>No filled orders</div>
          ) : (
            <div style={{ minHeight: 0, display: 'flex', flexDirection: 'column', fontFamily: TOKENS.mono, fontSize: 11 }}>
              <div style={{
                display: 'grid',
                gridTemplateColumns: '86px 56px 72px 72px 78px 66px minmax(0, 1fr)',
                gap: 12,
                alignItems: 'center',
                paddingBottom: 8,
                borderBottom: `1px solid ${TOKENS.line}`,
                flexShrink: 0,
              }}>
                <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 10 }}>symbol</span>
                <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 10 }}>side</span>
                <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 10 }}>qty</span>
                <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 10 }}>price</span>
                <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 10 }}>p&amp;l</span>
                <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 10 }}>fee</span>
                <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 10 }}>when</span>
              </div>
              <div style={{
                minHeight: 0,
                overflowY: 'auto',
                overflowX: 'hidden',
                WebkitOverflowScrolling: 'touch',
              }}>
                <div style={{
                  display: 'grid',
                  gridTemplateColumns: '86px 56px 72px 72px 78px 66px minmax(0, 1fr)',
                  gap: 12,
                  alignItems: 'center',
                  paddingTop: 8,
                }}>
                  {recentFills.map((o) => {
                    const side = String(o.side ?? '').toLowerCase();
                    const qty = Number(o.filled_quantity ?? o.quantity ?? 0);
                    const px = Number(o.avg_fill_price ?? o.limit_price ?? 0);
                    // ``Number(null) === 0`` so the chain below MUST short-circuit
                    // before coercion — otherwise opening trades (where the API
                    // returns ``trade_pnl_net = null``) render as "+$0", which
                    // looks like a real zero-profit close instead of "no P&L
                    // yet, this is an open". Read each candidate explicitly.
                    const pnlRaw =
                      o.trade_pnl_net ??
                      o.trade_pnl ??
                      o.realised_pnl_net ??
                      o.realised_pnl;
                    const tradePnl = pnlRaw != null ? Number(pnlRaw) : NaN;
                    const closesPosition = o.closes_position === true;
                    const feeRaw = o.trade_fee_net ?? o.fee;
                    const feeNum = feeRaw != null ? Number(feeRaw) : NaN;
                    const ts = o.timestamp ? Date.parse(o.timestamp) : NaN;
                    const sideColor = side === 'buy' ? TOKENS.profit : side === 'sell' ? TOKENS.loss : TOKENS.ink2;
                    return (
                      <Fragment key={o.id}>
                        <span title={String(o.symbol ?? '')} style={{ color: TOKENS.ink1, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {String(o.symbol ?? '').toUpperCase()}
                        </span>
                        <span style={{ color: sideColor }}>{side || 'fill'}</span>
                        <span style={{ color: TOKENS.ink2 }}>{Number.isFinite(qty) ? qty.toFixed(qty >= 100 ? 0 : 2) : '—'}</span>
                        <span style={{ color: TOKENS.ink2 }}>{fmtPrice(px)}</span>
                        {closesPosition && Number.isFinite(tradePnl) ? (
                          <Signed value={tradePnl} size={11} />
                        ) : (
                          <span
                            title={
                              !closesPosition
                                ? 'Opening trade — no realised P&L until the position is closed'
                                : 'No P&L data available for this trade'
                            }
                            style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}
                          >
                            —
                          </span>
                        )}
                        {Number.isFinite(feeNum) && feeNum > 0 ? (
                          <span
                            title={`Transaction fee: ${feeNum.toFixed(4)}`}
                            style={{ color: TOKENS.ink2, fontFamily: TOKENS.mono, fontSize: 11 }}
                          >
                            {feeNum.toFixed(2)}
                          </span>
                        ) : (
                          <span style={{ color: TOKENS.ink3 }}>—</span>
                        )}
                        <span title={o.timestamp ?? undefined} style={{ color: TOKENS.ink3, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {Number.isFinite(ts) ? `${minutesAgo(ts)} · ${o.broker ?? ''}` : o.broker ?? ''}
                        </span>
                      </Fragment>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
        </Card>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minHeight: 0, overflowY: 'auto' }}>
        <Card>
          <Label style={{ marginBottom: 10 }}>Totals</Label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div>
              <Label style={{ marginBottom: 2 }}>Unrealised</Label>
              <div style={{
                fontFamily: TOKENS.sans, fontSize: 24, fontWeight: 300,
                color: totalPnl >= 0 ? TOKENS.profit : TOKENS.loss,
                letterSpacing: '-0.02em',
              }}>
                {totalPnl >= 0 ? '+' : '−'}{CURRENCY_SYMBOL}{Math.abs(totalPnl).toFixed(2)}
              </div>
            </div>
            <div style={{ borderTop: `1px solid ${TOKENS.line}`, paddingTop: 10 }}>
              <Label style={{ marginBottom: 6 }}>Exposure</Label>
              {(
                [
                  ['gross', live.exposure.gross],
                  ['net', live.exposure.net],
                  ['gross free', live.exposure.cash],
                ] as const
              ).map(([k, v]) => (
                <div key={k} style={{
                  display: 'flex', justifyContent: 'space-between',
                  padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
                }}>
                  <span style={{ color: TOKENS.ink3 }}>{k}</span>
                  <span style={{ color: TOKENS.ink1 }}>{(v * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
            <div style={{ borderTop: `1px solid ${TOKENS.line}`, paddingTop: 10 }}>
              <Label style={{ marginBottom: 6 }}>Capital at work</Label>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
              }}>
                <span style={{ color: TOKENS.ink3 }}>deployed</span>
                <span style={{ color: TOKENS.ink1 }}>{CURRENCY_SYMBOL}{deployedCapital.toFixed(2)}</span>
              </div>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
              }}>
                <span style={{ color: TOKENS.ink3 }}>pending orders</span>
                <span style={{ color: TOKENS.ink1 }}>{CURRENCY_SYMBOL}{pendingCapital.toFixed(2)}</span>
              </div>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
              }}>
                <span style={{ color: TOKENS.ink3 }}>total working</span>
                <span style={{ color: TOKENS.ink1 }}>
                  {CURRENCY_SYMBOL}{capitalAtWorkValue.toFixed(2)} ({(capitalAtWorkPct * 100).toFixed(1)}%)
                </span>
              </div>
            </div>
          </div>
        </Card>
        <Card>
          <Label style={{ marginBottom: 10 }}>Brokers</Label>
          {live.brokers.length === 0 ? (
            <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>No brokers configured</div>
          ) : (
            live.brokers.map((b) => {
              // Distinct pill tone per broker state so an operator can tell
              // at a glance whether a broker is live, transiently warming,
              // or in a user-actionable failure (offline). The ``title``
              // surfaces the backend's concrete error without crowding the
              // card — same pattern the status bar uses for kill-switch
              // tooltips.
              const tone: 'profit' | 'caution' | 'danger' | 'neutral' =
                b.state === 'live' ? 'profit' :
                b.state === 'warming' ? 'caution' :
                b.state === 'offline' ? 'danger' :
                'neutral';
              const title = b.error
                ? `${b.name}: ${b.error}`
                : b.excluded
                  ? `${b.name} is excluded from NAV`
                  : b.name;
              return (
                <div
                  key={b.name}
                  title={title}
                  style={{
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '6px 0', fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2,
                    cursor: b.error ? 'help' : 'default',
                  }}
                >
                  <span>{b.name}</span>
                  <Pill size="sm" tone={tone}>{b.state}</Pill>
                </div>
              );
            })
          )}
        </Card>
        <Card>
          <Label style={{ marginBottom: 10 }}>News & data</Label>
          {live.newsDataProviders.length === 0 ? (
            <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
              No provider status from API
            </div>
          ) : (
            live.newsDataProviders.map((p) => {
              const tone: 'profit' | 'caution' | 'danger' | 'neutral' =
                p.state === 'live' ? 'profit' :
                p.state === 'stale' ? 'caution' :
                p.state === 'error' ? 'danger' :
                'neutral';
              const pillText = p.state;
              const title = p.error?.trim()
                ? `${p.label}: ${p.error.trim()}`
                : [p.ageLabel, p.lastIngestAt ? `ingest ${p.lastIngestAt}` : ''].filter(Boolean).join(' · ') || p.label;
              return (
                <div
                  key={p.id}
                  title={title}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    gap: 8,
                    padding: '6px 0',
                    fontFamily: TOKENS.mono,
                    fontSize: 11,
                    color: p.configured ? TOKENS.ink2 : TOKENS.ink3,
                    cursor: p.error ? 'help' : 'default',
                  }}
                >
                  <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>{p.label}</span>
                  <span
                    style={{
                      flex: 1,
                      textAlign: 'right',
                      color: TOKENS.ink3,
                      fontSize: 10,
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {p.ageLabel}
                  </span>
                  <Pill size="sm" tone={tone}>{pillText}</Pill>
                </div>
              );
            })
          )}
        </Card>
      </div>
    </div>
  );
}

function fmtPrice(v: number): string {
  if (!Number.isFinite(v) || v === 0) return '—';
  return v >= 100 ? v.toFixed(2) : v.toFixed(4);
}

/** Compact account-currency formatter for position notionals. Uses ``k`` /
 *  ``M`` suffixes above 1k / 1M so the Book row stays one line on narrow
 *  cards while still exposing exact cents for small positions. */
function fmtNotional(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return '—';
  if (v >= 1_000_000) return `${CURRENCY_SYMBOL}${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 10_000) return `${CURRENCY_SYMBOL}${(v / 1_000).toFixed(1)}k`;
  if (v >= 1_000) return `${CURRENCY_SYMBOL}${(v / 1_000).toFixed(2)}k`;
  return `${CURRENCY_SYMBOL}${v.toFixed(2)}`;
}

export function RiskScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const { approved, rejected, executionRejections } = live;

  const gauges = useMemo(() => {
    const portfolio = (live.snapshot?.portfolio ?? {}) as Record<string, unknown>;
    const nav = typeof portfolio.nav === 'string' || typeof portfolio.nav === 'number'
      ? Number(portfolio.nav) || 0 : 0;
    const gross = numFromPortfolio(portfolio.gross_exposure, nav);
    const net = Math.abs(numFromPortfolio(portfolio.net_exposure, nav));
    const maxPosCount = live.positions.length;
    const positionHeat = Math.min(1, maxPosCount / 20);
    const drawdown = numFromPortfolio(live.pnl?.metrics?.max_drawdown_pct);
    const orderRate = Math.min(1, (live.orders?.length ?? 0) / 50);
    const gauges: Array<{ name: string; v: number; cap: number; tone: 'profit' | 'caution' | 'danger' }> = [
      { name: 'Max drawdown',    v: Math.min(1, drawdown / 50), cap: 1, tone: drawdown > 20 ? 'danger' : drawdown > 10 ? 'caution' : 'profit' },
      { name: 'Position heat',   v: positionHeat, cap: 1, tone: positionHeat > 0.85 ? 'danger' : positionHeat > 0.6 ? 'caution' : 'profit' },
      { name: 'Gross exposure',  v: gross, cap: 1, tone: gross > 0.9 ? 'danger' : gross > 0.7 ? 'caution' : 'profit' },
      { name: 'Net exposure',    v: net, cap: 1, tone: net > 0.85 ? 'danger' : net > 0.6 ? 'caution' : 'profit' },
      { name: 'Order rate',      v: orderRate, cap: 1, tone: orderRate > 0.85 ? 'caution' : 'profit' },
    ];
    return gauges;
  }, [live.snapshot, live.positions, live.orders, live.pnl]);

  const demandDiag = useMemo(() => {
    const d = (live.snapshot?.demand ?? {}) as Record<string, unknown>;
    const rt = (live.runtimeDemand ?? {}) as Record<string, unknown>;
    const score = Number(d.score ?? rt.score ?? 0) || 0;
    const trend = String(d.trend ?? rt.trend ?? 'flat');
    const conf = Number(d.confidence ?? rt.confidence ?? 0) || 0;
    const vol = Number(d.market_volatility ?? (rt.components as Record<string, unknown> | undefined)?.market_volatility ?? 0) || 0;
    const history = Array.isArray(d.alert_history) ? d.alert_history : Array.isArray(rt.alert_history) ? rt.alert_history : [];
    return { score, trend, conf, vol, history };
  }, [live.snapshot, live.runtimeDemand]);

  const metaDiag = useMemo(() => {
    const rt = (live.runtimeMetaLabeling ?? {}) as Record<string, unknown>;
    const dyn = (rt.dynamic_bias && typeof rt.dynamic_bias === 'object') ? (rt.dynamic_bias as Record<string, unknown>) : {};
    const diag = (rt.diagnostics && typeof rt.diagnostics === 'object') ? (rt.diagnostics as Record<string, unknown>) : {};
    return { dyn, diag };
  }, [live.runtimeMetaLabeling]);

  const routingDiag = useMemo(() => {
    const rq = live.routingQuality;
    const qmap = (rq?.quality_map ?? {}) as Record<string, Record<string, number>>;
    const qstats = (rq?.quality_stats ?? {}) as Record<
      string,
      Record<string, { n: number; std: number; ci95_half: number; fused_score?: number }>
    >;
    const hist = (rq?.history ?? {}) as Record<string, Array<{ ts: string; broker: string; score: number }>>;
    const rows = Object.entries(qmap)
      .map(([sym, by]) => {
        const best = Object.entries(by).sort((a, b) => {
          const rowA = qstats[sym]?.[a[0]];
          const rowB = qstats[sym]?.[b[0]];
          const fa = typeof rowA?.fused_score === 'number' && Number.isFinite(rowA.fused_score) ? rowA.fused_score : a[1];
          const fb = typeof rowB?.fused_score === 'number' && Number.isFinite(rowB.fused_score) ? rowB.fused_score : b[1];
          return fb - fa;
        })[0];
        const bestBroker = best?.[0] ?? '—';
        const seriesRaw = (Array.isArray(hist[sym]) ? hist[sym] : [])
          .filter((x) => x.broker === bestBroker)
          .slice(-16);
        const series = seriesRaw.map((x) => Number(x.score) || 0);
        const stat = (qstats[sym] && qstats[sym][bestBroker]) ? qstats[sym][bestBroker] : null;
        const fused = typeof stat?.fused_score === 'number' && Number.isFinite(stat.fused_score)
          ? stat.fused_score
          : (Number.isFinite(best?.[1] ?? NaN) ? Number(best?.[1]) : 0);
        return {
          sym,
          bestBroker,
          bestScore: fused,
          points: Array.isArray(hist[sym]) ? hist[sym].length : 0,
          series,
          ci95: stat?.ci95_half ?? 0,
          n: stat?.n ?? 0,
        };
      })
      .sort((a, b) => b.bestScore - a.bestScore)
      .slice(0, 6);
    return { rows, updatedAt: rq?.updated_at ?? null };
  }, [live.routingQuality]);

  const routingBrokerTable = useMemo(() => {
    const raw = live.routingQuality?.broker_comparison;
    if (!Array.isArray(raw) || raw.length === 0) return [];
    return (raw as RoutingBrokerRow[]).slice(0, 32);
  }, [live.routingQuality]);

  return (
    <div style={{
      padding: 20, display: 'grid', gap: 14,
      gridTemplateColumns: '1fr 1fr', height: '100%', overflow: 'auto',
    }}>
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label accent={accentColor}>Approved · {approved.length}</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>last batch</span>
        </div>
        {approved.length === 0 ? (
          <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No approved signals yet
          </div>
        ) : approved.map((a, i) => (
          <div key={`${a.sym}-${i}`} style={{ padding: '10px 0', borderBottom: `1px solid ${TOKENS.line}` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span title={a.sym} style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{prettySymbol(a.sym) || '—'}</span>
                <Pill size="sm" tone="neutral">{a.side}</Pill>
              </div>
              <Pill size="sm" tone="profit">approved</Pill>
            </div>
            <div style={{
              display: 'flex', gap: 14, marginTop: 6,
              fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3,
            }}>
              <span>conf {a.conf.toFixed(1)}%</span>
              <span>quality {a.q.toFixed(2)}</span>
            </div>
          </div>
        ))}
      </Card>
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label style={{ color: TOKENS.loss }}>Rejected · {rejected.length}</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>last batch</span>
        </div>
        {rejected.length === 0 ? (
          <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11, lineHeight: 1.5 }}>
            No rejections
            <div style={{ marginTop: 4, color: TOKENS.ink4, fontSize: 10 }}>
              risk engine approved every recent signal
            </div>
          </div>
        ) : rejected.map((r, i) => (
          <div key={`${r.sym}-${i}`} style={{ padding: '10px 0', borderBottom: `1px solid ${TOKENS.line}` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span title={r.sym} style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{prettySymbol(r.sym) || '—'}</span>
                <Pill size="sm" tone="loss">{r.side}</Pill>
              </div>
              <Pill size="sm" tone="danger">blocked</Pill>
            </div>
            <div style={{ marginTop: 6, color: TOKENS.ink2, fontSize: 12, lineHeight: 1.4 }}>{r.explain}</div>
            <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.loss }}>{r.reason}</div>
          </div>
        ))}
      </Card>
      <Card style={{ gridColumn: '1 / -1' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label style={{ color: TOKENS.caution }}>
            Execution rejections · {executionRejections.length}
          </Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            broker-side · post risk-gate
          </span>
        </div>
        {executionRejections.length === 0 ? (
          <div style={{ padding: 12, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No execution rejections — every approved order made it to the broker
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 8 }}>
            {executionRejections.map((x) => (
              <div
                key={`${x.broker}-${x.sym}-${x.t}`}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '140px 80px 90px 1fr 140px',
                  alignItems: 'center',
                  gap: 12,
                  padding: '8px 10px',
                  background: 'rgba(255,255,255,0.02)',
                  border: `1px solid ${TOKENS.line}`,
                  borderRadius: 6,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span title={x.sym} style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, color: TOKENS.ink0 }}>
                    {prettySymbol(x.sym)}
                  </span>
                  <Pill size="sm" tone={x.side === 'long' ? 'profit' : 'loss'}>{x.side}</Pill>
                </div>
                <Pill size="sm" tone={x.status === 'rejected' ? 'danger' : 'caution'}>
                  {x.status}
                </Pill>
                <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>
                  {x.broker || '—'}
                </span>
                <span
                  style={{
                    fontFamily: TOKENS.mono,
                    fontSize: 11,
                    color: x.reason ? TOKENS.caution : TOKENS.ink3,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={x.reason ?? ''}
                >
                  {x.reason ?? '(no reason recorded — see backend log)'}
                </span>
                <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, textAlign: 'right' }}>
                  {formatRelativeTime(x.t)}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card style={{ gridColumn: '1 / -1' }}>
        <Label style={{ marginBottom: 12 }}>Risk gauges</Label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 18 }}>
          {gauges.map((g) => {
            const color = g.tone === 'danger' ? TOKENS.danger : g.tone === 'caution' ? TOKENS.caution : accentColor;
            const textColor = g.tone === 'danger' ? TOKENS.danger : g.tone === 'caution' ? TOKENS.caution : TOKENS.profit;
            return (
              <div key={g.name}>
                <div style={{
                  display: 'flex', justifyContent: 'space-between',
                  fontFamily: TOKENS.mono, fontSize: 11, marginBottom: 4,
                }}>
                  <span style={{ color: TOKENS.ink2 }}>{g.name}</span>
                  <span style={{ color: textColor }}>{(g.v * 100).toFixed(0)}%</span>
                </div>
                <div style={{ height: 4, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{
                    width: `${g.v * 100}%`, height: '100%',
                    background: color,
                    transition: `width 600ms ${TOKENS.ease}`,
                  }} />
                </div>
              </div>
            );
          })}
        </div>
      </Card>
      <Card style={{ gridColumn: '1 / -1' }}>
        <Label style={{ marginBottom: 10 }}>Demand & meta diagnostics</Label>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }}>
          <div style={{ border: `1px solid ${TOKENS.line}`, borderRadius: 8, padding: 12 }}>
            <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, marginBottom: 8 }}>
              demand score {demandDiag.score.toFixed(2)} · {demandDiag.trend} · conf {(demandDiag.conf * 100).toFixed(0)}%
            </div>
            <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3, marginBottom: 8 }}>
              market vol {(demandDiag.vol * 100).toFixed(2)}%
            </div>
            <div style={{ display: 'grid', gap: 6 }}>
              {demandDiag.history.slice(-4).reverse().map((h, i) => (
                <div key={i} style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                  {String((h as Record<string, unknown>).at ?? 'n/a')} · {String((h as Record<string, unknown>).trend ?? 'flat')} · {String((h as Record<string, unknown>).score ?? '0')}
                </div>
              ))}
              {demandDiag.history.length === 0 && (
                <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4 }}>no demand alerts yet</div>
              )}
            </div>
          </div>
          <div style={{ border: `1px solid ${TOKENS.line}`, borderRadius: 8, padding: 12 }}>
            <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, marginBottom: 8 }}>
              meta adaptation
            </div>
            <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, marginBottom: 8 }}>
              rows {Number((metaDiag.diag.rows as number) ?? 0)} · lookback {Number((metaDiag.diag.lookback_hours as number) ?? 0)}h
            </div>
            <div style={{ display: 'grid', gap: 6 }}>
              {Object.entries(metaDiag.dyn).slice(0, 5).map(([k, v]) => (
                <div key={k} style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                  {k}: {(Number(v) || 0).toFixed(3)}
                </div>
              ))}
              {Object.keys(metaDiag.dyn).length === 0 && (
                <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4 }}>no adaptive priors yet</div>
              )}
            </div>
          </div>
          <div style={{ border: `1px solid ${TOKENS.line}`, borderRadius: 8, padding: 12 }}>
            <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, marginBottom: 8 }}>
              routing quality {routingDiag.updatedAt ? '· persisted' : ''}
            </div>
            <div style={{ display: 'grid', gap: 6 }}>
              {routingDiag.rows.map((r) => (
                <div key={r.sym} style={{ display: 'grid', gridTemplateColumns: '1fr 72px', gap: 8, alignItems: 'center' }}>
                  <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                    {prettySymbol(r.sym)} → {r.bestBroker} ({r.bestScore.toFixed(3)})
                    {' · '}
                    CI95 +/-{(Number(r.ci95) || 0).toFixed(3)}
                    {' · n='}
                    {Math.round(Number(r.n) || 0)}
                  </div>
                  {r.series.length >= 2 ? (
                    <Spark values={r.series} width={72} height={20} accent={TOKENS.info} area={false} />
                  ) : (
                    <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4, textAlign: 'right' }}>
                      {r.points} pts
                    </div>
                  )}
                </div>
              ))}
              {routingDiag.rows.length === 0 && (
                <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4 }}>
                  no routing quality data yet
                </div>
              )}
            </div>
          </div>
        </div>
        {routingBrokerTable.length > 0 && (
          <div style={{
            marginTop: 14,
            borderTop: `1px solid ${TOKENS.line}`,
            paddingTop: 12,
            overflowX: 'auto',
          }}
          >
            <div style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2, marginBottom: 8 }}>
              Broker comparison (fused score · fee prior · CI95 · slip p50/p90 · fills)
            </div>
            <table style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontFamily: TOKENS.mono,
              fontSize: 10,
              color: TOKENS.ink3,
            }}
            >
              <thead>
                <tr style={{ color: TOKENS.ink2, textAlign: 'left' }}>
                  <th style={{ padding: '4px 8px 4px 0' }}>symbol</th>
                  <th style={{ padding: '4px 8px' }}>broker</th>
                  <th style={{ padding: '4px 8px' }}>fused</th>
                  <th style={{ padding: '4px 8px' }}>learned</th>
                  <th style={{ padding: '4px 8px' }}>prior</th>
                  <th style={{ padding: '4px 8px' }}>CI±</th>
                  <th style={{ padding: '4px 8px' }}>n</th>
                  <th style={{ padding: '4px 8px' }}>p50 slip</th>
                  <th style={{ padding: '4px 8px' }}>p90 slip</th>
                  <th style={{ padding: '4px 8px' }}>fill%</th>
                </tr>
              </thead>
              <tbody>
                {routingBrokerTable.map((row, idx) => (
                  <tr key={`${row.symbol}-${row.broker}-${idx}`} style={{ borderTop: `1px solid ${TOKENS.line}` }}>
                    <td style={{ padding: '6px 8px 6px 0', color: TOKENS.ink0 }}>{prettySymbol(row.symbol)}</td>
                    <td style={{ padding: '6px 8px' }}>{row.broker}</td>
                    <td style={{ padding: '6px 8px' }}>{Number(row.fused_score).toFixed(3)}</td>
                    <td style={{ padding: '6px 8px' }}>{Number(row.learned_score).toFixed(3)}</td>
                    <td style={{ padding: '6px 8px' }}>{Number(row.fee_prior).toFixed(3)}</td>
                    <td style={{ padding: '6px 8px' }}>{Number(row.ci95_half).toFixed(3)}</td>
                    <td style={{ padding: '6px 8px' }}>{Math.round(Number(row.n) || 0)}</td>
                    <td style={{ padding: '6px 8px' }}>{Number(row.p50_slippage_bps).toFixed(2)}</td>
                    <td style={{ padding: '6px 8px' }}>{Number(row.p90_slippage_bps).toFixed(2)}</td>
                    <td style={{ padding: '6px 8px' }}>{(Number(row.fill_rate) * 100).toFixed(0)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function numFromPortfolio(raw: unknown, nav = 0): number {
  if (raw == null || raw === '') return 0;
  const n = typeof raw === 'number' ? raw : parseFloat(String(raw));
  if (!Number.isFinite(n)) return 0;
  const a = Math.abs(n);
  if (a <= 1) return Math.max(0, Math.min(1, a));
  if (a <= 100) return Math.max(0, Math.min(1, a / 100));
  if (nav > 0) return Math.max(0, Math.min(1, a / nav));
  return 0;
}

function formatRelativeTime(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '—';
  const delta = Date.now() - ms;
  if (delta < 0) return 'just now';
  const s = Math.floor(delta / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export function StrategiesScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const rows = live.strategies;
  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>Strategy mix</Label>
      {rows.length === 0 ? (
        <Card>
          <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No strategies to display
          </div>
        </Card>
      ) : (
        <div style={{
          display: 'grid',
          gap: 14,
          gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
        }}
        >
          {rows.map((s) => {
            const trace = s.sparkValues && s.sparkValues.length >= 2 ? s.sparkValues : null;
            const mix = s.mix;
            const rosterIdle = mix
              ? mix.evaluated === 0
              : (!!s.idle || (s.weight === 0 && s.trades === 0));
            const kind = String(s.kind ?? 'signal');
            const isArb = kind === 'arbitrage';
            const runtimeLoaded = s.runtimeLoaded !== false;
            const kindLabel = kind === 'relative_value' ? 'relative value' : kind.replace(/_/g, ' ');
            const showSpark = trace != null || !rosterIdle;
            const synthSpark =
              !trace && !rosterIdle
                ? Array.from({ length: 12 }, (_, i) => Math.max(0.02, s.weight) * (1 + 0.04 * Math.sin(i * 0.55)))
                : null;
            const confPct = (v: number) =>
              v >= 0 && v <= 1 ? `${(v * 100).toFixed(0)}%` : (Number.isFinite(v) ? v.toFixed(2) : '—');
            const mixTime = (iso: string | null) => {
              if (!iso) return '—';
              const t = Date.parse(iso);
              return Number.isFinite(t) ? formatRelativeTime(t) : '—';
            };
            const lifecycleTone = (() => {
              if (!mix) return 'neutral' as const;
              if (mix.lifecycle === 'trading') return 'profit' as const;
              if (mix.lifecycle === 'blocked_by_risk') return 'danger' as const;
              if (mix.lifecycle === 'competing' || mix.lifecycle === 'selected') return 'caution' as const;
              return 'neutral' as const;
            })();
            return (
              <Card key={s.name} style={rosterIdle ? { opacity: 0.78 } : undefined}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 8 }}>
                  <span
                    title={s.name}
                    style={{
                      fontFamily: TOKENS.sans, fontSize: 15, fontWeight: 500,
                      color: TOKENS.ink0, letterSpacing: '-0.02em',
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}
                  >{formatStrategyDisplayName(s.name)}</span>
                  <div style={{ display: 'flex', gap: 6, flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                    {kind !== 'signal' && <Pill tone="neutral">{kindLabel}</Pill>}
                    {!runtimeLoaded && <Pill tone="neutral">configured</Pill>}
                    {s.enabled === false && <Pill tone="loss">disabled</Pill>}
                    {mix ? (
                      <Pill tone={lifecycleTone}>{mix.lifecycleDisplay}</Pill>
                    ) : rosterIdle ? (
                      <Pill tone="neutral">idle</Pill>
                    ) : (
                      <Pill tone="neutral">mix {(s.weight * 100).toFixed(0)}%</Pill>
                    )}
                  </div>
                </div>
                {showSpark ? (
                  <Spark
                    values={trace ?? synthSpark!}
                    width={280}
                    height={56}
                    accent={accentColor}
                  />
                ) : (
                  <div style={{
                    height: 56, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11,
                    borderRadius: 6, background: 'rgba(255,255,255,0.02)',
                    textAlign: 'center',
                    padding: '0 8px',
                  }}>
                    {isArb
                      ? 'Awaiting spread / funding opportunity'
                      : !runtimeLoaded
                        ? 'Configured research module · not loaded by trading loop'
                      : mix && mix.evaluated > 0
                        ? 'Activity from strategy_candidate_log (see metrics below)'
                        : 'No recent signals in DB window · strategy registered'}
                  </div>
                )}
                {mix && (
                  <div style={{
                    marginTop: 10,
                    padding: '10px 0 0 0',
                    borderTop: `1px solid ${TOKENS.line}`,
                    fontFamily: TOKENS.mono,
                    fontSize: 10,
                    color: TOKENS.ink2,
                    lineHeight: 1.55,
                  }}
                  >
                    <div style={{ marginBottom: 6, color: TOKENS.ink3, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                      24h observability
                    </div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '4px 12px' }}>
                      <span>Evaluated</span><span style={{ color: TOKENS.ink0 }}>{mix.evaluated}</span>
                      <span>Generated</span><span style={{ color: TOKENS.ink0 }}>{mix.counts.generated}</span>
                      <span>Filtered</span><span style={{ color: TOKENS.ink0 }}>{mix.filtered}</span>
                      <span>Lost (peer)</span><span style={{ color: TOKENS.ink0 }}>{mix.counts.lost_to_strategy}</span>
                      <span>Selected</span><span style={{ color: TOKENS.ink0 }}>{mix.counts.selected_for_allocation}</span>
                      <span>Risk ↯</span><span style={{ color: TOKENS.ink0 }}>{mix.counts.risk_rejected}</span>
                      <span>Executed</span><span style={{ color: TOKENS.ink0 }}>{mix.counts.executed}</span>
                      <span>Exec gap</span><span style={{ color: TOKENS.ink0 }}>{mix.counts.execution_incomplete ?? 0}</span>
                      <span>Skipped</span><span style={{ color: TOKENS.ink0 }}>{mix.counts.skipped}</span>
                    </div>
                    <div style={{ marginTop: 8, display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '4px 12px' }}>
                      <span>Last seen</span><span style={{ color: TOKENS.ink0 }}>{mixTime(mix.lastEvaluatedAt)}</span>
                      <span>Last candidate</span><span style={{ color: TOKENS.ink0 }}>{mixTime(mix.lastGeneratedAt)}</span>
                    </div>
                    {mix.blockerHint && (
                      <div style={{ marginTop: 8, color: TOKENS.ink0, lineHeight: 1.45 }}>
                        <span style={{ color: TOKENS.ink3 }}>Focus · </span>
                        {mix.blockerHint}
                      </div>
                    )}
                    {mix.topFailedConditions && mix.topFailedConditions.length > 0 && (
                      <div style={{ marginTop: 6, color: TOKENS.ink2, fontSize: 9, lineHeight: 1.45 }}>
                        <span style={{ color: TOKENS.ink3 }}>No-setup signals · </span>
                        {mix.topFailedConditions.slice(0, 2).map((f) => (
                          <span key={f.key} style={{ display: 'block' }}>
                            {f.label} ({f.count}×)
                          </span>
                        ))}
                      </div>
                    )}
                    {mix.topRiskRejectionReasons && mix.topRiskRejectionReasons.length > 0 && mix.counts.risk_rejected > 0 && (
                      <div style={{ marginTop: 6, color: TOKENS.ink2, fontSize: 9, lineHeight: 1.45 }}>
                        <span style={{ color: TOKENS.ink3 }}>Risk · </span>
                        {mix.topRiskRejectionReasons.slice(0, 2).map((f) => (
                          <span key={f.reason} style={{ display: 'block' }}>
                            {f.reason} ({f.count}×)
                          </span>
                        ))}
                      </div>
                    )}
                    {mix.topExecutionIncomplete && mix.topExecutionIncomplete.length > 0 && (mix.counts.execution_incomplete ?? 0) > 0 && (
                      <div style={{ marginTop: 6, color: TOKENS.ink2, fontSize: 9, lineHeight: 1.45 }}>
                        <span style={{ color: TOKENS.ink3 }}>Post-risk exec · </span>
                        {mix.topExecutionIncomplete.slice(0, 2).map((f) => (
                          <span key={f.reason} style={{ display: 'block' }}>
                            {f.reason} ({f.count}×)
                          </span>
                        ))}
                      </div>
                    )}
                    {mix.topSkipReason && (
                      <div style={{ marginTop: 8 }}>
                        <span style={{ color: TOKENS.ink3 }}>Top skip reason · </span>
                        <span style={{ color: TOKENS.ink0 }}>{mix.topSkipReason}</span>
                      </div>
                    )}
                  </div>
                )}
                <div style={{
                  display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 12,
                  marginTop: 12, paddingTop: 12, borderTop: `1px solid ${TOKENS.line}`,
                }}>
                  <StratStat label="Conf" value={confPct(s.sharpe)} />
                  <StratStat label="Avg opp conf" value={confPct(s.winRate)} />
                  <StratStat label="Opps" value={String(s.trades)} />
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

function StratStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <Label style={{ marginBottom: 2 }}>{label}</Label>
      <span style={{ fontFamily: TOKENS.sans, fontSize: 16, fontWeight: 400, color: TOKENS.ink0 }}>{value}</span>
    </div>
  );
}

export function TradeLogScreen({ live }: { live: LiveData }) {
  const rows = useMemo(() => mapOrdersToTradeLog(live.orders), [live.orders]);
  const totalToday = rows.length;

  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>Trade log · {totalToday} events</Label>
      <Card noPad>
        {rows.length === 0 ? (
          <div style={{ padding: 40, textAlign: 'center', color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No orders yet
          </div>
        ) : rows.map((r, i) => (
          <div key={`${r.t}-${i}`} style={{
            padding: '12px 18px',
            borderBottom: i < rows.length - 1 ? `1px solid ${TOKENS.line}` : 'none',
            display: 'flex', alignItems: 'center', gap: 16,
            fontFamily: TOKENS.mono, fontSize: 11,
          }}>
            <span style={{ color: TOKENS.ink3, width: 130 }}>{r.t}</span>
            <Pill size="sm" tone={r.ok === true ? 'profit' : r.ok === false ? 'danger' : 'neutral'}>{r.kind}</Pill>
            {r.sym && <span title={r.sym} style={{ color: TOKENS.ink0, fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, width: 60 }}>{prettySymbol(r.sym)}</span>}
            {r.side && <span style={{ color: TOKENS.ink2, width: 44 }}>{r.side}</span>}
            {r.qty !== undefined && Number.isFinite(r.qty) && (
              <span style={{ color: TOKENS.ink1 }}>
                {r.qty} {r.price ? `@ ${r.price}` : ''}
              </span>
            )}
            {r.reason && <span style={{ color: TOKENS.loss }}>{r.reason}</span>}
            <span style={{ flex: 1 }} />
            {r.venue && <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 9 }}>{r.venue}</span>}
          </div>
        ))}
      </Card>
    </div>
  );
}
