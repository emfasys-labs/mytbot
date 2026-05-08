/**
 * Dashboard screen — the main cockpit.
 * Wired to live system via `LiveData` from useLiveSystem().
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { CapitalPanel } from './capital';
import { Conviction, Coverage, LiveEvent, Position } from './data';
import { prettySymbol } from './mapping';
import { Card, Glyph, Label, NavNumber, Pill, Signed, Spark } from './primitives';
import { ACCENTS, AccentName, CURRENCY_SYMBOL, Density, SystemState, TOKENS } from './tokens';
import type { BackendSystemState } from '../lib/api';
import type { LiveData } from './useLiveSystem';

export function DashboardScreen({
  state, accent, density, live,
}: {
  state: SystemState;
  accent: AccentName;
  density: Density;
  onArm?: (v: boolean) => void;
  armed?: boolean;
  live: LiveData;
}) {
  const [newSignal, setNewSignal] = useState<{ sym: string; score: number; t: number } | null>(null);
  const [milestoneFlash, setMilestoneFlash] = useState(false);
  const accentColor = ACCENTS[accent].main;
  const accentGlow = ACCENTS[accent].glow;

  // Flash on a fresh incoming signal event from the live feed.
  const lastEventRef = useRef<string | null>(null);
  useEffect(() => {
    const latest = live.events[0];
    if (!latest || latest.kind !== 'signal') return;
    const key = `${latest.t}:${latest.text}`;
    if (key === lastEventRef.current) return;
    lastEventRef.current = key;
    const match = latest.text.match(/^([A-Z0-9.\-]+)\s+(long|short)\s+([0-9.]+)/i);
    if (!match) return;
    const sym = match[1].toUpperCase();
    const score = parseFloat(match[3]);
    if (!Number.isFinite(score)) return;
    setNewSignal({ sym, score, t: Date.now() });
    const timeout = setTimeout(() => setNewSignal(null), 2400);
    return () => clearTimeout(timeout);
  }, [live.events]);

  // Milestone glow when NAV breaks peak.
  const navValue = live.nav;
  const navPeak = live.navPeak;
  useEffect(() => {
    if (navValue > navPeak && navValue > 0) {
      setMilestoneFlash(true);
      const t = setTimeout(() => setMilestoneFlash(false), 2000);
      return () => clearTimeout(t);
    }
  }, [navValue, navPeak]);

  const pad = density === 'compact' ? 12 : 20;
  const gap = density === 'compact' ? 10 : 14;
  const dayChange = Number.isFinite(live.pnlRollups.d) ? live.pnlRollups.d : navValue - live.navOpen;
  const dayPct = live.navOpen > 0 ? (dayChange / live.navOpen) * 100 : 0;

  const topConviction = live.conviction[0];
  const tradable = live.tradableCapital;
  const navPending = state !== 'off' && !live.navReady;

  return (
    <div style={{
      padding: pad, display: 'grid', gap,
      gridTemplateColumns: 'minmax(0,1fr) 320px',
      // Row order: hero NAV · capital allocation · conviction+live-feed · book.
      // The capital row gets its own full-width band directly under NAV so
      // the "deployed · free" figures stay visually tied to the account
      // number above; `auto` lets the card size to its 360px track + paddings.
      gridTemplateRows: 'minmax(260px, auto) auto minmax(300px, 1fr) auto',
      height: '100%', overflow: 'auto',
    }}>
      <Card style={{ gridColumn: '1 / -1', padding: '22px 26px', position: 'relative' }}>
        {milestoneFlash && (
          <div style={{
            position: 'absolute', inset: 0,
            background: `radial-gradient(ellipse at top, ${accentGlow}, transparent 70%)`,
            opacity: 0.8, pointerEvents: 'none', animation: 'ds-fade-out-slow 2s ease',
          }} />
        )}
        {state !== 'off' && !live.coverage.full && live.coverage.excluded.length > 0 && (
          <PartialCoverageBanner coverage={live.coverage} />
        )}
        {state !== 'off' && live.navMissing.length > 0 && (
          <NavMissingBanner missing={live.navMissing} />
        )}
        {state === 'off' ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 18, minHeight: 128 }}>
            <div>
              <Label accent={TOKENS.ink3} style={{ marginBottom: 8 }}>Net asset value</Label>
              <div style={{
                fontFamily: TOKENS.sans,
                fontSize: density === 'compact' ? 44 : 56,
                fontWeight: 300,
                color: TOKENS.ink3,
                letterSpacing: '-0.02em',
                lineHeight: 1,
              }}>
                —
              </div>
              <div style={{
                marginTop: 10, fontFamily: TOKENS.mono, fontSize: 11,
                color: TOKENS.ink3, letterSpacing: '0.04em', textTransform: 'uppercase',
              }}>
                system off · press start to begin
              </div>
            </div>
          </div>
        ) : navPending ? (
          <NavPendingPanel
            uiState={state}
            backendState={live.backendState}
            missing={live.navMissing}
            coverage={live.coverage}
            density={density}
          />
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'flex-end', gap: 32, flexWrap: 'wrap' }}>
              <div>
                <Label accent={TOKENS.ink3} style={{ marginBottom: 8 }}>Net asset value</Label>
                <NavNumber value={navValue} accent={accentColor} size={density === 'compact' ? 54 : 68} />
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 14, marginTop: 10 }}>
                  <span style={{
                    fontFamily: TOKENS.mono, fontSize: 13,
                    color: dayChange >= 0 ? TOKENS.profit : TOKENS.loss,
                    fontVariantNumeric: 'tabular-nums',
                  }}>
                    {dayChange >= 0 ? '+' : '−'}{CURRENCY_SYMBOL}{Math.abs(dayChange).toFixed(2)}
                  </span>
                  <span style={{
                    fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3,
                    fontVariantNumeric: 'tabular-nums',
                  }}>
                    {dayPct >= 0 ? '+' : ''}{dayPct.toFixed(2)}% today
                  </span>
                </div>
              </div>

              <div style={{ flex: 1 }} />

              <div style={{ display: 'flex', gap: 28 }}>
                <MiniStat label="Week"  value={live.pnlRollups.w} />
                <MiniStat label="Month" value={live.pnlRollups.m} />
                <MiniStat label="YTD"   value={live.pnlRollups.y} />
              </div>

              <div style={{ width: 1, height: 48, background: TOKENS.line }} />

              <ExposureRing
                gross={live.exposure.gross}
                net={live.exposure.net}
                accent={accentColor}
                navBasis={live.exposure.navBasis}
                navDivergencePct={live.exposure.navDivergencePct}
              />
            </div>

            <div style={{ marginTop: 18 }}>
              <EquityCurve values={live.equity.length ? live.equity : [navValue, navValue]} accent={accentColor} width={900} height={48} />
            </div>

            {tradable != null && (
              <div style={{
                marginTop: 10, display: 'flex', gap: 12, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3,
                flexWrap: 'wrap',
              }}>
                <span>Tradable {CURRENCY_SYMBOL}{Math.round(tradable).toLocaleString()}</span>
                <span>·</span>
                <span>Allocation {Math.round(live.capitalPct * 100)}%</span>
                {state !== 'running' && (
                  <span style={{ color: TOKENS.caution }}>
                    · system {state === 'starting' ? 'warming up' : state}
                  </span>
                )}
                {!live.coverage.full && live.coverage.excluded.length > 0 && (
                  <span
                    style={{ color: TOKENS.caution }}
                    title={live.coverage.excluded
                      .map((e) => `${e.name}: ${e.reason}`)
                      .join('\n')}
                  >
                    · partial NAV (excl. {live.coverage.excluded.map((e) => e.name).join(', ')})
                  </span>
                )}
              </div>
            )}
          </>
        )}
      </Card>

      {/* Capital allocation — mounted in every dashboard view; when the
          system is off the slider is non-interactive and hides percentages. */}
      <CapitalPanel
        live={live}
        accent={accentColor}
        systemState={state}
        style={{ gridColumn: '1 / -1' }}
      />


      <Card style={{ minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label>Conviction river</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            {live.conviction.length} tracked{topConviction ? ` · top ${prettySymbol(topConviction.sym)}` : ''}
          </span>
        </div>
        <ConvictionRiver conviction={live.conviction} accent={accentColor} newSignal={newSignal} />
      </Card>

      <Card style={{ minHeight: 0, display: 'flex', flexDirection: 'column', padding: 0 }}>
        <div style={{ padding: '14px 16px 10px', borderBottom: `1px solid ${TOKENS.line}` }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <Label>Live feed</Label>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
              <Glyph state={state} accent={accentColor} size={8} />
              {live.wsConnected ? 'streaming' : state === 'running' ? 'polling' : 'idle'}
            </span>
          </div>
        </div>
        <LiveFeed events={live.events} accent={accentColor} />
      </Card>

      <Card style={{ gridColumn: '1 / -1', padding: '14px 18px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, overflowX: 'auto' }}>
          <Label style={{ flexShrink: 0 }}>Book</Label>
          {live.positions.length === 0 ? (
            <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3 }}>
              No open positions
            </span>
          ) : live.positions.map((p) => (
            <PositionChip key={p.sym} pos={p} accent={accentColor} />
          ))}
          <div style={{ flex: 1 }} />
          <BookFooter live={live} />
        </div>
      </Card>
    </div>
  );
}

function BookFooter({ live }: { live: LiveData }) {
  const totalPnl = useMemo(() => live.positions.reduce((s, p) => s + p.pnl, 0), [live.positions]);
  return (
    <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3, flexShrink: 0 }}>
      {live.positions.length} positions
      {live.positions.length > 0 && (
        <>
          {' · '}
          <span style={{ color: totalPnl >= 0 ? TOKENS.profit : TOKENS.loss }}>
            {totalPnl >= 0 ? '+' : '−'}{CURRENCY_SYMBOL}{Math.abs(totalPnl).toFixed(0)}
          </span>
        </>
      )}
    </span>
  );
}

