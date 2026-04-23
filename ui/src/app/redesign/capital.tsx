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
 *   • Drag UP past the deployed line → commits on release via
 *     `live.setCapitalPct(pct)` → `PUT /system/capital-allocation`.
 *   • Drag DOWN below the deployed line → stages a trim with a
 *     weakest-first preview and explicit confirm. On confirm the ceiling
 *     is lowered (same endpoint); the engine unwinds on its own signals —
 *     no force-close, and the UI says so.
 *   • Drag to 0% (below `FLATTEN_THRESHOLD`) → hold-to-flatten confirm.
 *     `POST /positions/flatten` is not shipped yet; the component is
 *     honest about that — it lowers the ceiling to 0 (prevents new
 *     deploys) and shows a "backend pending" banner. When the endpoint
 *     ships, only `confirmFlatten` needs rewiring.
 *
 * Wiring
 * ──────
 *   <CapitalPanel live={live} accent={accentColor} />
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
import { TOKENS } from './tokens';
import type { LiveData } from './useLiveSystem';
import type { Position } from './data';

// ────────────────────────── local tokens ──────────────────────────────
const SNAP_PCT = 0.015;         // snap radius around the deployed line
const FLATTEN_THRESHOLD = 0.03; // below this, the stage turns into flatten
const HOLD_MS = 1200;           // hold-to-flatten duration

// ────────────────────────── result banner type ────────────────────────
type CapitalResult =
  | { kind: 'committed'; at: number }
  | { kind: 'trim-requested'; at: number; pct: number }
  | { kind: 'flatten-requested'; at: number };

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
      £{Math.round(v).toLocaleString()}
    </span>
  );
}

function positionNotional(p: Position): number {
  return Math.abs((p.qty ?? 0) * (p.last ?? p.avg ?? 0));
}

