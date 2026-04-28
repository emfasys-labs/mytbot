/**
 * Capital allocation — hybrid slider with trim / flatten confirmation flows.
 *
 * Ported from `newui/project/prototypes/redesign_capital_port/capital.tsx`.
 * The Kill Switch control and the prototype's inlined `CAPITAL_KEYFRAMES`
 * export were deliberately left out: the brief scopes this file to the
 * slider only, and the three keyframes it needs (`ds-tick-flash`,
 * `ds-danger-pulse`, `ds-slide-up`) now live in
 * `src/styles/design-system.css` alongside the rest of the redesign's
 * animations.
 *
 * Behaviour
 * ─────────
 *   • Drag UP past the at-work line → commits on release via
 *     `live.setCapitalPct(pct)` → `PUT /system/capital-allocation`.
 *   • Drag DOWN below the at-work line → stages a trim with a
 *     weakest-first preview and explicit confirm. On confirm the ceiling
 *     is lowered (same endpoint); the engine unwinds on its own signals —
 *     no force-close, and the UI says so.
 *   • Drag to 0% (below `FLATTEN_THRESHOLD`) → hold-to-flatten confirm.
 *     This lowers the ceiling to 0; the backend treats zero allocation as
 *     a flatten request and emits reduce-only close intents through the
 *     normal risk/execution path.
 *
 * The landmark line is gauged against **capital at work** = filled
 * position notional + reserved notional of still-open orders. That's
 * what the backend's ``cap_slider`` gates (``deploy = NAV × ge ×
 * cap_slider`` in ``portfolio/allocation_engine.py``), so the slider
 * reports the same % as the Book screen's "Capital at work" card.
 *
 * Wiring
 * ──────
 *   <CapitalPanel live={live} accent={accentColor} systemState={state} />
 *
 * When ``systemState === 'off'``, the gauge is non-interactive and hides
 * percentages until the operator starts the system.
 */

import {
  CSSProperties,
  PointerEvent as RPointerEvent,
  ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Card, Label } from './primitives';
import { CURRENCY_SYMBOL, SystemState, TOKENS } from './tokens';
import { capitalAtWork } from './mapping';
import type { LiveData } from './useLiveSystem';
import type { Position } from './data';

// ────────────────────────── local tokens ──────────────────────────────
const SNAP_PCT = 0.015;         // snap radius around the deployed line
const FLATTEN_THRESHOLD = 0.03; // below this, the stage turns into flatten
const HOLD_MS = 1200;           // hold-to-flatten duration

// ────────────────────────── utility helpers ───────────────────────────
function money(v: number, size = 14, tone?: string, bold?: boolean): ReactNode {
  return (
    <span
      style={{
        fontFamily: TOKENS.mono,
        fontSize: size,
        fontVariantNumeric: 'tabular-nums',
        color: tone ?? TOKENS.ink1,
        fontWeight: bold ? 500 : 400,
        letterSpacing: '-0.01em',
      }}
    >
      {CURRENCY_SYMBOL}{Math.round(v).toLocaleString()}
    </span>
  );
}

function positionNotional(p: Position): number {
  return Math.abs((p.qty ?? 0) * (p.last ?? p.avg ?? 0));
}

function computeTrim(
  targetPct: number,
  workingValue: number,
  nav: number,
  positions: Position[],
  protectedSyms: Set<string>,
): { closes: Position[]; released: number; mustRelease: number; remaining: number } {
  // ``workingValue`` is positions + pending-order notional — the same figure
  // the backend ceiling gates against. ``mustRelease`` is therefore honest
  // about the full over-commitment, while the close list below only offers
  // positions (pending orders unwind via cancel / engine signals, not
  // through this UI).
  const targetValue = nav * targetPct;
  const mustRelease = Math.max(0, workingValue - targetValue);
  if (mustRelease <= 0 || positions.length === 0) {
    return { closes: [], released: 0, mustRelease: 0, remaining: workingValue };
  }
  // Weakest-first = ascending unrealised P&L as a hold-score proxy. If/when
  // the backend exposes a per-position hold score we swap this in without
  // touching the UI.
  const sorted = [...positions].sort((a, b) => (a.pnl ?? 0) - (b.pnl ?? 0));
  let released = 0;
  const closes: Position[] = [];
  for (const p of sorted) {
    if (protectedSyms.has(p.sym)) continue;
    if (released >= mustRelease) break;
    closes.push(p);
    released += positionNotional(p);
  }
  return { closes, released, mustRelease, remaining: workingValue - released };
}

