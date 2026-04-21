/**
 * "Living instrument" App root.
 * Ported from mytbot-design-system/project/prototypes/redesign/app.jsx.
 *
 * This is the design-handoff redesign the user landed on — a full app shell with
 * sidebar, command palette, 6 screens, mobile/tablet viewports, and a tweakable
 * accent/state/theme/density. The prototype runs on fake demo data; real-API
 * wiring lives in ui/src/app/App.tsx (legacy shell) and can be integrated per
 * panel as the redesign is validated.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { DashboardScreen } from './dashboard';
import { MobileApp } from './mobile';
import { Glyph, Label } from './primitives';
import {
  BookScreen,
  RiskScreen,
  SignalsScreen,
  StrategiesScreen,
  TradeLogScreen,
} from './screens';
import { CmdPalette, Sidebar, TopBar, TweaksPanel } from './shell';
import {
  ACCENTS,
  DEFAULT_TWEAKS,
  Route,
  TOKENS,
  Tweaks,
} from './tokens';

const TITLES: Record<Route, string> = {
  dash:    'Dashboard',
  signals: 'Signals',
  book:    'Book',
  risk:    'Risk',
  strat:   'Strategies',
  log:     'Trade log',
};

const TWEAKS_KEY = 'mytbot-redesign-tweaks';
const ROUTE_KEY = 'mytbot-redesign-route';

export default function App() {
  const [tweaks, setTweaks] = useState<Tweaks>(() => {
    try {
      const raw = localStorage.getItem(TWEAKS_KEY);
      if (raw) return { ...DEFAULT_TWEAKS, ...(JSON.parse(raw) as Partial<Tweaks>) };
    } catch {
      /* ignore */
    }
    return DEFAULT_TWEAKS;
  });

  useEffect(() => {
    try { localStorage.setItem(TWEAKS_KEY, JSON.stringify(tweaks)); } catch { /* ignore */ }
  }, [tweaks]);

  const [route, setRoute] = useState<Route>(() => {
    try {
      const r = localStorage.getItem(ROUTE_KEY) as Route | null;
      if (r && r in TITLES) return r;
    } catch { /* ignore */ }
    return 'dash';
  });

  useEffect(() => {
    try { localStorage.setItem(ROUTE_KEY, route); } catch { /* ignore */ }
  }, [route]);

  const [cmdOpen, setCmdOpen] = useState(false);
  const [tweaksOpen, setTweaksOpen] = useState(false);
  const [armed, setArmed] = useState(false);

  const accentMain = useMemo(() => ACCENTS[tweaks.accent].main, [tweaks.accent]);

  // ⌘K shortcut and Escape to close
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setCmdOpen((o) => !o);
      }
      if (e.key === 'Escape') {
        setCmdOpen(false);
        setArmed(false);
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  // After long-press arm → kill in 1.8s.
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => {
      setArmed(false);
      setTweaks((v) => ({ ...v, state: 'off' }));
    }, 1800);
    return () => clearTimeout(t);
  }, [armed]);

  const togglePower = useCallback(() => {
    setTweaks((v) => ({ ...v, state: v.state === 'running' ? 'paused' : 'running' }));
  }, []);

  const isLight = tweaks.theme === 'light';
  const bg = isLight ? '#f7f7f5' : TOKENS.bg0;

  if (tweaks.viewport === 'mobile') {
    return (
      <div
        data-screen-label={`mobile · ${TITLES[route]}`}
        style={{
          width: '100vw', height: '100vh', background: TOKENS.bg0,
          display: 'flex', flexDirection: 'column', alignItems: 'center',
          overflow: 'auto', padding: '20px 0',
        }}
      >
        <MobileApp
          state={tweaks.state}
          accent={tweaks.accent}
          armed={armed}
          onArm={setArmed}
          onPower={togglePower}
        />
        <TweaksPanel
          open={tweaksOpen}
          onClose={() => setTweaksOpen(false)}
          tweaks={tweaks}
          setTweaks={setTweaks}
        />
        <TweaksToggle onClick={() => setTweaksOpen((o) => !o)} />
      </div>
    );
  }

  const containerStyle = tweaks.viewport === 'tablet'
    ? { width: 1024, height: 768, margin: '20px auto', border: `1px solid ${TOKENS.lineStrong}`, borderRadius: 12, overflow: 'hidden' as const }
    : { width: '100vw', height: '100vh' };

  return (
    <div
      data-screen-label={`${tweaks.viewport} · ${TITLES[route]}`}
      style={{
        ...containerStyle,
        display: 'flex', background: bg, color: TOKENS.ink1,
        filter: isLight ? 'invert(0.93) hue-rotate(180deg)' : 'none',
      }}
    >
      <Sidebar
        current={route}
        onNav={setRoute}
        accent={accentMain}
        state={tweaks.state}
        collapsed={tweaks.density === 'compact'}
      />
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <TopBar
          state={tweaks.state}
          accent={accentMain}
          onArm={setArmed}
          armed={armed}
          onPower={togglePower}
          currentTitle={TITLES[route]}
          onOpenCmd={() => setCmdOpen(true)}
          onOpenTweaks={() => setTweaksOpen((o) => !o)}
        />
        <div style={{ flex: 1, minHeight: 0, background: TOKENS.bg0, position: 'relative' }}>
          {route === 'dash'    && <DashboardScreen  state={tweaks.state} accent={tweaks.accent} density={tweaks.density} onArm={setArmed} armed={armed} />}
          {route === 'signals' && <SignalsScreen    accent={tweaks.accent} />}
          {route === 'book'    && <BookScreen       accent={tweaks.accent} />}
          {route === 'risk'    && <RiskScreen       accent={tweaks.accent} />}
          {route === 'strat'   && <StrategiesScreen accent={tweaks.accent} />}
          {route === 'log'     && <TradeLogScreen />}
          {tweaks.state === 'error' && <ErrorOverlay />}
          {armed && <ArmOverlay />}
        </div>
      </main>
      <CmdPalette open={cmdOpen} onClose={() => setCmdOpen(false)} onNav={setRoute} />
      <TweaksPanel open={tweaksOpen} onClose={() => setTweaksOpen(false)} tweaks={tweaks} setTweaks={setTweaks} />
    </div>
  );
}