/**
 * Amber banner sitting at the top of the NAV card whenever the aggregated
 * NAV is missing one or more configured brokers.
 *
 * The NAV number itself is unchanged — showing zero brokers is never
 * "zero notional"; it's an incomplete view of the real book. The banner
 * names the missing wallets and surfaces the backend's reason on hover so
 * the operator can go straight to the fix (launch the Gateway, rotate keys,
 * whatever the ``reason`` says). Without this the dashboard silently showed
 * a partial NAV with no indication that anything was wrong — the bug
 * behind the "£98k" scare on 2026-04-22.
 */
function PartialCoverageBanner({ coverage }: { coverage: Coverage }) {
  const names = coverage.excluded.map((e) => e.name).join(', ');
  const title = coverage.excluded
    .map((e) => `${e.name}: ${e.reason || 'not ready'}`)
    .join('\n');
  return (
    <div
      title={title}
      style={{
        marginBottom: 14,
        padding: '8px 12px',
        borderRadius: 6,
        background: 'rgba(255, 191, 0, 0.08)',
        border: '1px solid rgba(255, 191, 0, 0.25)',
        color: TOKENS.caution,
        fontFamily: TOKENS.mono,
        fontSize: 11,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        cursor: 'help',
      }}
    >
      <span style={{ fontWeight: 500, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
        Partial NAV
      </span>
      <span style={{ color: TOKENS.ink2 }}>
        NAV below reflects {coverage.included.length} of {coverage.configured.length} configured brokers.
        Excluded: {names}. Hover for details.
      </span>
    </div>
  );
}

function NavMissingBanner({ missing }: { missing: string[] }) {
  return (
    <div
      title={`Waiting for a current balance from: ${missing.join(', ')}`}
      style={{
        marginBottom: 14,
        padding: '8px 12px',
        borderRadius: 6,
        background: 'rgba(255, 191, 0, 0.08)',
        border: '1px solid rgba(255, 191, 0, 0.25)',
        color: TOKENS.caution,
        fontFamily: TOKENS.mono,
        fontSize: 11,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}
    >
      <span style={{ fontWeight: 500, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
        NAV verifying
      </span>
      <span style={{ color: TOKENS.ink2 }}>
        Balance hidden until current values arrive from: {missing.join(', ')}.
      </span>
    </div>
  );
}

function NavPendingPanel({
  uiState,
  backendState,
  missing,
  coverage,
  density,
}: {
  uiState: SystemState;
  backendState: BackendSystemState;
  missing: string[];
  coverage: Coverage;
  density: Density;
}) {
  const stopping = backendState === 'stopping';
  const waiting = (() => {
    if (missing.length > 0) {
      return `waiting for ${missing.join(', ')}`;
    }
    if (stopping) {
      return 'shutting down';
    }
    if (uiState === 'running') {
      return 'verifying broker balances';
    }
    if (uiState === 'error') {
      return 'system needs attention';
    }
    if (uiState === 'paused') {
      return 'paused';
    }
    if (uiState === 'starting' && coverage.full) {
      return 'syncing net asset value';
    }
    return 'system warming up';
  })();
  const coverageText = stopping
    ? 'finishing shutdown — NAV hidden until next session'
    : coverage.configured.length > 0
      ? `${coverage.included.length} of ${coverage.configured.length} configured brokers ready`
      : 'discovering configured brokers';
  const glyphState: SystemState = stopping ? 'stopping' : uiState;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 18, minHeight: 150 }}>
      <Glyph state={glyphState} accent={TOKENS.caution} size={14} />
      <div>
        <Label accent={TOKENS.ink3} style={{ marginBottom: 8 }}>Net asset value</Label>
        <div style={{
          fontFamily: TOKENS.sans,
          fontSize: density === 'compact' ? 44 : 56,
          fontWeight: 300,
          color: TOKENS.ink3,
          letterSpacing: '-0.02em',
          lineHeight: 1,
        }}>
          —
        </div>
        <div style={{
          marginTop: 10,
          fontFamily: TOKENS.mono,
          fontSize: 11,
          color: TOKENS.caution,
          letterSpacing: '0.04em',
          textTransform: 'uppercase',
        }}>
          {waiting}
        </div>
        <div style={{ marginTop: 6, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          {coverageText}
        </div>
      </div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: number }) {
  const pos = value >= 0;
  return (
    <div>
      <Label style={{ marginBottom: 4 }}>{label}</Label>
      <div style={{
        fontFamily: TOKENS.sans, fontSize: 20, fontWeight: 300,
        color: pos ? TOKENS.profit : TOKENS.loss,
        fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.02em',
      }}>
        {pos ? '+' : '−'}{CURRENCY_SYMBOL}{Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}
      </div>
    </div>
  );
}

function ExposureRing({
  gross,
  net,
  accent,
  navBasis,
  navDivergencePct,
}: {
  gross: number;
  net: number;
  accent: string;
  navBasis: 'snapshot' | 'pnl_today_portfolio_value' | 'none';
  navDivergencePct: number | null;
}) {
  // ``gross``/``net`` are *true* ratios (gross_notional / NAV). Margined
  // paper books regularly run >1.0; the ring used to clamp those to 100%
  // and silently hide the over-leverage. We now:
  //   - cap the visual arc at one full revolution (it can't go further);
  //   - display the real percentage (e.g. "221%");
  //   - flip the colour to ``danger`` and pulse when ratio > 1.0 so an
  //     over-leveraged book cannot be missed at a glance.
  const r = 22;
  const c = 2 * Math.PI * r;
  const grossArc = Math.max(0, Math.min(1, gross));   // visual cap only
  const overLev = gross > 1.0;
  const ringColor = overLev ? TOKENS.danger : accent;
  const numColor = overLev ? TOKENS.danger : TOKENS.ink1;
  const grossPct = (gross * 100).toFixed(0);
  const netPct = (net * 100).toFixed(0);
  const fallback = navBasis === 'pnl_today_portfolio_value';
  const navMismatch = navDivergencePct != null && navDivergencePct > 0.15;
  return (
    <div
      style={{ display: 'flex', alignItems: 'center', gap: 12 }}
      title={overLev
        ? `Over-leveraged: gross ${grossPct}% of NAV. Positions exceed equity via margin.`
        : `Gross ${grossPct}% · Net ${netPct}% of NAV`}
    >
      <svg width="60" height="60" viewBox="0 0 60 60">
        <circle cx="30" cy="30" r={r} fill="none" stroke={TOKENS.line} strokeWidth="3" />
        <circle
          cx="30" cy="30" r={r} fill="none" stroke={ringColor} strokeWidth="3"
          strokeDasharray={`${c * grossArc} ${c}`} strokeLinecap="round"
          transform="rotate(-90 30 30)"
          style={{ transition: `stroke-dasharray 600ms ${TOKENS.ease}, stroke 300ms ${TOKENS.ease}` }}
        />
        {overLev && (
          // Inner accent stroke as a "second lap" marker so it visually
          // reads as "more than full" rather than being indistinguishable
          // from a healthy 100%.
          <circle
            cx="30" cy="30" r={r - 5} fill="none" stroke={ringColor} strokeWidth="1.5"
            strokeDasharray={`${(2 * Math.PI * (r - 5)) * Math.min(1, gross - 1)} ${2 * Math.PI * (r - 5)}`}
            strokeLinecap="round" transform="rotate(-90 30 30)" opacity="0.6"
          />
        )}
        <text x="30" y="33" textAnchor="middle" fontSize="11" fontFamily="Geist Mono"
              fill={numColor} fontWeight="400">
          {grossPct}
        </text>
      </svg>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Label accent={overLev ? TOKENS.danger : undefined}>
          {overLev ? 'Gross · over-lev' : 'Gross'}
        </Label>
        <span style={{
          fontFamily: TOKENS.mono, fontSize: 12, color: numColor,
          fontVariantNumeric: 'tabular-nums', fontWeight: overLev ? 500 : 400,
        }}>
          {grossPct}%
        </span>
        <span style={{
          fontFamily: TOKENS.mono, fontSize: 10,
          color: overLev ? TOKENS.danger : TOKENS.ink3,
          fontVariantNumeric: 'tabular-nums',
        }}>
          net {netPct}%
        </span>
        {(fallback || navMismatch) && (
          <span
            style={{
              fontFamily: TOKENS.mono,
              fontSize: 9,
              color: TOKENS.caution,
              letterSpacing: '0.02em',
            }}
            title={
              fallback
                ? 'Exposure uses /pnl NAV fallback because snapshot NAV looked stale/invalid.'
                : 'Snapshot NAV and /pnl NAV differ materially; exposure may be temporarily noisy.'
            }
          >
            {fallback ? 'nav source: pnl' : 'nav mismatch'}
          </span>
        )}
      </div>
    </div>
  );
}

export function EquityCurve({
  values, accent, width = 900, height = 48,
}: { values: number[]; accent: string; width?: number; height?: number }) {
  const finite = values.map((v) => (Number.isFinite(v) ? v : 0));
  const safeValues = finite.length >= 2
    ? finite
    : finite.length === 1
      ? [finite[0] ?? 0, finite[0] ?? 0]
      : [0, 0];
  const min = Math.min(...safeValues);
  const max = Math.max(...safeValues);
  const rng = max - min || 1;
  // Inset the plot area so the pulsing endpoint dot (r grows to 7) and the
  // stroke itself never clip against the SVG edge. preserveAspectRatio="none"
  // means viewBox pixels are stretched to container width, so padding must be
  // applied inside viewBox coordinates.
  const padX = 10;
  const padY = 6;
  const plotW = Math.max(1, width - padX * 2);
  const plotH = Math.max(1, height - padY * 2);
  const pts = safeValues.map((v, i) => {
    const x = padX + (i / (safeValues.length - 1)) * plotW;
    const y = padY + plotH - ((v - min) / rng) * plotH;
    return [x, y] as const;
  });
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0]},${p[1]}`).join(' ');
  const first = pts[0];
  const last = pts[pts.length - 1];
  const area = `${line} L${last[0]},${height} L${first[0]},${height} Z`;
  return (
    <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" style={{ display: 'block' }}>
      <defs>
        <linearGradient id="ds-eq-area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stopColor={accent} stopOpacity="0.15" />
          <stop offset="100%" stopColor={accent} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill="url(#ds-eq-area)" />
      <path d={line} stroke={accent} strokeWidth="1.4" fill="none" opacity="0.9" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      <circle cx={last[0]} cy={last[1]} r="3" fill={accent}>
        <animate attributeName="r" from="3" to="7" dur="1.8s" repeatCount="indefinite" />
        <animate attributeName="opacity" from="1" to="0" dur="1.8s" repeatCount="indefinite" />
      </circle>
      <circle cx={last[0]} cy={last[1]} r="2.5" fill={accent} />
    </svg>
  );
}

function ConvictionRiver({
  conviction, accent, newSignal,
}: {
  conviction: Conviction[];
  accent: string;
  newSignal: { sym: string; score: number; t: number } | null;
}) {
  const [flashId, setFlashId] = useState<string | null>(null);
  useEffect(() => {
    if (newSignal) {
      setFlashId(newSignal.sym + newSignal.t);
      const t = setTimeout(() => setFlashId(null), 2000);
      return () => clearTimeout(t);
    }
  }, [newSignal]);

  if (conviction.length === 0) {
    return (
      <div style={{
        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11,
      }}>
        awaiting first loop publish
      </div>
    );
  }

  const sorted = [...conviction].sort((a, b) => b.score - a.score);
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 2, minHeight: 0, overflowY: 'auto' }}>
      {sorted.map((c, i) => {
        const pct = c.score * 100;
        const isFresh = flashId === c.sym + (newSignal?.t || 0);
        const barColor = c.side === 'short' ? TOKENS.loss : accent;
        return (
          <div key={c.sym} style={{
            position: 'relative', display: 'flex', alignItems: 'center',
            padding: '10px 4px', borderBottom: `1px solid ${TOKENS.line}`,
            animation: `ds-slide-in 320ms ${TOKENS.ease} ${i * 0.03}s both`,
          }}>
            {isFresh && (
              <div style={{
                position: 'absolute', left: -16, top: 0, bottom: 0, width: 3,
                background: accent, animation: 'ds-flash-bar 2s ease', borderRadius: 2,
              }} />
            )}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, width: 130, flexShrink: 0 }}>
              <span
                title={c.sym}
                style={{
                  fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500,
                  color: TOKENS.ink0, letterSpacing: '-0.02em',
                  width: 64,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                {prettySymbol(c.sym)}
              </span>
              <Pill size="sm" tone={c.side === 'short' ? 'loss' : 'neutral'}>
                {c.side}
              </Pill>
            </div>
            <div style={{
              flex: 1, height: 6, background: 'rgba(255,255,255,0.04)',
              borderRadius: 3, overflow: 'hidden', position: 'relative',
            }}>
              <div style={{
                position: 'absolute', left: 0, top: 0, bottom: 0,
                width: `${pct}%`,
                background: `linear-gradient(90deg, ${barColor}40, ${barColor})`,
                borderRadius: 3,
                transition: `width 600ms ${TOKENS.ease}`,
              }} />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: 140, flexShrink: 0, justifyContent: 'flex-end' }}>
              <span style={{
                fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3,
                maxWidth: 92, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}>
                {c.strat}
              </span>
              <span style={{
                fontFamily: TOKENS.mono, fontSize: 13,
                color: c.side === 'short' ? TOKENS.loss : accent,
                fontVariantNumeric: 'tabular-nums', width: 40, textAlign: 'right',
              }}>
                {c.score.toFixed(2)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function LiveFeed({ events, accent }: { events: LiveEvent[]; accent: string }) {
  if (events.length === 0) {
    return (
      <div style={{
        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11, padding: '14px 16px',
      }}>
        no events yet
      </div>
    );
  }
  return (
    <div style={{ flex: 1, overflowY: 'auto', padding: '8px 16px' }}>
      {events.map((e, i) => (
        <div key={`${e.kind}-${i}-${e.text}`} style={{
          padding: '8px 0', borderBottom: `1px solid ${TOKENS.line}`,
          fontFamily: TOKENS.mono, fontSize: 11,
          animation: i === 0 ? `ds-slide-in 320ms ${TOKENS.ease}` : 'none',
          opacity: Math.max(0.4, 1 - i * 0.06),
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{
              width: 5, height: 5, borderRadius: 999,
              background: e.ok === true ? accent : e.ok === false ? TOKENS.danger : TOKENS.ink3,
              flexShrink: 0,
              boxShadow: e.ok === true && i === 0 ? `0 0 6px ${accent}` : 'none',
            }} />
            <span style={{
              color: TOKENS.ink3, textTransform: 'uppercase', letterSpacing: '0.08em',
              fontSize: 9, width: 42, flexShrink: 0,
            }}>
              {e.kind}
            </span>
            <span style={{
              color: e.ok === false ? TOKENS.loss : TOKENS.ink1,
              flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
            }}>
              {e.text}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}

function PositionChip({ pos, accent }: { pos: Position; accent: string }) {
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 10, flexShrink: 0,
      padding: '8px 12px', borderRadius: 10,
      background: TOKENS.bg2, border: `1px solid ${TOKENS.line}`,
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
        <span
          title={pos.sym}
          style={{
            fontFamily: TOKENS.sans, fontSize: 12, fontWeight: 500,
            color: TOKENS.ink0, letterSpacing: '-0.02em',
          }}>{prettySymbol(pos.sym)}</span>
        <span style={{
          fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3,
          fontVariantNumeric: 'tabular-nums',
        }}>{pos.qty}</span>
      </div>
      <Spark values={[pos.avg, pos.avg * 1.01, pos.avg * 0.995, pos.last || pos.avg]} width={32} height={18} accent={accent} />
      <Signed value={pos.pnl} size={12} />
    </div>
  );
}