// ───────────────────────── core component ─────────────────────────────
export interface CapitalPanelProps {
  live: LiveData;
  accent: string;
  /** When `off`, the slider is read-only and percentages are hidden. */
  systemState?: SystemState;
  style?: CSSProperties;
}

export function CapitalPanel({ live, accent, systemState = 'running', style }: CapitalPanelProps) {
  const interactive = systemState !== 'off';
  const nav = live.nav;
  const ceilingPct = live.capitalPct;
  // Gauge the slider against **capital at work** — positions + pending
  // orders — because that's what the backend's ``cap_slider`` actually
  // gates (see ``portfolio/allocation_engine.py`` and ``mapping.capitalAtWork``).
  // Using positions-only here under-reports commitment by the pending-order
  // book, which puts the snap landmark and "free to deploy" headroom in the
  // wrong place. ``deployedValue`` is still tracked separately so the trim
  // close list can honestly show which positions are actually closable.
  const { deployed: deployedValue, pending: pendingValue, working: workingValue } = useMemo(
    () => capitalAtWork(live.positions, live.orders),
    [live.positions, live.orders],
  );
  const workingRatio = nav > 0 ? Math.max(0, workingValue / nav) : 0;
  const workingPct = nav > 0 ? Math.min(1, workingValue / nav) : 0;

  const trackRef = useRef<HTMLDivElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dragPct, setDragPct] = useState(ceilingPct);
  const [stagedPct, setStagedPct] = useState<number | null>(null);
  const [flashTick, setFlashTick] = useState(false);
  const [protectedSyms, setProtectedSyms] = useState<Set<string>>(new Set());
  const [reviewing, setReviewing] = useState(false);
  const crossedRef = useRef(false);

  useEffect(() => {
    if (interactive) return;
    setDragging(false);
    setStagedPct(null);
    setReviewing(false);
    setProtectedSyms(new Set());
    setDragPct(ceilingPct);
    crossedRef.current = false;
  }, [interactive, ceilingPct]);

  // If the ceiling changes under us (e.g. WS tick from another client) while
  // we're idle, follow it. During a drag or a staged trim we leave the
  // local value alone so the operator's in-flight input isn't clobbered.
  useEffect(() => {
    if (!dragging && stagedPct == null) setDragPct(ceilingPct);
  }, [ceilingPct, dragging, stagedPct]);

  // Tick flash when the thumb first crosses the at-work line downward.
  useEffect(() => {
    if (!dragging) return;
    if (dragPct < workingPct && !crossedRef.current) {
      crossedRef.current = true;
      setFlashTick(true);
      const t = setTimeout(() => setFlashTick(false), 400);
      return () => clearTimeout(t);
    }
    if (dragPct >= workingPct && crossedRef.current) {
      crossedRef.current = false;
    }
  }, [dragPct, dragging, workingPct]);

  const shownPct = dragging ? dragPct : stagedPct ?? ceilingPct;

  const readPointerPct = useCallback(
    (clientY: number) => {
      const el = trackRef.current;
      if (!el) return ceilingPct;
      const rect = el.getBoundingClientRect();
      const raw = 1 - (clientY - rect.top) / rect.height;
      const clamped = Math.max(0, Math.min(1, raw));
      // Snap to the deployed landmark so small hand tremors don't
      // accidentally stage a trim when the user is hovering at parity.
      return Math.abs(clamped - workingPct) < SNAP_PCT ? workingPct : clamped;
    },
    [ceilingPct, workingPct],
  );

  const commitCeiling = useCallback(
    async (pct: number) => {
      try {
        await live.setCapitalPct(pct);
      } catch {
        // `setCapitalPct` already reverts optimistic state on failure; the
        // next status poll reconciles the slider with reality.
      }
    },
    [live],
  );

  const startDrag = useCallback(
    (e: RPointerEvent<HTMLDivElement>) => {
      if (!interactive) return;
      e.preventDefault();
      setDragging(true);
      setStagedPct(null);
      setReviewing(false);
      setDragPct(readPointerPct(e.clientY));

      const move = (ev: PointerEvent) => setDragPct(readPointerPct(ev.clientY));
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        setDragging(false);
        setDragPct((prev) => {
          if (prev >= workingPct - 0.005) {
            // Upward (or unchanged) — commit immediately.
            void commitCeiling(prev);
            setStagedPct(null);
          } else {
            // Downward past deployed — stage for confirmation instead of
            // silently lowering the ceiling.
            setStagedPct(prev);
          }
          return prev;
        });
        crossedRef.current = false;
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    },
    [interactive, readPointerPct, workingPct, commitCeiling],
  );

  const cancelStage = useCallback(() => {
    setStagedPct(null);
    setDragPct(ceilingPct);
    setReviewing(false);
    setProtectedSyms(new Set());
  }, [ceilingPct]);

  const confirmTrim = useCallback(async () => {
    if (stagedPct == null) return;
    await commitCeiling(stagedPct);
    setStagedPct(null);
    setReviewing(false);
    setProtectedSyms(new Set());
  }, [stagedPct, commitCeiling]);

  const confirmFlatten = useCallback(async () => {
    // Lower the ceiling to 0. The trading loop interprets zero allocation
    // as "flatten held exposure" and emits reduce-only close intents.
    await commitCeiling(0);
    setStagedPct(null);
  }, [commitCeiling]);

  const previewPct = stagedPct ?? (dragging ? dragPct : ceilingPct);
  const previewBelow = previewPct < workingPct - 0.005;
  const trim = useMemo(
    () =>
      previewBelow
        ? computeTrim(previewPct, workingValue, nav, live.positions, protectedSyms)
        : null,
    [previewPct, previewBelow, workingValue, nav, live.positions, protectedSyms],
  );

  const isFlatten = stagedPct != null && stagedPct < FLATTEN_THRESHOLD;
  const zoneColor = previewBelow ? (isFlatten ? TOKENS.danger : TOKENS.caution) : accent;

  const height = 360;
  const thumbY = (1 - shownPct) * height;
  const workingY = (1 - workingPct) * height;

  return (
    <Card style={{ padding: 20, ...style }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 16,
        }}
      >
        <Label style={{ color: interactive ? undefined : TOKENS.ink3 }}>Capital allocation</Label>
        <span
          style={{
            fontFamily: TOKENS.mono,
            fontSize: 10,
            color: interactive ? TOKENS.ink3 : TOKENS.ink4,
          }}
        >
          {interactive ? 'ceiling · at work · free' : 'idle'}
        </span>
      </div>

      <div
        style={{
          display: 'flex',
          gap: 24,
          alignItems: 'flex-start',
          opacity: interactive ? 1 : 0.42,
          filter: interactive ? 'none' : 'grayscale(1)',
          transition: `opacity 220ms ${TOKENS.ease}, filter 220ms ${TOKENS.ease}`,
        }}
      >
        {/* Scale */}
        <div
          style={{
            height,
            position: 'relative',
            width: 42,
            flexShrink: 0,
            fontFamily: TOKENS.mono,
            fontSize: 10,
          }}
        >
          {[1, 0.75, 0.5, 0.25, 0].map((p) => (
            <div
              key={p}
              style={{
                position: 'absolute',
                right: 0,
                top: (1 - p) * height - 6,
                color: interactive ? TOKENS.ink3 : TOKENS.ink4,
                textAlign: 'right',
              }}
            >
              {interactive ? `${(p * 100).toFixed(0)}%` : '—'}
            </div>
          ))}
        </div>

        {/* Track */}
        <div style={{ position: 'relative', paddingRight: 140 }}>
          <div
            ref={trackRef}
            onPointerDown={interactive ? startDrag : undefined}
            style={{
              position: 'relative',
              width: 32,
              height,
              borderRadius: 10,
              background: TOKENS.bg2,
              border: `1px solid ${TOKENS.line}`,
              cursor: interactive ? (dragging ? 'grabbing' : 'grab') : 'not-allowed',
              touchAction: interactive ? 'none' : 'auto',
              pointerEvents: interactive ? 'auto' : 'none',
            }}
          >
            {/* deployed fill (dim) */}
            {interactive && workingPct > 0 && (
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  bottom: 0,
                  height: `${workingPct * 100}%`,
                  background: `${accent}26`,
                  borderBottomLeftRadius: 10,
                  borderBottomRightRadius: 10,
                }}
              />
            )}

            {/* ceiling headroom fill */}
            {interactive && shownPct > workingPct && (
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  bottom: `${workingPct * 100}%`,
                  height: `${(shownPct - workingPct) * 100}%`,
                  background: `linear-gradient(to top, ${accent}55, ${accent}22)`,
                  transition: dragging ? 'none' : `all 400ms ${TOKENS.ease}`,
                }}
              />
            )}

            {/* release zone (below deployed) */}
            {interactive && shownPct < workingPct && (
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  bottom: `${shownPct * 100}%`,
                  height: `${(workingPct - shownPct) * 100}%`,
                  background: `repeating-linear-gradient(135deg, ${
                    isFlatten || shownPct < FLATTEN_THRESHOLD
                      ? 'rgba(248,113,113,0.14)'
                      : 'rgba(252,211,77,0.14)'
                  } 0 6px, transparent 6px 10px)`,
                  borderLeft: `1px solid ${zoneColor}66`,
                  borderRight: `1px solid ${zoneColor}66`,
                  transition: dragging ? 'none' : `all 400ms ${TOKENS.ease}`,
                }}
              />
            )}

            {/* deployed landmark line */}
            {interactive && (
              <>
                <div
                  style={{
                    position: 'absolute',
                    left: -6,
                    right: -6,
                    top: workingY - 0.5,
                    height: 1,
                    background: TOKENS.ink1,
                    pointerEvents: 'none',
                    animation: flashTick ? 'ds-tick-flash 400ms ease' : 'none',
                  }}
                />
                <div
                  style={{
                    position: 'absolute',
                    left: 44,
                    top: workingY - 9,
                    fontFamily: TOKENS.mono,
                    fontSize: 10,
                    color: TOKENS.ink2,
                    whiteSpace: 'nowrap',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                  }}
                >
                  <span style={{ width: 14, height: 1, background: TOKENS.ink2 }} />
                  at work · {(workingRatio * 100).toFixed(1)}%
                </div>
              </>
            )}

            {/* thumb */}
            {interactive && (
              <div
                style={{
                  position: 'absolute',
                  left: -16,
                  top: thumbY - 14,
                  width: 64,
                  height: 28,
                  borderRadius: 6,
                  background: TOKENS.ink0,
                  color: TOKENS.bg0,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontFamily: TOKENS.mono,
                  fontSize: 12,
                  fontWeight: 500,
                  boxShadow: `0 6px 14px rgba(0,0,0,0.5), 0 0 0 1px ${zoneColor}55`,
                  cursor: dragging ? 'grabbing' : 'grab',
                  transition: dragging
                    ? 'none'
                    : `top 300ms ${TOKENS.ease}, box-shadow 300ms ${TOKENS.ease}`,
                  animation: isFlatten ? 'ds-danger-pulse 1.2s ease-in-out infinite' : 'none',
                  userSelect: 'none',
                }}
              >
                {(shownPct * 100).toFixed(0)}%
              </div>
            )}

            {/* staged ghost bar */}
            {interactive && stagedPct != null && (
              <div
                style={{
                  position: 'absolute',
                  left: -4,
                  right: -4,
                  top: (1 - stagedPct) * height - 0.5,
                  height: 2,
                  background: zoneColor,
                  borderRadius: 1,
                  pointerEvents: 'none',
                }}
              />
            )}
          </div>
        </div>

        {/* Info / action panel */}
        <div style={{ flex: 1, minWidth: 280 }}>
          {!interactive ? (
            <CapitalOffIdle />
          ) : stagedPct == null ? (
            <IdleInfo
              ceilingPct={ceilingPct}
              shownPct={shownPct}
              dragging={dragging}
              nav={nav}
              deployedValue={deployedValue}
              pendingValue={pendingValue}
              workingValue={workingValue}
              accent={accent}
            />
          ) : isFlatten ? (
            <FlattenConfirm
              positionsCount={live.positions.length}
              workingValue={workingValue}
              onCancel={cancelStage}
              onConfirm={confirmFlatten}
            />
          ) : (
            <TrimPreview
              stagedPct={stagedPct}
              workingPct={workingPct}
              trim={trim!}
              positions={live.positions}
              reviewing={reviewing}
              setReviewing={setReviewing}
              protectedSyms={protectedSyms}
              setProtectedSyms={setProtectedSyms}
              onCancel={cancelStage}
              onConfirm={confirmTrim}
            />
          )}
        </div>
      </div>
    </Card>
  );
}

