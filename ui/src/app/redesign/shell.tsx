/**
 * App shell: Sidebar, TopBar, MasterButton, CmdPalette, TweaksPanel.
 */

import {
  CSSProperties,
  MouseEvent as ReactMouseEvent,
  ReactElement,
  useEffect,
  useRef,
  useState,
} from 'react';
import { Glyph, I, Label, Wordmark } from './primitives';
import {
  ACCENTS,
  Density,
  Route,
  SystemState,
  Theme,
  TOKENS,
  Tweaks,
  Viewport,
} from './tokens';
import type { TradingMode } from '../lib/api';

interface NavItem {
  id: Route;
  label: string;
  icon: (p?: Record<string, unknown>) => ReactElement;
}

const NAV: NavItem[] = [
  { id: 'dash',    label: 'Dashboard',  icon: I.dash },
  { id: 'signals', label: 'Signals',    icon: I.signal },
  { id: 'book',    label: 'Book',       icon: I.wallet },
  { id: 'risk',    label: 'Risk',       icon: I.shield },
  { id: 'strat',   label: 'Strategies', icon: I.brain },
  { id: 'universe', label: 'Universe',  icon: I.universe },
  { id: 'log',     label: 'Trade log',  icon: I.log },
];

export function Sidebar({
  current, onNav, accent, state, collapsed, loopIteration, path, universeNavDisabled,
}: {
  current: Route;
  onNav: (r: Route) => void;
  accent: string;
  state: SystemState;
  collapsed: boolean;
  loopIteration: number;
  path: string;
  /** When true, Universe is not reachable (orchestrator not running). */
  universeNavDisabled?: boolean;
}) {
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
        {NAV.map((n) => {
          const uniLock = n.id === 'universe' && universeNavDisabled;
          const active = n.id === current && !uniLock;
          return (
            <button
              key={n.id}
              type="button"
              title={uniLock ? 'Start the system to open Universe' : undefined}
              disabled={uniLock}
              onClick={() => { if (!uniLock) onNav(n.id); }}
              style={{
                display: 'flex', alignItems: 'center', gap: 12, padding: '8px 10px',
                borderRadius: 8, background: active ? 'rgba(255,255,255,0.06)' : 'transparent',
                border: 'none', cursor: uniLock ? 'not-allowed' : 'pointer',
                opacity: uniLock ? 0.42 : 1,
                color: active ? TOKENS.ink0 : TOKENS.ink2,
                fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 450,
                letterSpacing: '-0.01em', textAlign: 'left',
                transition: `background ${TOKENS.fast}ms ${TOKENS.ease}, color ${TOKENS.fast}ms ${TOKENS.ease}`,
              }}
              onMouseEnter={(e: ReactMouseEvent<HTMLButtonElement>) => {
                if (uniLock || active) return;
                e.currentTarget.style.background = 'rgba(255,255,255,0.03)';
              }}
              onMouseLeave={(e: ReactMouseEvent<HTMLButtonElement>) => {
                if (uniLock || active) return;
                e.currentTarget.style.background = 'transparent';
              }}
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
            path {path || '—'} · #{loopIteration || 0}
          </div>
        </div>
      )}
    </aside>
  );
}

export function TopBar({
  state, accent, onArm, onPower, armed, currentTitle, onOpenCmd, onOpenTweaks,
  loopIteration, path, wsConnected, mode, onSetMode,
}: {
  state: SystemState;
  accent: string;
  onArm: (v: boolean) => void;
  onPower: () => void;
  armed: boolean;
  currentTitle: string;
  onOpenCmd: () => void;
  onOpenTweaks: () => void;
  loopIteration: number;
  path: string;
  wsConnected: boolean;
  mode: TradingMode;
  onSetMode: (m: TradingMode) => void;
}) {
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
        {(() => {
          const displayState: SystemState = armed ? 'paused' : state;
          const label =
            displayState === 'starting' ? 'warming up' :
            displayState === 'stopping' ? 'shutting down' :
            displayState;
          const color =
            displayState === 'running'  ? accent :
            displayState === 'error'    ? TOKENS.danger :
            displayState === 'starting' ? TOKENS.caution :
            displayState === 'stopping' ? TOKENS.caution :
            displayState === 'paused'   ? TOKENS.caution :
            TOKENS.ink3;
          return (
            <>
              <Glyph state={displayState} accent={accent} size={10} />
              <span style={{ color }}>{label}</span>
            </>
          );
        })()}
        <span style={{ color: TOKENS.ink4 }}>·</span>
        <span>loop #{loopIteration || 0}</span>
        <span style={{ color: TOKENS.ink4 }}>·</span>
        <span>{path || '—'}</span>
        <span style={{ color: TOKENS.ink4 }}>·</span>
        <span
          title={wsConnected ? 'WebSocket live' : 'WebSocket disconnected'}
          style={{ color: wsConnected ? accent : TOKENS.ink4 }}
        >
          {wsConnected ? 'ws' : 'ws·off'}
        </span>
      </div>
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 4, padding: 2,
          borderRadius: 8, border: `1px solid ${TOKENS.line}`, background: TOKENS.bg1,
        }}>
          {(['defender', 'trader', 'hunter'] as const).map((m) => {
            const active = mode === m;
            return (
              <button
                key={m}
                onClick={() => onSetMode(m)}
                style={{
                  padding: '4px 8px',
                  borderRadius: 6,
                  border: `1px solid ${active ? `${accent}55` : TOKENS.line}`,
                  background: active ? `${accent}18` : 'transparent',
                  color: active ? accent : TOKENS.ink3,
                  fontFamily: TOKENS.mono,
                  fontSize: 10,
                  letterSpacing: '0.04em',
                  textTransform: 'uppercase',
                  cursor: 'pointer',
                }}
                title={`Set mode: ${m}`}
              >
                {m}
              </button>
            );
          })}
        </div>
        <button
          onClick={onOpenCmd}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
            background: TOKENS.bg2, border: `1px solid ${TOKENS.line}`, borderRadius: 8,
            color: TOKENS.ink2, fontFamily: TOKENS.sans, fontSize: 12, cursor: 'pointer',
            letterSpacing: '-0.01em',
          }}
        >
          <span style={{ color: TOKENS.ink3 }}>Search, jump, command</span>
          <span style={{
            fontFamily: TOKENS.mono, fontSize: 10, padding: '1px 5px',
            borderRadius: 4, background: TOKENS.bg3, color: TOKENS.ink3,
          }}>⌘K</span>
        </button>
        <button
          onClick={onOpenTweaks}
          title="Tweaks"
          style={{
            padding: '6px 10px', background: TOKENS.bg2, border: `1px solid ${TOKENS.line}`,
            borderRadius: 8, color: TOKENS.ink2, fontFamily: TOKENS.sans, fontSize: 12,
            cursor: 'pointer', letterSpacing: '-0.01em',
          }}
        >
          Tweaks
        </button>
        <MasterButton state={state} accent={accent} onArm={onArm} onPower={onPower} armed={armed} />
      </div>
    </div>
  );
}