function computeTrim(
  targetPct: number,
  deployedValue: number,
  nav: number,
  positions: Position[],
  protectedSyms: Set<string>,
): { closes: Position[]; released: number; mustRelease: number; remaining: number } {
  const targetValue = nav * targetPct;
  const mustRelease = Math.max(0, deployedValue - targetValue);
  if (mustRelease <= 0 || positions.length === 0) {
    return { closes: [], released: 0, mustRelease: 0, remaining: deployedValue };
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
  return { closes, released, mustRelease, remaining: deployedValue - released };
}

// ───────────────────────── core component ─────────────────────────────
export interface CapitalPanelProps {
  live: LiveData;
  accent: string;
  style?: CSSProperties;
}

export function CapitalPanel({ live, accent, style }: CapitalPanelProps) {
  const nav = live.nav;
  const ceilingPct = live.capitalPct;
  const deployedValue = useMemo(
    () => live.positions.reduce((s, p) => s + positionNotional(p), 0),
    [live.positions],
  );
  const deployedPct = nav > 0 ? Math.min(1, deployedValue / nav) : 0;

  const trackRef = useRef<HTMLDivElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dragPct, setDragPct] = useState(ceilingPct);
  const [stagedPct, setStagedPct] = useState<number | null>(null);
  const [flashTick, setFlashTick] = useState(false);
  const [protectedSyms, setProtectedSyms] = useState<Set<string>>(new Set());
  const [reviewing, setReviewing] = useState(false);
  const [lastResult, setLastResult] = useState<CapitalResult | null>(null);
  const crossedRef = useRef(false);

  // If the ceiling changes under us (e.g. WS tick from another client) while
  // we're idle, follow it. During a drag or a staged trim we leave the
  // local value alone so the operator's in-flight input isn't clobbered.
  useEffect(() => {
    if (!dragging && stagedPct == null) setDragPct(ceilingPct);
  }, [ceilingPct, dragging, stagedPct]);

  // Tick flash when the thumb first crosses the deployed line downward.
  useEffect(() => {
    if (!dragging) return;
    if (dragPct < deployedPct && !crossedRef.current) {
      crossedRef.current = true;
      setFlashTick(true);
      const t = setTimeout(() => setFlashTick(false), 400);
      return () => clearTimeout(t);
    }
    if (dragPct >= deployedPct && crossedRef.current) {
      crossedRef.current = false;
    }
  }, [dragPct, dragging, deployedPct]);

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
      return Math.abs(clamped - deployedPct) < SNAP_PCT ? deployedPct : clamped;
    },
    [ceilingPct, deployedPct],
  );

  const commitCeiling = useCallback(
    async (pct: number) => {
      try {
        await live.setCapitalPct(pct);
        setLastResult({ kind: 'committed', at: Date.now() });
      } catch {
        // `setCapitalPct` already reverts optimistic state on failure; the
        // next status poll will reconcile the slider with reality. We just
        // suppress the banner so the UI doesn't falsely claim success.
      }
    },
    [live],
  );

  const startDrag = useCallback(
    (e: RPointerEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragging(true);
      setStagedPct(null);
      setReviewing(false);
      setLastResult(null);
      setDragPct(readPointerPct(e.clientY));

      const move = (ev: PointerEvent) => setDragPct(readPointerPct(ev.clientY));
      const up = () => {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
        setDragging(false);
        setDragPct((prev) => {
          if (prev >= deployedPct - 0.005) {
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
    [readPointerPct, deployedPct, commitCeiling],
  );

  const cancelStage = useCallback(() => {
    setStagedPct(null);
    setDragPct(ceilingPct);
    setReviewing(false);
    setProtectedSyms(new Set());
  }, [ceilingPct]);

  const confirmTrim = useCallback(async () => {
    if (stagedPct == null) return;
    const target = stagedPct;
    await commitCeiling(target);
    setStagedPct(null);
    setReviewing(false);
    setProtectedSyms(new Set());
    setLastResult({ kind: 'trim-requested', at: Date.now(), pct: target });
  }, [stagedPct, commitCeiling]);

  const confirmFlatten = useCallback(async () => {
    // UI-side: lower the ceiling to 0 so no new positions open. Per-symbol
    // close lives in the Book screen; we cannot force-close from here
    // until the backend exposes `POST /positions/flatten`.
    await commitCeiling(0);
    setStagedPct(null);
    setLastResult({ kind: 'flatten-requested', at: Date.now() });
  }, [commitCeiling]);

  const previewPct = stagedPct ?? (dragging ? dragPct : ceilingPct);
  const previewBelow = previewPct < deployedPct - 0.005;
  const trim = useMemo(
    () =>
      previewBelow
        ? computeTrim(previewPct, deployedValue, nav, live.positions, protectedSyms)
        : null,
    [previewPct, previewBelow, deployedValue, nav, live.positions, protectedSyms],
  );

  const isFlatten = stagedPct != null && stagedPct < FLATTEN_THRESHOLD;
  const zoneColor = previewBelow ? (isFlatten ? TOKENS.danger : TOKENS.caution) : accent;

  const height = 360;
  const thumbY = (1 - shownPct) * height;
  const deployedY = (1 - deployedPct) * height;

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
        <Label>Capital allocation</Label>
        <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          ceiling · deployed · free
        </span>
      </div>

      <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>
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
                color: TOKENS.ink3,
                textAlign: 'right',
              }}
            >
              {(p * 100).toFixed(0)}%
            </div>
          ))}
        </div>

        {/* Track */}
        <div style={{ position: 'relative', paddingRight: 140 }}>
          <div
            ref={trackRef}
            onPointerDown={startDrag}
            style={{
              position: 'relative',
              width: 32,
              height,
              borderRadius: 10,
              background: TOKENS.bg2,
              border: `1px solid ${TOKENS.line}`,
              cursor: dragging ? 'grabbing' : 'grab',
              touchAction: 'none',
            }}
          >
            {/* deployed fill (dim) */}
            {deployedPct > 0 && (
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  bottom: 0,
                  height: `${deployedPct * 100}%`,
                  background: `${accent}26`,
                  borderBottomLeftRadius: 10,
                  borderBottomRightRadius: 10,
                }}
              />
            )}

            {/* ceiling headroom fill */}
            {shownPct > deployedPct && (
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  bottom: `${deployedPct * 100}%`,
                  height: `${(shownPct - deployedPct) * 100}%`,
                  background: `linear-gradient(to top, ${accent}55, ${accent}22)`,
                  transition: dragging ? 'none' : `all 400ms ${TOKENS.ease}`,
                }}
              />
            )}

            {/* release zone (below deployed) */}
            {shownPct < deployedPct && (
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  bottom: `${shownPct * 100}%`,
                  height: `${(deployedPct - shownPct) * 100}%`,
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
            <div
              style={{
                position: 'absolute',
                left: -6,
                right: -6,
                top: deployedY - 0.5,
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
                top: deployedY - 9,
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
              deployed · {(deployedPct * 100).toFixed(1)}%
            </div>

            {/* thumb */}
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

            {/* staged ghost bar */}
            {stagedPct != null && (
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

          <div
            style={{
              position: 'absolute',
              left: 44,
              bottom: -2,
              fontFamily: TOKENS.mono,
              fontSize: 10,
              color: TOKENS.ink3,
            }}
          >
            flatten all
          </div>
        </div>

        {/* Info / action panel */}
        <div style={{ flex: 1, minWidth: 280 }}>
          {lastResult && !dragging && stagedPct == null ? (
            <ResultBanner
              result={lastResult}
              accent={accent}
              onDismiss={() => setLastResult(null)}
            />
          ) : stagedPct == null ? (
            <IdleInfo
              ceilingPct={ceilingPct}
              shownPct={shownPct}
              dragging={dragging}
              nav={nav}
              deployedValue={deployedValue}
              accent={accent}
            />
          ) : isFlatten ? (
            <FlattenConfirm
              positionsCount={live.positions.length}
              deployedValue={deployedValue}
              onCancel={cancelStage}
              onConfirm={confirmFlatten}
            />
          ) : (
            <TrimPreview
              stagedPct={stagedPct}
              deployedPct={deployedPct}
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
function IdleInfo({
  ceilingPct,
  shownPct,
  dragging,
  nav,
  deployedValue,
  accent,
}: {
  ceilingPct: number;
  shownPct: number;
  dragging: boolean;
  nav: number;
  deployedValue: number;
  accent: string;
}) {
  const targetValue = nav * shownPct;
  const delta = shownPct - ceilingPct;
  const up = delta > 0.005;
  const down = delta < -0.005;
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
        <Row label="Deployed now" value={money(deployedValue, 12, TOKENS.ink1)} />
        <Row
          label="Free to deploy"
          value={money(Math.max(0, targetValue - deployedValue), 12, up ? accent : TOKENS.ink2)}
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
            Below deployed line — drop to stage a release with position preview.
          </span>
        )}
        {!dragging && (
          <>
            Drag up to raise the ceiling for new positions. Drag below the deployed line to stage a
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
  deployedPct,
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
  deployedPct: number;
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
          {(deployedPct * 100).toFixed(0)}
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
                    pnl {(p.pnl ?? 0) >= 0 ? '+' : '−'}£{Math.abs(p.pnl ?? 0).toFixed(0)}
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
  deployedValue,
  onCancel,
  onConfirm,
}: {
  positionsCount: number;
  deployedValue: number;
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
        Deployed {money(deployedValue, 12, TOKENS.ink1, true)} · ceiling → 0%
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
        <span style={{ color: TOKENS.danger, fontWeight: 500 }}>Backend pending.</span> This lowers
        the ceiling to 0% so no new positions open. Force-close across venues needs{' '}
        <code style={{ fontFamily: TOKENS.mono, color: TOKENS.ink1 }}>POST /positions/flatten</code>{' '}
        — not yet shipped. For now, close per-symbol in Book, or stop the engine via the Power
        control.
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

function ResultBanner({
  result,
  accent,
  onDismiss,
}: {
  result: CapitalResult;
  accent: string;
  onDismiss: () => void;
}) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 5000);
    return () => clearTimeout(t);
  }, [onDismiss]);

  const { kind } = result;
  const tone =
    kind === 'committed' ? accent : kind === 'trim-requested' ? TOKENS.caution : TOKENS.danger;
  const title =
    kind === 'committed'
      ? 'Ceiling updated'
      : kind === 'trim-requested'
        ? 'Ceiling lowered'
        : 'Flatten requested';
  const body =
    kind === 'committed'
      ? 'Applied to trading loop. New positions will respect the new cap.'
      : kind === 'trim-requested'
        ? `New ceiling ${Math.round(result.pct * 100)}%. Engine will unwind excess on its own signals — not a force-close.`
        : 'Ceiling → 0%. No new positions will open. Use Book to close per-symbol or stop the engine.';

  return (
    <div
      style={{
        animation: `ds-slide-up 260ms ${TOKENS.ease}`,
        padding: 14,
        border: `1px solid ${tone}66`,
        borderRadius: 10,
        background: `${tone}0a`,
      }}
    >
      <Label accent={tone}>{title}</Label>
      <div
        style={{
          marginTop: 8,
          fontFamily: TOKENS.sans,
          fontSize: 13,
          color: TOKENS.ink1,
          lineHeight: 1.5,
        }}
      >
        {body}
      </div>
      <button
        type="button"
        onClick={onDismiss}
        style={{
          marginTop: 10,
          padding: '5px 10px',
          background: 'transparent',
          border: `1px solid ${TOKENS.line}`,
          borderRadius: 4,
          color: TOKENS.ink3,
          fontFamily: TOKENS.sans,
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
          cursor: 'pointer',
        }}
      >
        dismiss
      </button>
    </div>
  );
}