// ────────────────────────── sub-panels ────────────────────────────────
function CapitalOffIdle() {
  return (
    <div style={{ animation: `ds-fade-in 200ms ${TOKENS.ease}` }}>
      <div
        style={{
          fontFamily: TOKENS.sans,
          fontSize: 14,
          fontWeight: 400,
          color: TOKENS.ink2,
          lineHeight: 1.55,
          maxWidth: 420,
        }}
      >
        Allocation controls stay idle while the system is off. Press{' '}
        <span style={{ color: TOKENS.ink0, fontWeight: 500 }}>Start</span> to load the book and set
        your deployment ceiling.
      </div>
      <div
        style={{
          marginTop: 14,
          fontFamily: TOKENS.mono,
          fontSize: 10,
          color: TOKENS.ink4,
          letterSpacing: '0.04em',
          textTransform: 'uppercase',
        }}
      >
        Slider locked · no preview
      </div>
    </div>
  );
}

function IdleInfo({
  ceilingPct,
  shownPct,
  dragging,
  nav,
  deployedValue,
  pendingValue,
  workingValue,
  accent,
}: {
  ceilingPct: number;
  shownPct: number;
  dragging: boolean;
  nav: number;
  deployedValue: number;
  pendingValue: number;
  workingValue: number;
  accent: string;
}) {
  const targetValue = nav * shownPct;
  const delta = shownPct - ceilingPct;
  const up = delta > 0.005;
  const down = delta < -0.005;
  // Only surface the pending breakdown when it's materially non-zero — most
  // of the time the slider is gauging a book of filled positions and the
  // extra row would just be noise.
  const showPendingBreakdown = pendingValue > 0.5;
  return (
    <div style={{ animation: `ds-fade-in 200ms ${TOKENS.ease}` }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <div
          style={{
            fontFamily: TOKENS.sans,
            fontSize: 38,
            fontWeight: 300,
            letterSpacing: '-0.03em',
            color: TOKENS.ink0,
          }}
        >
          {(shownPct * 100).toFixed(0)}
          <span style={{ fontSize: 20, color: TOKENS.ink3 }}>%</span>
        </div>
        <div style={{ fontFamily: TOKENS.mono, fontSize: 12, color: TOKENS.ink3 }}>
          {money(targetValue, 12, TOKENS.ink2)} of {money(nav, 12, TOKENS.ink3)}
        </div>
      </div>

      <div
        style={{
          marginTop: 16,
          paddingTop: 14,
          borderTop: `1px solid ${TOKENS.line}`,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}
      >
        <Row label="At work" value={money(workingValue, 12, TOKENS.ink1)} />
        {showPendingBreakdown && (
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              fontFamily: TOKENS.mono,
              fontSize: 10,
              color: TOKENS.ink3,
              marginTop: -2,
              paddingLeft: 2,
            }}
          >
            <span>positions {money(deployedValue, 10, TOKENS.ink3)}</span>
            <span>pending {money(pendingValue, 10, TOKENS.ink3)}</span>
          </div>
        )}
        <Row
          label="Free to deploy"
          value={money(Math.max(0, targetValue - workingValue), 12, up ? accent : TOKENS.ink2)}
        />
      </div>

      <div
        style={{
          marginTop: 14,
          paddingTop: 14,
          borderTop: `1px solid ${TOKENS.line}`,
          fontFamily: TOKENS.sans,
          fontSize: 12,
          color: TOKENS.ink2,
          lineHeight: 1.5,
        }}
      >
        {dragging && up && (
          <>Raising ceiling. Commits on release — system may deploy up to {money(targetValue, 12, accent, true)}.</>
        )}
        {dragging && !up && !down && <>No change. Release to keep current ceiling.</>}
        {dragging && down && (
          <span style={{ color: TOKENS.caution }}>
            Below at-work line — drop to stage a release with position preview.
          </span>
        )}
        {!dragging && (
          <>
            Drag up to raise the ceiling for new positions. Drag below the at-work line to stage a
            reduction; 0% triggers a hold-to-flatten.
          </>
        )}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
      <span
        style={{
          fontFamily: TOKENS.sans,
          fontSize: 11,
          color: TOKENS.ink3,
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
        }}
      >
        {label}
      </span>
      {value}
    </div>
  );
}