export function MasterButton({
  state, accent, onArm, onPower, armed,
}: {
  state: SystemState;
  accent: string;
  onArm: (v: boolean) => void;
  onPower: () => void;
  armed: boolean;
}) {
  const [pressing, setPressing] = useState(false);
  const [progress, setProgress] = useState(0);
  const rafRef = useRef<number | null>(null);
  const startRef = useRef(0);
  const pressingRef = useRef(false);
  const progressRef = useRef(0);
  const holdMs = 900;

  const running = state === 'running';
  const off = state === 'off';
  const starting = state === 'starting';
  const stopping = state === 'stopping';
  const holding = pressing;
  const c = holding
    ? TOKENS.danger
    : armed
      ? TOKENS.caution
      : running
        ? accent
        : starting
          ? TOKENS.caution
          : stopping
            ? TOKENS.caution
          : off
            ? TOKENS.ink3
            : TOKENS.caution;
  const borderColor = off ? TOKENS.line : `${c}44`;
  const bgColor = off ? 'transparent' : `${c}10`;
  const textColor = off ? TOKENS.ink2 : c;

  const cancelRaf = () => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  };

  const down = () => {
    if (off) { onPower(); return; }
    pressingRef.current = true;
    progressRef.current = 0;
    setPressing(true);
    startRef.current = performance.now();
    const tick = () => {
      const p = Math.min(1, (performance.now() - startRef.current) / holdMs);
      progressRef.current = p;
      setProgress(p);
      if (p < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        pressingRef.current = false;
        onArm(false);
        onPower();
        setPressing(false);
        setProgress(0);
      }
    };
    rafRef.current = requestAnimationFrame(tick);
  };

  const up = () => {
    cancelRaf();
    const wasPressing = pressingRef.current;
    const p = progressRef.current;
    pressingRef.current = false;
    progressRef.current = 0;
    if (!wasPressing) return;
    if (p < 1) onArm(!armed);
    setProgress(0);
    setPressing(false);
  };

  const cancel = () => {
    cancelRaf();
    pressingRef.current = false;
    progressRef.current = 0;
    setPressing(false);
    setProgress(0);
  };

  return (
    <div style={{ position: 'relative' }}>
      <button
        onMouseDown={down} onMouseUp={up} onMouseLeave={cancel}
        onTouchStart={down} onTouchEnd={up}
        style={{
          position: 'relative', width: 110, height: 32, borderRadius: 8,
          border: `1px solid ${borderColor}`, background: bgColor,
          color: textColor, fontFamily: TOKENS.sans, fontSize: 11, fontWeight: 500,
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
          {armed
            ? 'paused'
            : running
              ? 'live'
              : starting
                ? 'warming up'
                : stopping
                  ? 'stopping'
                : off
                  ? 'start'
                  : 'error'}
        </span>
      </button>
    </div>
  );
}

interface CmdItem {
  id: string;
  label: string;
  hint: string;
  kind: 'nav' | 'action' | 'mode';
  route?: Route;
  action?: 'start' | 'stop';
  mode?: TradingMode;
}

const CMD_ITEMS: CmdItem[] = [
  { id: 'dash',        label: 'Go to Dashboard',  hint: 'overview',       kind: 'nav',    route: 'dash' },
  { id: 'signals',     label: 'Go to Signals',    hint: 'conviction',     kind: 'nav',    route: 'signals' },
  { id: 'book',        label: 'Go to Book',       hint: 'positions',      kind: 'nav',    route: 'book' },
  { id: 'risk',        label: 'Go to Risk',       hint: 'approvals',      kind: 'nav',    route: 'risk' },
  { id: 'strat',       label: 'Go to Strategies', hint: 'performance',    kind: 'nav',    route: 'strat' },
  { id: 'universe',    label: 'Go to Universe',   hint: 'tiers · funnel', kind: 'nav',    route: 'universe' },
  { id: 'log',         label: 'Go to Trade log',  hint: 'events',         kind: 'nav',    route: 'log' },
  { id: 'start',       label: 'Start system',     hint: 'api /system/start', kind: 'action', action: 'start' },
  { id: 'stop',        label: 'Stop system',      hint: 'api /system/stop',  kind: 'action', action: 'stop' },
  { id: 'mode-trader',   label: 'Mode · trader',   hint: 'normal trading', kind: 'mode', mode: 'trader' },
  { id: 'mode-defender', label: 'Mode · defender', hint: 'defensive',      kind: 'mode', mode: 'defender' },
  { id: 'mode-hunter',   label: 'Mode · hunter',   hint: 'aggressive',     kind: 'mode', mode: 'hunter' },
];

export function CmdPalette({
  open, onClose, onNav, onStart, onStop, onSetMode, universeNavEnabled = true,
}: {
  open: boolean;
  onClose: () => void;
  onNav: (r: Route) => void;
  onStart: () => void;
  onStop: () => void;
  onSetMode: (m: TradingMode) => void;
  universeNavEnabled?: boolean;
}) {
  const [q, setQ] = useState('');
  useEffect(() => { if (!open) setQ(''); }, [open]);
  if (!open) return null;

  const filtered = q
    ? CMD_ITEMS.filter((i) =>
        i.label.toLowerCase().includes(q.toLowerCase()) ||
        i.hint.toLowerCase().includes(q.toLowerCase()),
      )
    : CMD_ITEMS;

  const execute = (it: CmdItem) => {
    if (it.kind === 'nav' && it.route === 'universe' && !universeNavEnabled) return;
    if (it.kind === 'nav' && it.route) onNav(it.route);
    if (it.kind === 'action' && it.action === 'start') onStart();
    if (it.kind === 'action' && it.action === 'stop') onStop();
    if (it.kind === 'mode' && it.mode) onSetMode(it.mode);
    onClose();
  };

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
        backdropFilter: 'blur(12px)', zIndex: 100, display: 'flex', justifyContent: 'center',
        paddingTop: '12vh', animation: `ds-fade-in 160ms ease`,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 520, maxHeight: '60vh', background: TOKENS.bg1,
          border: `1px solid ${TOKENS.lineStrong}`, borderRadius: 12,
          boxShadow: '0 20px 60px rgba(0,0,0,0.6)', overflow: 'hidden',
          display: 'flex', flexDirection: 'column',
        }}
      >
        <input
          autoFocus value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="Search, jump, run commands…"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && filtered[0]) { execute(filtered[0]); }
          }}
          style={{
            padding: 16, background: 'transparent', border: 'none', outline: 'none',
            color: TOKENS.ink0, fontFamily: TOKENS.sans, fontSize: 15, fontWeight: 400,
            letterSpacing: '-0.01em', borderBottom: `1px solid ${TOKENS.line}`,
          }}
        />
        <div style={{ flex: 1, overflowY: 'auto', padding: 8 }}>
          {filtered.length === 0 ? (
            <div style={{ padding: 14, color: TOKENS.ink3, fontSize: 12 }}>No matches</div>
          ) : filtered.map((it) => {
            const uniLock = it.id === 'universe' && !universeNavEnabled;
            return (
            <button
              key={it.id}
              type="button"
              title={uniLock ? 'Start the system to open Universe' : undefined}
              disabled={uniLock}
              onClick={() => execute(it)}
              style={{
                display: 'flex', alignItems: 'center', width: '100%', padding: '10px 12px',
                background: 'transparent', border: 'none', borderRadius: 8,
                cursor: uniLock ? 'not-allowed' : 'pointer',
                opacity: uniLock ? 0.45 : 1,
                color: TOKENS.ink1, fontFamily: TOKENS.sans, fontSize: 13, textAlign: 'left',
                justifyContent: 'space-between',
              }}
              onMouseEnter={(e: ReactMouseEvent<HTMLButtonElement>) => {
                if (uniLock) return;
                e.currentTarget.style.background = 'rgba(255,255,255,0.04)';
              }}
              onMouseLeave={(e: ReactMouseEvent<HTMLButtonElement>) => {
                e.currentTarget.style.background = 'transparent';
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{
                  fontFamily: TOKENS.mono, fontSize: 9, padding: '2px 6px', borderRadius: 4,
                  background: TOKENS.bg3, color: TOKENS.ink3, textTransform: 'uppercase',
                }}>{it.kind}</span>
                {it.label}
              </span>
              <span style={{ color: TOKENS.ink3, fontSize: 11 }}>{uniLock ? 'start system' : it.hint}</span>
            </button>
          );})}
        </div>
      </div>
    </div>
  );
}