function TweaksToggle({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        position: 'fixed', bottom: 18, right: 18, zIndex: 70,
        padding: '6px 12px', background: TOKENS.bg2, border: `1px solid ${TOKENS.lineStrong}`,
        borderRadius: 8, color: TOKENS.ink2, fontFamily: TOKENS.sans, fontSize: 12, cursor: 'pointer',
      }}
    >
      Tweaks
    </button>
  );
}

function ArmOverlay() {
  return (
    <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 50 }}>
      <div style={{
        position: 'absolute', inset: 0,
        border: `2px solid ${TOKENS.danger}`,
        animation: 'ds-pulse 0.9s ease-in-out infinite',
      }} />
      <div style={{
        position: 'absolute', top: 16, left: '50%', transform: 'translateX(-50%)',
        padding: '8px 14px', background: TOKENS.bg2, border: `1px solid ${TOKENS.danger}`,
        borderRadius: 8, color: TOKENS.danger,
        fontFamily: TOKENS.sans, fontSize: 12, fontWeight: 500,
        letterSpacing: '0.04em', textTransform: 'uppercase',
      }}>
        stopping · flattening book
      </div>
    </div>
  );
}

function ErrorOverlay() {
  return (
    <div style={{
      position: 'absolute', top: 18, right: 18, zIndex: 40,
      padding: 14, background: TOKENS.bg2, border: `1px solid ${TOKENS.danger}`,
      borderRadius: 10, boxShadow: '0 20px 40px rgba(0,0,0,0.4)', maxWidth: 320,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <Glyph state="error" size={12} />
        <Label style={{ color: TOKENS.danger }}>Broker disconnected</Label>
      </div>
      <div style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink1, lineHeight: 1.4 }}>
        ibkr-gateway unreachable for 45s. Trading halted. Positions preserved. Retrying in 12s.
      </div>
    </div>
  );
}