function TrimPreview({
  stagedPct,
  workingPct,
  trim,
  positions,
  reviewing,
  setReviewing,
  protectedSyms,
  setProtectedSyms,
  onCancel,
  onConfirm,
}: {
  stagedPct: number;
  workingPct: number;
  trim: { closes: Position[]; released: number; mustRelease: number; remaining: number };
  positions: Position[];
  reviewing: boolean;
  setReviewing: (v: boolean | ((b: boolean) => boolean)) => void;
  protectedSyms: Set<string>;
  setProtectedSyms: (fn: (prev: Set<string>) => Set<string>) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const sorted = [...positions].sort((a, b) => (a.pnl ?? 0) - (b.pnl ?? 0));
  const willClose = new Set(trim.closes.map((c) => c.sym));
  const shortage = Math.max(0, trim.mustRelease - trim.released);

  return (
    <div
      style={{
        animation: `ds-slide-up 260ms ${TOKENS.ease}`,
        padding: 14,
        border: `1px solid ${TOKENS.caution}66`,
        borderRadius: 10,
        background: 'rgba(252,211,77,0.03)',
      }}
    >
      <div
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
      >
        <Label accent={TOKENS.caution}>Reduce to target</Label>
        <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          lowers ceiling · engine unwinds
        </span>
      </div>

      <div style={{ marginTop: 10, display: 'flex', alignItems: 'baseline', gap: 10 }}>
        <div
          style={{
            fontFamily: TOKENS.sans,
            fontSize: 26,
            fontWeight: 300,
            letterSpacing: '-0.03em',
            color: TOKENS.ink0,
          }}
        >
          {(workingPct * 100).toFixed(0)}
          <span style={{ fontSize: 14, color: TOKENS.ink3 }}>%</span>
          <span style={{ color: TOKENS.ink3, margin: '0 8px', fontWeight: 200 }}>→</span>
          <span style={{ color: TOKENS.caution }}>{(stagedPct * 100).toFixed(0)}%</span>
        </div>
      </div>

      <div
        style={{
          marginTop: 12,
          paddingTop: 10,
          borderTop: `1px solid ${TOKENS.line}`,
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
        }}
      >
        <Row label="Must release" value={money(trim.mustRelease, 12, TOKENS.caution, true)} />
        <Row
          label="Will touch"
          value={
            <span style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink1 }}>
              {trim.closes.length} {trim.closes.length === 1 ? 'position' : 'positions'}
            </span>
          }
        />
      </div>

      {positions.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              marginBottom: 6,
            }}
          >
            <Label>Weakest first · by unrealised pnl</Label>
            <button
              type="button"
              onClick={() => setReviewing((r) => !r)}
              style={{
                padding: '3px 8px',
                background: 'transparent',
                border: `1px solid ${TOKENS.line}`,
                borderRadius: 4,
                color: TOKENS.ink2,
                fontFamily: TOKENS.sans,
                fontSize: 10,
                textTransform: 'uppercase',
                letterSpacing: '0.08em',
                cursor: 'pointer',
              }}
            >
              {reviewing ? 'done' : 'review'}
            </button>
          </div>

          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 3,
              maxHeight: 160,
              overflowY: 'auto',
            }}
          >
            {(reviewing ? sorted : trim.closes).map((p) => {
              const willGo = willClose.has(p.sym) && !protectedSyms.has(p.sym);
              const isProtected = protectedSyms.has(p.sym);
              const posValue = positionNotional(p);
              return (
                <div
                  key={p.sym}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                    padding: '7px 9px',
                    borderRadius: 6,
                    background: willGo ? 'rgba(252,211,77,0.08)' : 'transparent',
                    border: `1px solid ${willGo ? `${TOKENS.caution}44` : TOKENS.line}`,
                    opacity: reviewing && !willGo && !isProtected ? 0.4 : 1,
                  }}
                >
                  {reviewing && (
                    <input
                      type="checkbox"
                      checked={!isProtected}
                      onChange={() =>
                        setProtectedSyms((prev) => {
                          const next = new Set(prev);
                          if (next.has(p.sym)) next.delete(p.sym);
                          else next.add(p.sym);
                          return next;
                        })
                      }
                      style={{ accentColor: TOKENS.caution, cursor: 'pointer' }}
                    />
                  )}
                  <span
                    style={{
                      fontFamily: TOKENS.sans,
                      fontSize: 12,
                      fontWeight: 500,
                      color: TOKENS.ink0,
                      width: 56,
                    }}
                  >
                    {p.sym}
                  </span>
                  <span
                    style={{
                      fontFamily: TOKENS.mono,
                      fontSize: 10,
                      color: TOKENS.ink3,
                      flex: 1,
                    }}
                  >
                    pnl {(p.pnl ?? 0) >= 0 ? '+' : '−'}{CURRENCY_SYMBOL}{Math.abs(p.pnl ?? 0).toFixed(0)}
                  </span>
                  {money(posValue, 11, willGo ? TOKENS.caution : TOKENS.ink2)}
                </div>
              );
            })}
          </div>

          {shortage > 0.5 && (
            <div
              style={{
                marginTop: 8,
                padding: 8,
                background: 'rgba(248,113,113,0.10)',
                border: `1px solid ${TOKENS.danger}44`,
                borderRadius: 6,
                fontFamily: TOKENS.sans,
                fontSize: 11,
                color: TOKENS.danger,
                lineHeight: 1.4,
              }}
            >
              Protecting leaves a {money(shortage, 11, TOKENS.danger, true)} shortfall. Target won't
              be fully reached — the ceiling still lowers but unwinding depends on the engine's
              signals.
            </div>
          )}
        </div>
      )}

      <div
        style={{
          marginTop: 10,
          padding: 8,
          background: 'rgba(147,197,253,0.06)',
          border: '1px solid rgba(147,197,253,0.18)',
          borderRadius: 6,
          fontFamily: TOKENS.sans,
          fontSize: 11,
          color: TOKENS.info,
          lineHeight: 1.4,
        }}
      >
        Confirm lowers the ceiling to {(stagedPct * 100).toFixed(0)}%. Existing positions stay open
        — the engine unwinds on its own signals. For immediate per-symbol close, use Book → Close.
      </div>

      <div style={{ marginTop: 12, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button
          type="button"
          onClick={onCancel}
          style={{
            padding: '7px 12px',
            background: 'transparent',
            border: `1px solid ${TOKENS.line}`,
            borderRadius: 6,
            color: TOKENS.ink2,
            fontFamily: TOKENS.sans,
            fontSize: 12,
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onConfirm}
          style={{
            padding: '7px 14px',
            background: TOKENS.caution,
            border: 'none',
            borderRadius: 6,
            color: TOKENS.bg0,
            fontFamily: TOKENS.sans,
            fontSize: 12,
            fontWeight: 500,
            letterSpacing: '-0.01em',
            cursor: 'pointer',
          }}
        >
          Lower to {(stagedPct * 100).toFixed(0)}%
        </button>
      </div>
    </div>
  );
}

function FlattenConfirm({
  positionsCount,
  workingValue,
  onCancel,
  onConfirm,
}: {
  positionsCount: number;
  workingValue: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [hold, setHold] = useState(0);
  const holding = useRef(false);
  const raf = useRef<number | null>(null);
  const start = useRef(0);

  const down = useCallback(() => {
    holding.current = true;
    start.current = performance.now();
    const tick = () => {
      if (!holding.current) return;
      const p = Math.min(1, (performance.now() - start.current) / HOLD_MS);
      setHold(p);
      if (p < 1) raf.current = requestAnimationFrame(tick);
      else {
        holding.current = false;
        onConfirm();
      }
    };
    raf.current = requestAnimationFrame(tick);
  }, [onConfirm]);

  const up = useCallback(() => {
    holding.current = false;
    if (raf.current != null) cancelAnimationFrame(raf.current);
    setHold(0);
  }, []);

  useEffect(() => {
    // Safety: if this panel unmounts mid-hold (e.g. user cancels elsewhere)
    // we still cancel the RAF loop so it doesn't fire after dismount.
    return () => {
      holding.current = false;
      if (raf.current != null) cancelAnimationFrame(raf.current);
    };
  }, []);

  return (
    <div
      style={{
        animation: `ds-slide-up 260ms ${TOKENS.ease}`,
        padding: 14,
        border: `1px solid ${TOKENS.danger}88`,
        borderRadius: 10,
        background: 'linear-gradient(180deg, rgba(248,113,113,0.05), rgba(10,10,11,1))',
      }}
    >
      <Label accent={TOKENS.danger}>Flatten book</Label>
      <div
        style={{
          marginTop: 12,
          fontFamily: TOKENS.sans,
          fontSize: 22,
          fontWeight: 400,
          color: TOKENS.ink0,
          letterSpacing: '-0.02em',
        }}
      >
        Zero capital · {positionsCount} open
      </div>
      <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 12, color: TOKENS.ink2 }}>
        At work {money(workingValue, 12, TOKENS.ink1, true)} · ceiling → 0%
      </div>

      <div
        style={{
          marginTop: 12,
          padding: 10,
          background: 'rgba(248,113,113,0.08)',
          border: `1px solid ${TOKENS.danger}44`,
          borderRadius: 6,
          fontFamily: TOKENS.sans,
          fontSize: 12,
          color: TOKENS.ink2,
          lineHeight: 1.5,
        }}
      >
        This lowers the ceiling to 0% and tells the engine to flatten held exposure using
        reduce-only close orders. Broker session hours and already-working orders can delay fills;
        use Book for per-symbol intervention if an order sits.
      </div>

      <div style={{ marginTop: 12, display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button
          type="button"
          onClick={onCancel}
          style={{
            padding: '7px 12px',
            background: 'transparent',
            border: `1px solid ${TOKENS.line}`,
            borderRadius: 6,
            color: TOKENS.ink2,
            fontFamily: TOKENS.sans,
            fontSize: 12,
            fontWeight: 500,
            cursor: 'pointer',
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          onPointerDown={down}
          onPointerUp={up}
          onPointerLeave={up}
          onPointerCancel={up}
          style={{
            position: 'relative',
            padding: '8px 16px',
            background: TOKENS.bg1,
            border: `1px solid ${TOKENS.danger}`,
            borderRadius: 6,
            color: TOKENS.danger,
            fontFamily: TOKENS.sans,
            fontSize: 12,
            fontWeight: 500,
            textTransform: 'uppercase',
            letterSpacing: '0.1em',
            overflow: 'hidden',
            cursor: 'pointer',
          }}
        >
          <div
            style={{
              position: 'absolute',
              inset: 0,
              background: TOKENS.danger,
              opacity: hold,
              transition: 'opacity 40ms linear',
            }}
          />
          <span
            style={{
              position: 'relative',
              color: hold > 0.5 ? TOKENS.ink0 : TOKENS.danger,
              transition: 'color 200ms',
            }}
          >
            {hold > 0 ? 'Holding…' : 'Hold to lower to 0%'}
          </span>
        </button>
      </div>
    </div>
  );
}

