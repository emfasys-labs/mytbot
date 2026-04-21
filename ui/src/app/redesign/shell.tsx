/**
 * App shell: Sidebar, TopBar, MasterButton, CmdPalette, TweaksPanel.
 * Ported from mytbot-design-system/project/prototypes/redesign/shell.jsx.
 */

import {
  CSSProperties,
  MouseEvent as ReactMouseEvent,
  ReactElement,
  useEffect,
  useRef,
  useState,
} from 'react';
import { DATA } from './data';
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
  { id: 'log',     label: 'Trade log',  icon: I.log },
];

export function Sidebar({
  current, onNav, accent, state, collapsed,
}: {
  current: Route;
  onNav: (r: Route) => void;
  accent: string;
  state: SystemState;
  collapsed: boolean;
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
          const active = n.id === current;
          return (
            <button
              key={n.id}
              onClick={() => onNav(n.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: 12, padding: '8px 10px',
                borderRadius: 8, background: active ? 'rgba(255,255,255,0.06)' : 'transparent',
                border: 'none', cursor: 'pointer',
                color: active ? TOKENS.ink0 : TOKENS.ink2,
                fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 450,
                letterSpacing: '-0.01em', textAlign: 'left',
                transition: `background ${TOKENS.fast}ms ${TOKENS.ease}, color ${TOKENS.fast}ms ${TOKENS.ease}`,
              }}
              onMouseEnter={(e: ReactMouseEvent<HTMLButtonElement>) => {
                if (!active) e.currentTarget.style.background = 'rgba(255,255,255,0.03)';
              }}
              onMouseLeave={(e: ReactMouseEvent<HTMLButtonElement>) => {
                if (!active) e.currentTarget.style.background = 'transparent';
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
            path {DATA.path} · #{DATA.loop}
          </div>
        </div>
      )}
    </aside>
  );
}

export function TopBar({
  state, accent, onArm, onPower, armed, currentTitle, onOpenCmd, onOpenTweaks,
}: {
  state: SystemState;
  accent: string;
  onArm: (v: boolean) => void;
  onPower: () => void;
  armed: boolean;
  currentTitle: string;
  onOpenCmd: () => void;
  onOpenTweaks: () => void;
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
        <Glyph state={state} accent={accent} size={10} />
        <span style={{ color: state === 'running' ? accent : state === 'error' ? TOKENS.danger : TOKENS.ink3 }}>{state}</span>
        <span style={{ color: TOKENS.ink4 }}>·</span>
        <span>loop #{DATA.loop}</span>
        <span style={{ color: TOKENS.ink4 }}>·</span>
        <span>{DATA.path}</span>
      </div>
      <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 10 }}>
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
  const holdMs = 900;

  const running = state === 'running';
  const off = state === 'off';
  const danger = armed;
  const c = danger ? TOKENS.danger : running ? accent : off ? TOKENS.ink3 : TOKENS.caution;

  const cancelRaf = () => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  };

  const down = () => {
    if (off) { onPower(); return; }
    setPressing(true);
    startRef.current = performance.now();
    const tick = () => {
      const p = Math.min(1, (performance.now() - startRef.current) / holdMs);
      setProgress(p);
      if (p < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        onArm(true);
        setPressing(false);
      }
    };
    rafRef.current = requestAnimationFrame(tick);
  };

  const up = () => {
    cancelRaf();
    if (pressing) {
      if (progress < 1) onPower();
      setProgress(0);
      setPressing(false);
    }
  };

  const cancel = () => {
    cancelRaf();
    setPressing(false);
    setProgress(0);
  };

  return (
    <div style={{ position: 'relative' }}>
      <button
        onMouseDown={down} onMouseUp={up} onMouseLeave={cancel}
        onTouchStart={down} onTouchEnd={up}
        style={{
          position: 'relative', width: 80, height: 32, borderRadius: 8,
          border: `1px solid ${c}44`, background: `${c}10`,
          color: c, fontFamily: TOKENS.sans, fontSize: 11, fontWeight: 500,
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
          {armed ? 'stop?' : running ? 'live' : off ? 'start' : 'paused'}
        </span>
      </button>
      {pressing && !armed && (
        <div style={{
          position: 'absolute', bottom: -14, left: 0, right: 0,
          fontFamily: TOKENS.mono, fontSize: 9, color: TOKENS.ink3, textAlign: 'center',
        }}>
          hold to arm
        </div>
      )}
    </div>
  );
}

interface CmdItem {
  id: string;
  label: string;
  hint: string;
  kind: 'nav' | 'action' | 'sym';
  route?: Route;
}

const CMD_ITEMS: CmdItem[] = [
  { id: 'dash',        label: 'Go to Dashboard',  hint: 'overview',        kind: 'nav',    route: 'dash' },
  { id: 'signals',     label: 'Go to Signals',    hint: 'conviction',      kind: 'nav',    route: 'signals' },
  { id: 'book',        label: 'Go to Book',       hint: 'positions',       kind: 'nav',    route: 'book' },
  { id: 'risk',        label: 'Go to Risk',       hint: 'approvals',       kind: 'nav',    route: 'risk' },
  { id: 'strat',       label: 'Go to Strategies', hint: 'performance',     kind: 'nav',    route: 'strat' },
  { id: 'log',         label: 'Go to Trade log',  hint: 'events',          kind: 'nav',    route: 'log' },
  { id: 'pause',       label: 'Pause system',     hint: 'soft stop',       kind: 'action' },
  { id: 'flatten',     label: 'Flatten book',     hint: 'close all',       kind: 'action' },
  { id: 'query-nvda',  label: 'NVDA',             hint: 'conviction 0.84', kind: 'sym' },
  { id: 'query-aapl',  label: 'AAPL',             hint: 'conviction 0.71', kind: 'sym' },
];

export function CmdPalette({
  open, onClose, onNav,
}: { open: boolean; onClose: () => void; onNav: (r: Route) => void }) {
  const [q, setQ] = useState('');
  useEffect(() => { if (!open) setQ(''); }, [open]);
  if (!open) return null;

  const filtered = q
    ? CMD_ITEMS.filter((i) =>
        i.label.toLowerCase().includes(q.toLowerCase()) ||
        i.hint.toLowerCase().includes(q.toLowerCase()),
      )
    : CMD_ITEMS;

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
          style={{
            padding: 16, background: 'transparent', border: 'none', outline: 'none',
            color: TOKENS.ink0, fontFamily: TOKENS.sans, fontSize: 15, fontWeight: 400,
            letterSpacing: '-0.01em', borderBottom: `1px solid ${TOKENS.line}`,
          }}
        />
        <div style={{ flex: 1, overflowY: 'auto', padding: 8 }}>
          {filtered.length === 0 ? (
            <div style={{ padding: 14, color: TOKENS.ink3, fontSize: 12 }}>No matches</div>
          ) : filtered.map((it) => (
            <button
              key={it.id}
              onClick={() => { if (it.kind === 'nav' && it.route) onNav(it.route); onClose(); }}
              style={{
                display: 'flex', alignItems: 'center', width: '100%', padding: '10px 12px',
                background: 'transparent', border: 'none', borderRadius: 8, cursor: 'pointer',
                color: TOKENS.ink1, fontFamily: TOKENS.sans, fontSize: 13, textAlign: 'left',
                justifyContent: 'space-between',
              }}
              onMouseEnter={(e: ReactMouseEvent<HTMLButtonElement>) => { e.currentTarget.style.background = 'rgba(255,255,255,0.04)'; }}
              onMouseLeave={(e: ReactMouseEvent<HTMLButtonElement>) => { e.currentTarget.style.background = 'transparent'; }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{
                  fontFamily: TOKENS.mono, fontSize: 9, padding: '2px 6px', borderRadius: 4,
                  background: TOKENS.bg3, color: TOKENS.ink3, textTransform: 'uppercase',
                }}>{it.kind}</span>
                {it.label}
              </span>
              <span style={{ color: TOKENS.ink3, fontSize: 11 }}>{it.hint}</span>
            </button>
          ))}
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
      {renderChoiceRow<SystemState>('System state', ['running', 'paused', 'off', 'error'], tweaks.state, (v) => setK('state', v), row, lbl)}
      {renderChoiceRow<Theme>('Theme', ['dark', 'light'], tweaks.theme, (v) => setK('theme', v), row, lbl)}
      {renderChoiceRow<Viewport>('View', ['desktop', 'tablet', 'mobile'], tweaks.viewport, (v) => setK('viewport', v), { ...row, borderBottom: 'none' }, lbl)}
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
