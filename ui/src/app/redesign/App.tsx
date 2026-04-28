/**
 * "Living instrument" App root.
 *
 * Wires the redesign shell to the live trading backend via `useLiveSystem`:
 *   - backend state drives the visual SystemState (off / paused / running / error)
 *   - MasterButton tap/hold calls api.systemStart() / api.systemStop()
 *   - all six screens read real HTTP + WebSocket data
 *
 * Pass ?legacy=1 in the URL to load the previous production shell.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DashboardScreen } from './dashboard';
import { UniverseScreen } from './universe/UniverseScreen';
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
import { useLiveSystem } from './useLiveSystem';

const TITLES: Record<Route, string> = {
  dash:    'Dashboard',
  signals: 'Signals',
  book:    'Book',
  risk:    'Risk',
  strat:   'Strategies',
  universe: 'Universe',
  log:     'Trade log',
};

const TWEAKS_KEY = 'mytbot-redesign-tweaks';
const ROUTE_KEY = 'mytbot-redesign-route';

export default function App() {
  const live = useLiveSystem();

  const [tweaks, setTweaks] = useState<Tweaks>(() => {
    try {
      const raw = localStorage.getItem(TWEAKS_KEY);
      if (raw) {
        const parsed = JSON.parse(raw) as Partial<Tweaks>;
        // System state is always owned by the backend; never restore it from storage.
        const { state: _discard, ...rest } = parsed as Partial<Tweaks> & { state?: unknown };
        void _discard;
        return { ...DEFAULT_TWEAKS, ...rest };
      }
    } catch {
      /* ignore */
    }
    return DEFAULT_TWEAKS;
  });

  useEffect(() => {
    try {
      const { state: _discard, ...persisted } = tweaks as Tweaks & { state?: unknown };
      void _discard;
      localStorage.setItem(TWEAKS_KEY, JSON.stringify(persisted));
    } catch { /* ignore */ }
  }, [tweaks]);

  // Backend is the source of truth for system state.
  useEffect(() => {
    setTweaks((v) => (v.state === live.uiState ? v : { ...v, state: live.uiState }));
  }, [live.uiState]);

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

  /** Universe is intelligence-only; keep sidebar highlight on Dashboard when the stack is idle (same as legacy discovery gating). */
  const universeNavEnabled =
    live.backendState === 'running' || live.backendState === 'starting';
  const effectiveRoute: Route =
    universeNavEnabled || route !== 'universe' ? route : 'dash';
  useEffect(() => {
    if (!universeNavEnabled && route === 'universe') setRoute('dash');
  }, [universeNavEnabled, route]);

  const [cmdOpen, setCmdOpen] = useState(false);
  const [tweaksOpen, setTweaksOpen] = useState(false);
  const [armed, setArmed] = useState(false);

  const accentMain = useMemo(() => ACCENTS[tweaks.accent].main, [tweaks.accent]);

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

  // Power action from button/cmd: start if off, stop if running.
  const togglePower = useCallback(() => {
    if (live.backendState === 'running' || live.backendState === 'starting') {
      void live.stop();
    } else {
      void live.start();
    }
  }, [live]);

  // Clear armed (paused visual) whenever the backend is no longer live.
  useEffect(() => {
    if (live.backendState !== 'running' && live.backendState !== 'starting' && armed) {
      setArmed(false);
    }
  }, [live.backendState, armed]);

  const state = live.uiState;

  const isLight = tweaks.theme === 'light';
  const bg = isLight ? '#f7f7f5' : TOKENS.bg0;

  if (tweaks.viewport === 'mobile') {
    return (
      <div
        data-screen-label={`mobile · ${TITLES[effectiveRoute]}`}
        style={{
          width: '100vw', height: '100vh', background: TOKENS.bg0,
          display: 'flex', flexDirection: 'column', alignItems: 'center',
          overflow: 'auto', padding: '20px 0',
        }}
      >
        <MobileApp
          state={state}
          accent={tweaks.accent}
          armed={armed}
          onArm={setArmed}
          onPower={togglePower}
          live={live}
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
      data-screen-label={`${tweaks.viewport} · ${TITLES[effectiveRoute]}`}
      style={{
        ...containerStyle,
        display: 'flex', background: bg, color: TOKENS.ink1,
        filter: isLight ? 'invert(0.93) hue-rotate(180deg)' : 'none',
      }}
    >
      <Sidebar
        current={effectiveRoute}
        onNav={setRoute}
        accent={accentMain}
        state={state}
        collapsed={tweaks.density === 'compact'}
        loopIteration={live.loopIteration}
        path={live.path}
        universeNavDisabled={!universeNavEnabled}
      />
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <TopBar
          state={state}
          accent={accentMain}
          onArm={setArmed}
          armed={armed}
          onPower={togglePower}
          currentTitle={TITLES[effectiveRoute]}
          onOpenCmd={() => setCmdOpen(true)}
          onOpenTweaks={() => setTweaksOpen((o) => !o)}
          loopIteration={live.loopIteration}
          path={live.path}
          wsConnected={live.wsConnected}
          mode={live.mode}
          onSetMode={(m) => void live.setMode(m)}
        />
        <div style={{ flex: 1, minHeight: 0, overflow: 'hidden', background: TOKENS.bg0, position: 'relative' }}>
          {effectiveRoute === 'dash' && (
            <DashboardScreen
              state={state}
              accent={tweaks.accent}
              density={tweaks.density}
              onArm={setArmed}
              armed={armed}
              live={live}
            />
          )}
          {effectiveRoute === 'signals' && (
            <SignalsScreen accent={tweaks.accent} live={live} />
          )}
          {effectiveRoute === 'book' && (
            <BookScreen accent={tweaks.accent} live={live} />
          )}
          {effectiveRoute === 'risk' && (
            <RiskScreen accent={tweaks.accent} live={live} />
          )}
          {effectiveRoute === 'strat' && (
            <StrategiesScreen accent={tweaks.accent} live={live} />
          )}
          {effectiveRoute === 'universe' && (
            <UniverseScreen accent={tweaks.accent} density={tweaks.density} live={live} />
          )}
          {effectiveRoute === 'log' && (
            <TradeLogScreen live={live} />
          )}
          {state === 'error' && <ErrorOverlay message={live.lastStartError ?? undefined} />}
          {armed && <ArmOverlay />}
        </div>
        <NewsFooterTicker
          news={live.news}
          paused={state === 'off' || state === 'error'}
        />
      </main>
      <CmdPalette
        open={cmdOpen}
        onClose={() => setCmdOpen(false)}
        onNav={setRoute}
        onStart={() => void live.start()}
        onStop={() => void live.stop()}
        onSetMode={(m) => void live.setMode(m)}
        universeNavEnabled={universeNavEnabled}
      />
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
        border: `1px solid ${TOKENS.caution}66`,
      }} />
      <div style={{
        position: 'absolute', top: 16, left: '50%', transform: 'translateX(-50%)',
        padding: '8px 14px', background: TOKENS.bg2, border: `1px solid ${TOKENS.caution}`,
        borderRadius: 8, color: TOKENS.caution,
        fontFamily: TOKENS.sans, fontSize: 12, fontWeight: 500,
        letterSpacing: '0.04em', textTransform: 'uppercase',
      }}>
        paused · hold power to stop
      </div>
    </div>
  );
}