export function TweaksPanel({
  open, onClose, tweaks, setTweaks,
}: {
  open: boolean;
  onClose: () => void;
  tweaks: Tweaks;
  setTweaks: (t: Tweaks) => void;
}) {
  if (!open) return null;

  const row: CSSProperties = {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '8px 0', borderBottom: `1px solid ${TOKENS.line}`, gap: 10,
  };
  const lbl: CSSProperties = {
    fontFamily: TOKENS.sans, fontSize: 11, color: TOKENS.ink2, letterSpacing: '-0.01em',
  };

  const setK = <K extends keyof Tweaks>(k: K, v: Tweaks[K]) => setTweaks({ ...tweaks, [k]: v });

  return (
    <div style={{
      position: 'fixed', bottom: 18, right: 18, width: 260,
      background: TOKENS.bg1, border: `1px solid ${TOKENS.lineStrong}`,
      borderRadius: 12, padding: 14, zIndex: 80,
      boxShadow: '0 20px 40px rgba(0,0,0,0.5)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
        <Label>Tweaks</Label>
        <button onClick={onClose} style={{ background: 'none', border: 'none', color: TOKENS.ink3, cursor: 'pointer', padding: 2 }}>
          <I.x />
        </button>
      </div>

      <div style={row}>
        <span style={lbl}>Accent</span>
        <div style={{ display: 'flex', gap: 4 }}>
          {(Object.keys(ACCENTS) as Array<keyof typeof ACCENTS>).map((k) => (
            <button
              key={k}
              onClick={() => setK('accent', k)}
              style={{
                width: 18, height: 18, borderRadius: 999,
                border: `1.5px solid ${tweaks.accent === k ? TOKENS.ink0 : 'transparent'}`,
                background: ACCENTS[k].main, cursor: 'pointer', padding: 0,
              }}
              title={k}
            />
          ))}
        </div>
      </div>

      {renderChoiceRow<Density>('Density', ['comfort', 'compact'], tweaks.density, (v) => setK('density', v), row, lbl)}
      {renderChoiceRow<Theme>('Theme', ['dark', 'light'], tweaks.theme, (v) => setK('theme', v), row, lbl)}
      {renderChoiceRow<Viewport>('View', ['desktop', 'tablet', 'mobile'], tweaks.viewport, (v) => setK('viewport', v), { ...row, borderBottom: 'none' }, lbl)}

      <div style={{ marginTop: 8, paddingTop: 8, borderTop: `1px solid ${TOKENS.line}`, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink4 }}>
        system state is controlled by the backend · use the master button or ⌘K
      </div>
    </div>
  );
}

function renderChoiceRow<T extends string>(
  label: string,
  options: readonly T[],
  value: T,
  onChange: (v: T) => void,
  rowStyle: CSSProperties,
  lblStyle: CSSProperties,
) {
  return (
    <div style={rowStyle}>
      <span style={lblStyle}>{label}</span>
      <div style={{ display: 'flex', gap: 4 }}>
        {options.map((o) => (
          <button
            key={o}
            onClick={() => onChange(o)}
            style={{
              padding: '3px 8px', fontSize: 10, borderRadius: 4,
              background: value === o ? 'rgba(255,255,255,0.08)' : 'transparent',
              color: value === o ? TOKENS.ink0 : TOKENS.ink3,
              border: `1px solid ${value === o ? TOKENS.lineStrong : TOKENS.line}`,
              cursor: 'pointer', fontFamily: TOKENS.sans,
            }}
          >
            {o}
          </button>
        ))}
      </div>
    </div>
  );
}
