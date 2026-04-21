/**
 * useLiveSystem — single source of truth for the redesign shell.
 *
 * Wraps HTTP polling + WebSocket tick stream against the live backend
 * (api/server.py) and exposes normalized state for the "living instrument"
 * screens. Keeps the redesign decoupled from the legacy shell while reusing
 * the same endpoints and WS contract.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  api,
  setDashboardReadToken,
  toNumber,
  type ApiNewsResponse,
  type ApiOrderRow,
  type ApiPnlResponse,
  type ApiPositionsResponse,
  type DashboardSnapshot,
  type IntelligenceSignalsResponse,
  type SystemState as BackendSystemState,
  type TradingMode,
} from '../lib/api';
import { eventTimestamp, formatWsEventLine, getWsUrl, type WsTickEvent, type WsTickMessage } from '../lib/ws';
import {
  estimateNavOpen,
  equityPeak,
  mapApprovedRejected,
  mapBrokers,
  mapConviction,
  mapExposure,
  mapNews,
  mapOrderEvent,
  mapPnlRollups,
  mapPositions,
  mapStrategies,
  mergeStrategiesWithSignals,
  mapSystemState,
} from './mapping';
import type { Approved, BrokerStatus, Conviction, LiveEvent, NewsRow, Position, Rejected, Strategy } from './data';
import type { SystemState as DesignSystemState } from './tokens';

const REFRESH_INTERVAL_MS = 10_000;
const INTEL_THROTTLE_MS = 12_000;

export interface LiveData {
  backendState: BackendSystemState;
  uiState: DesignSystemState;
  killSwitch: boolean;
  wsConnected: boolean;
  snapshotFetchFailed: boolean;
  lastStartError: string | null;

  brokers: BrokerStatus[];
  activeBrokers: string[];

  nav: number;
  navOpen: number;
  navPeak: number;
  pnl: ApiPnlResponse | null;
  pnlRollups: { d: number; w: number; m: number; y: number };
  tradableCapital: number | null;
  capitalPct: number;

  exposure: { gross: number; net: number; cash: number };
  equity: number[];
  equitySeries: Array<{ date: string; value: number }>;

  snapshot: DashboardSnapshot | null;
  conviction: Conviction[];
  positions: Position[];
  approved: Approved[];
  rejected: Rejected[];
  strategies: Strategy[];
  events: LiveEvent[];
  eventLines: string[];
  news: NewsRow[];
  orders: ApiOrderRow[];
  intelligence: IntelligenceSignalsResponse | null;

  loopIteration: number;
  path: string;
  mode: TradingMode;

  start: () => Promise<void>;
  stop: () => Promise<void>;
  setMode: (m: TradingMode) => Promise<void>;
  setCapitalPct: (p: number) => Promise<void>;
  refresh: () => void;
}

type PositionChange = { symbol: string; change: number };

function toPositionChanges(pos: ApiPositionsResponse | null): PositionChange[] {
  const rows = pos?.positions ?? [];
  return rows.slice(0, 24).map((p) => {
    const entry = toNumber(p.avg_entry_price, 0);
    const current = toNumber(p.current_price, 0);
    const unreal = toNumber(p.unrealised_pnl, 0);
    const pct = entry > 0 ? ((current - entry) / entry) * 100 : unreal >= 0 ? 0.5 : -0.5;
    return { symbol: p.symbol, change: Number.isFinite(pct) ? pct : 0 };
  });
}

export function useLiveSystem(): LiveData {
  // ────── core state ──────
  const [backendState, setBackendState] = useState<BackendSystemState>('off');
  const [killSwitch, setKillSwitch] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [snapshotFetchFailed, setSnapshotFetchFailed] = useState(false);
  const [lastStartError, setLastStartError] = useState<string | null>(null);
  const [activeBrokers, setActiveBrokers] = useState<string[]>([]);
  const [brokersRaw, setBrokersRaw] = useState<Record<string, { configured: boolean; connected: boolean; balance_ready?: boolean }>>({});
  const [mode, setModeState] = useState<TradingMode>('trader');
  const [capitalPct, setCapitalPctState] = useState<number>(() => {
    try {
      const raw = parseFloat(localStorage.getItem('mytbot_capital_pct') ?? '');
      return Number.isFinite(raw) && raw >= 0 && raw <= 1 ? raw : 1;
    } catch { return 1; }
  });

  const [pnl, setPnl] = useState<ApiPnlResponse | null>(null);
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [positionsRaw, setPositionsRaw] = useState<ApiPositionsResponse | null>(null);
  const [equitySeries, setEquitySeries] = useState<Array<{ date: string; value: number }>>([]);
  const [news, setNews] = useState<ApiNewsResponse | null>(null);
  const [orders, setOrders] = useState<ApiOrderRow[]>([]);
  const [intelligence, setIntelligence] = useState<IntelligenceSignalsResponse | null>(null);
  const [wsEvents, setWsEvents] = useState<WsTickEvent[]>([]);
  const [orderEvents, setOrderEvents] = useState<LiveEvent[]>([]);

  // ────── refs ──────
  const refreshLock = useRef(false);
  const refreshPending = useRef(false);
  const lastIntelRefresh = useRef(0);
  const stateRef = useRef(backendState);
  useEffect(() => { stateRef.current = backendState; }, [backendState]);

  // ────── clear ephemeral live data when we're off ──────
  const clearLive = useCallback(() => {
    setSnapshot(null);
    setPositionsRaw(null);
    setEquitySeries([]);
    setNews(null);
    setOrders([]);
    setIntelligence(null);
    setWsEvents([]);
    setOrderEvents([]);
    setPnl(null);
    setActiveBrokers([]);
    setSnapshotFetchFailed(false);
    setKillSwitch(false);
  }, []);

  // ────── dashboard_token=… bootstrap (one-time) ──────
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const t = params.get('dashboard_token')?.trim();
    if (!t) return;
    setDashboardReadToken(t);
    params.delete('dashboard_token');
    const q = params.toString();
    window.history.replaceState({}, '', `${window.location.pathname}${q ? `?${q}` : ''}${window.location.hash}`);
  }, []);

  // ────── HTTP refresh ──────
  const refresh = useCallback(async () => {
    if (refreshLock.current) { refreshPending.current = true; return; }
    refreshLock.current = true;
    try {
      const res = await Promise.allSettled([
        api.getPnl(),
        api.getPnlHistory(90),
        api.getPositions(16),
        api.getStatus(),
        api.getSystemStatus(),
        api.getNews(30),
        api.getDashboardSnapshot(),
        api.getOrders(50),
      ]);
      const pnlRes = res[0].status === 'fulfilled' ? res[0].value : null;
      const histRes = res[1].status === 'fulfilled' ? res[1].value : null;
      const posRes = res[2].status === 'fulfilled' ? res[2].value : null;
      const statusRes = res[3].status === 'fulfilled' ? res[3].value : null;
      const sysRes = res[4].status === 'fulfilled' ? res[4].value : null;
      const newsRes = res[5].status === 'fulfilled' ? res[5].value : null;
      const snapRes = res[6].status === 'fulfilled' ? res[6].value : null;
      const ordRes = res[7].status === 'fulfilled' ? res[7].value : null;

      if (sysRes) {
        const newState: BackendSystemState = sysRes.state ?? 'off';
        setBackendState(newState);
        setLastStartError(
          typeof sysRes.last_start_error === 'string' && sysRes.last_start_error.trim()
            ? sysRes.last_start_error.trim() : null,
        );
        if (Array.isArray(sysRes.active_brokers)) setActiveBrokers(sysRes.active_brokers);
        if (sysRes.brokers) setBrokersRaw(sysRes.brokers as Record<string, { configured: boolean; connected: boolean; balance_ready?: boolean }>);
        if (typeof sysRes.capital_pct === 'number' && Number.isFinite(sysRes.capital_pct)) {
          const c = Math.max(0, Math.min(1, sysRes.capital_pct));
          setCapitalPctState(c);
          try { localStorage.setItem('mytbot_capital_pct', String(c)); } catch { /* ignore */ }
        }
      }
      const feedsLive = (sysRes?.state ?? stateRef.current) === 'running';

      if (statusRes) setKillSwitch(!!statusRes.kill_switch);

      if (feedsLive) {
        if (pnlRes) setPnl(pnlRes);
        if (histRes) {
          const series = (histRes.history || [])
            .map((x) => ({ date: String(x.date ?? ''), value: toNumber(x.portfolio_value, 0) }))
            .filter((x) => x.date && Number.isFinite(x.value));
          setEquitySeries(series);
        }
        if (posRes) setPositionsRaw(posRes);
        if (newsRes) setNews(newsRes);
        if (ordRes) {
          const rows = (ordRes as { orders?: ApiOrderRow[] }).orders ?? [];
          setOrders(rows);
          // Merge fresh order events (last 40) into our event log.
          const newEvents = rows
            .slice(0, 40)
            .map(mapOrderEvent)
            .filter((x): x is LiveEvent => !!x);
          setOrderEvents(newEvents);
        }
        if (res[6].status === 'fulfilled' && snapRes) {
          setSnapshotFetchFailed(false);
          setSnapshot(snapRes);
        } else if (res[6].status === 'rejected') {
          setSnapshotFetchFailed(true);
        }
      } else {
        clearLive();
      }

      // Throttled intelligence / mode fetch.
      if (feedsLive && Date.now() - lastIntelRefresh.current > INTEL_THROTTLE_MS) {
        lastIntelRefresh.current = Date.now();
        const [sig, modeRes] = await Promise.allSettled([
          api.getIntelligenceSignals(16),
          api.getSystemMode(),
        ]);
        if (sig.status === 'fulfilled') setIntelligence(sig.value);
        if (modeRes.status === 'fulfilled' && modeRes.value.mode) setModeState(modeRes.value.mode);
      }
    } catch {
      /* keep last-known state */
    } finally {
      refreshLock.current = false;
      if (refreshPending.current) { refreshPending.current = false; void refresh(); }
    }
  }, [clearLive]);

  useEffect(() => { void refresh(); }, [refresh]);

  // ────── Polling fallback when WS is down ──────
  useEffect(() => {
    if (wsConnected) return;
    const t = window.setInterval(() => { void refresh(); }, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(t);
  }, [wsConnected, refresh]);

  // ────── WebSocket stream ──────
  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let stopped = false;

    const connect = async () => {
      if (stopped) return;
      try {
        const url = await getWsUrl();
        ws = new WebSocket(url);
      } catch {
        scheduleReconnect();
        return;
      }
      ws.onopen = () => {
        attempt = 0;
        setWsConnected(true);
        void refresh();
      };
      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data as string) as WsTickMessage;
          if (msg.type !== 'tick' || !msg.payload) return;
          const evs = msg.payload.events;
          if (evs && evs.length) setWsEvents(evs.slice(-40));
          const sys = msg.payload.system as Record<string, unknown> | undefined;
          const wsKill = !!msg.payload.status?.kill_switch;
          setKillSwitch(wsKill);
          if (sys?.state && typeof sys.state === 'string') setBackendState(sys.state as BackendSystemState);
          if (sys?.active_brokers && Array.isArray(sys.active_brokers)) setActiveBrokers(sys.active_brokers as string[]);
          if (sys?.brokers && typeof sys.brokers === 'object') setBrokersRaw(sys.brokers as Record<string, { configured: boolean; connected: boolean; balance_ready?: boolean }>);
          void refresh();
        } catch { /* malformed frame */ }
      };
      ws.onerror = () => { /* onclose will run */ };
      ws.onclose = () => { setWsConnected(false); ws = null; scheduleReconnect(); };
    };
    const scheduleReconnect = () => {
      if (stopped) return;
      const delay = Math.min(30_000, 800 * 2 ** attempt);
      attempt += 1;
      reconnect = setTimeout(() => { reconnect = null; connect(); }, delay);
    };
    connect();
    return () => {
      stopped = true;
      if (reconnect) clearTimeout(reconnect);
      if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
      setWsConnected(false);
    };
  }, [refresh]);

  // ────── Actions ──────
  const start = useCallback(async () => {
    try {
      const r = await api.systemStart();
      if (r.state) setBackendState(r.state);
      if (Array.isArray(r.active_brokers)) setActiveBrokers(r.active_brokers);
    } catch {
      setBackendState('error');
    }
  }, []);

  const stop = useCallback(async () => {
    try {
      const r = await api.systemStop();
      if (r.state) setBackendState(r.state);
      clearLive();
    } catch {
      /* ignore — status poll will eventually reflect reality */
    }
  }, [clearLive]);

  const setMode = useCallback(async (m: TradingMode) => {
    setModeState(m);
    try { await api.setSystemMode(m); } catch { /* ignore */ }
  }, []);

  const setCapitalPct = useCallback(async (p: number) => {
    const c = Math.max(0, Math.min(1, p));
    setCapitalPctState(c);
    try { localStorage.setItem('mytbot_capital_pct', String(c)); } catch { /* ignore */ }
    try { await api.setCapitalAllocation(c); } catch { /* ignore */ }
  }, []);

  // ────── derived views ──────
  const positionChanges = useMemo(() => toPositionChanges(positionsRaw), [positionsRaw]);

  const nav = useMemo(() => {
    const v = toNumber(pnl?.today?.portfolio_value, 0);
    return Math.max(0, v);
  }, [pnl]);

  const equityValues = useMemo(() => equitySeries.map((x) => x.value).filter(Number.isFinite), [equitySeries]);
  const navPeak = useMemo(() => equityPeak(equityValues, nav), [equityValues, nav]);
  const navOpen = useMemo(() => estimateNavOpen(nav, pnl), [nav, pnl]);

  const positions = useMemo(() => mapPositions(positionsRaw, nav), [positionsRaw, nav]);
  const conviction = useMemo(() => mapConviction(snapshot, positionChanges), [snapshot, positionChanges]);
  const { approved, rejected } = useMemo(() => mapApprovedRejected(intelligence), [intelligence]);
  const strategies = useMemo(
    () => mergeStrategiesWithSignals(mapStrategies(snapshot), intelligence),
    [snapshot, intelligence],
  );
  const exposure = useMemo(() => mapExposure(snapshot), [snapshot]);
  const newsRows = useMemo(() => mapNews(news), [news]);
  const pnlRollups = useMemo(() => mapPnlRollups(pnl), [pnl]);
  const brokers = useMemo(() => mapBrokers(brokersRaw), [brokersRaw]);

  // ────── event log: merge WS + orders, newest first ──────
  const events = useMemo<LiveEvent[]>(() => {
    const wsLines = wsEvents
      .slice(-40)
      .map((e) => {
        const line = formatWsEventLine(e);
        if (!line) return null;
        const ts = eventTimestamp(e);
        const t = ts ? Date.parse(String(ts)) : Date.now();
        const kind: LiveEvent['kind'] = e.type === 'order_filled'
          ? 'fill'
          : e.type === 'signal_generated'
            ? 'signal'
            : 'tick';
        return { t, kind, text: line, ok: kind === 'fill' ? true : kind === 'signal' ? true : null } as LiveEvent;
      })
      .filter((x): x is LiveEvent => !!x);
    const merged: LiveEvent[] = [...wsLines, ...orderEvents];
    merged.sort((a, b) => (b.t ?? 0) - (a.t ?? 0));
    const seen = new Set<string>();
    const dedup: LiveEvent[] = [];
    for (const e of merged) {
      const key = `${e.kind}:${e.text}`;
      if (seen.has(key)) continue;
      seen.add(key);
      dedup.push(e);
      if (dedup.length >= 20) break;
    }
    return dedup;
  }, [wsEvents, orderEvents]);

  const eventLines = useMemo(() => events.map((e) => e.text), [events]);

  const loopIteration = snapshot?.loop_iteration ?? 0;
  const path = snapshot?.path ?? '—';

  const tradableCapital = useMemo(() => {
    const v = toNumber(pnl?.today?.tradable_capital, -1);
    return v >= 0 ? v : null;
  }, [pnl]);

  const uiState = mapSystemState(backendState, killSwitch);

  return {
    backendState,
    uiState,
    killSwitch,
    wsConnected,
    snapshotFetchFailed,
    lastStartError,

    brokers,
    activeBrokers,

    nav,
    navOpen,
    navPeak,
    pnl,
    pnlRollups,
    tradableCapital,
    capitalPct,

    exposure,
    equity: equityValues,
    equitySeries,

    snapshot,
    conviction,
    positions,
    approved,
    rejected,
    strategies,
    events,
    eventLines,
    news: newsRows,
    orders,
    intelligence,

    loopIteration,
    path,
    mode,

    start,
    stop,
    setMode,
    setCapitalPct,
    refresh: () => { void refresh(); },
  };
}