function ErrorOverlay({ message }: { message?: string }) {
  return (
    <div style={{
      position: 'absolute', top: 18, right: 18, zIndex: 40,
      padding: 14, background: TOKENS.bg2, border: `1px solid ${TOKENS.danger}`,
      borderRadius: 10, boxShadow: '0 20px 40px rgba(0,0,0,0.4)', maxWidth: 360,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <Glyph state="error" size={12} />
        <Label style={{ color: TOKENS.danger }}>System in error state</Label>
      </div>
      <div style={{ fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink1, lineHeight: 1.4 }}>
        {message || 'Trading halted. See server logs for details.'}
      </div>
    </div>
  );
}

function NewsFooterTicker({
  news,
  paused,
}: {
  news: Array<{ text: string; src: string; age: string; s: -1 | 0 | 1 }>;
  paused: boolean;
}) {
  const [x, setX] = useState(0);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const rows = news.length > 0 ? news : [{ text: 'Awaiting news feed...', src: 'system', age: '-', s: 0 as const }];
  const newsSignature = useMemo(
    () => rows.map((n) => `${n.src}\u001f${n.text}\u001f${n.s}`).join('\u001e'),
    [rows],
  );

  useEffect(() => {
    setX(0);
  }, [newsSignature]);

  useEffect(() => {
    if (paused || news.length === 0) return;
    let raf = 0;
    let last = 0;
    const speed = 34; // px/s
    const tick = (ts: number) => {
      if (last > 0) {
        const dt = Math.min(80, ts - last);
        setX((prev) => {
          const el = trackRef.current;
          const half = el ? el.scrollWidth / 2 : 0;
          if (half <= 0) return 0;
          const next = prev + (speed * dt) / 1000;
          return next >= half ? next - half : next;
        });
      }
      last = ts;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [newsSignature, news.length, paused]);

  const doubled = [...rows, ...rows];

  return (
    <div style={{ borderTop: `1px solid ${TOKENS.line}`, background: TOKENS.bg1 }}>
      <div style={{ overflow: 'hidden', height: 34, display: 'flex', alignItems: 'center' }}>
        <div
          ref={trackRef}
          style={{
            display: 'flex',
            alignItems: 'center',
            transform: `translateX(-${x}px)`,
            willChange: 'transform',
            height: '100%',
            whiteSpace: 'nowrap',
            lineHeight: 1,
          }}
        >
          {doubled.map((n, i) => (
            <span key={`${n.text}-${i}`} title={`${n.src} | ${n.text}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '0 18px', fontFamily: TOKENS.sans, fontSize: 12, color: TOKENS.ink2, lineHeight: '14px', flex: '0 0 auto' }}>
              <span style={{ width: 5, height: 5, borderRadius: 999, background: n.s > 0 ? TOKENS.profit : n.s < 0 ? TOKENS.loss : TOKENS.ink3 }} />
              <span style={{ textTransform: 'uppercase', letterSpacing: '0.06em', fontSize: 10, color: TOKENS.ink3 }}>{n.src}</span>
              <span style={{ color: TOKENS.ink1 }}>{n.text}</span>
              <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{n.age}</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
