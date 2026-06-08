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
  type ApiRealisedCurveResponse,
  type ApiPositionsResponse,
  type ConnectHubConnector,
  type DashboardSnapshot,
  type DeploymentReadiness,
  type ConnectHubResponse,
  type IntelligenceSignalsResponse,
  type IntelligenceUniverseResponse,
  type RoutingQualityResponse,
  type StrategyCandidateMixResponse,
  type SystemStatusResponse,
  type SystemState as BackendSystemState,
  type TradingMode,
} from '../lib/api';
import { eventTimestamp, formatWsEventLine, getWsUrl, type WsTickEvent, type WsTickMessage } from '../lib/ws';
import {
  estimateNavOpen,
  equityPeak,
  forwardFillNavSeries,
  mapApprovedRejected,
  mapBrokers,
  mapConviction,
  mapCoverage,
  capitalAtWork,
  mapExecutionRejections,
  mapExposure,
  mapNews,
  mapOrderEvent,
  mapPnlRollups,
  mapPositions,
  mapStrategies,
  mergeStrategiesWithSignals,
  mapSystemState,
} from './mapping';
import { defaultNewsDataProviderRows } from './data';
import type {
  Approved,
  BrokerStatus,
  Conviction,
  Coverage,
  ExecutionRejection,
  LiveEvent,
  NewsRow,
  NewsDataProviderRow,
  NewsSourceStat,
  Position,
  Rejected,
  Strategy,
} from './data';
import type { SystemState as DesignSystemState } from './tokens';

const REFRESH_INTERVAL_MS = 10_000;
const INTEL_THROTTLE_MS = 12_000;
// /intelligence/universe is heavier than the dashboard read path (it
// asks every connected broker for its full supported-symbol catalogue).
// Prefetch it on a slower cadence so the Universe tab opens instantly
// from the cached payload instead of paying a cold round-trip per visit.
const UNIVERSE_THROTTLE_MS = 60_000;

// Bundled at build time (see vite.config.ts). Surfaced in the console so a
// stale cache can be spotted at a glance; also participates in the chunk
// hash so any change forces a new bundle URL.
declare const __MYTBOT_BUILD_ID__: string;
try {
  const buildId = typeof __MYTBOT_BUILD_ID__ !== 'undefined' ? __MYTBOT_BUILD_ID__ : 'dev';
  // eslint-disable-next-line no-console
  console.info(`[mytbot] UI build id: ${buildId}`);
} catch {
  /* ignore */
}
// ~1h of intraday NAV samples at 10s cadence. Keeps the hero equity line
// responsive while the system is running without blowing up memory.
const MAX_LIVE_NAV_SAMPLES = 360;
const MAX_REALISED_TODAY_SAMPLES = 720;

export interface LiveData {
  backendState: BackendSystemState;
  uiState: DesignSystemState;
  killSwitch: boolean;
  wsConnected: boolean;
  snapshotFetchFailed: boolean;
  lastStartError: string | null;

  brokers: BrokerStatus[];
  activeBrokers: string[];
  coverage: Coverage;

  nav: number;
  navReady: boolean;
  navMissing: string[];
  navOpen: number;
  navPeak: number;
  pnl: ApiPnlResponse | null;
  pnlRollups: { d: number; w: number; m: number; y: number };
  tradableCapital: number | null;
  capitalPct: number;
  capitalAtWork: {
    deployed: number;
    pending: number;
    working: number;
    source: 'dashboard_snapshot' | 'positions_orders';
    /** ``cash_deployed / full_book_nav`` — what the trading loop uses for
     *  deployment-pressure (loop-consistent across coverage gaps). */
    cashDeployedPct: number | null;
    /** ``cash_deployed / active_nav`` — what the operator sees on the
     *  scoped dashboard. Can exceed 1.0 during partial coverage when
     *  positions held at active brokers exceed active-broker NAV. */
    activeExposurePct: number | null;
  };

  exposure: {
    gross: number;
    net: number;
    cash: number;
    navBasis: 'snapshot' | 'pnl_today_portfolio_value' | 'none';
    navDivergencePct: number | null;
  };
  equity: number[];
  equitySeries: Array<{ date: string; value: number }>;
  /** Order-derived daily realised P&L (per-UTC-day delta + running total). */
  realisedSeries: Array<{ date: string; realised: number; cumulative: number }>;
  /** Intraday samples of today's realised P&L, for the live "Today" view. */
  realisedTodaySamples: Array<{ t: number; value: number }>;

  snapshot: DashboardSnapshot | null;
  conviction: Conviction[];
  positions: Position[];
  approved: Approved[];
  rejected: Rejected[];
  executionRejections: ExecutionRejection[];
  strategies: Strategy[];
  events: LiveEvent[];
  eventLines: string[];
  news: NewsRow[];
  newsSourceStats: Record<string, NewsSourceStat>;
  /** NewsAPI / FRED / etc. from ``/system/status`` ``news_data_providers``. */
  newsDataProviders: NewsDataProviderRow[];
  connectHub: ConnectHubResponse | null;
  orders: ApiOrderRow[];
  intelligence: IntelligenceSignalsResponse | null;
  /** Prefetched /intelligence/universe payload (background polled). */
  universeIntel: IntelligenceUniverseResponse | null;
  runtimeDemand: Record<string, unknown> | null;
  runtimeMetaLabeling: Record<string, unknown> | null;
  routingQuality: RoutingQualityResponse | null;
  deployment: DeploymentReadiness | null;

  loopIteration: number;
  path: string;
  mode: TradingMode;

  start: () => Promise<void>;
  stop: () => Promise<void>;
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

function fallbackConnectHubFromStatus(sys: SystemStatusResponse | null): ConnectHubResponse | null {
  if (!sys) return null;
  const brokerRows: ConnectHubConnector[] = Object.entries(sys.brokers ?? {}).map(([id, b]) => ({
    id,
    label: id.toUpperCase(),
    category: 'brokers',
    enabled: !!b.configured,
    configured: !!b.configured,
    connected: !!b.connected,
    healthy: !!b.connected && b.balance_ready !== false,
    state: b.connected ? 'connected' : b.configured ? 'unavailable' : 'needs_credentials',
    adapter: id,
    auth_type: id === 'ibkr' ? 'gateway' : 'api_key',
    required_secrets: [],
    capabilities: {
      can_trade: true,
      can_read_balance: true,
    },
    roles: [],
    safety: {},
    docs_url: null,
    notes: b.error ?? null,
    status: { ...b },
    next_actions: b.connected
      ? []
      : [{ kind: b.configured ? 'start_system' : 'set_env', label: b.configured ? 'Reconnect/start system' : 'Add credentials in .env' }],
  }));

  const feedRows: ConnectHubConnector[] = (sys.news_data_providers ?? []).map((p) => {
    const state = String(p.state ?? 'off');
    const connected = state === 'live' || state === 'stale';
    return {
      id: p.id,
      label: p.label,
      category: 'information_feeds',
      enabled: true,
      configured: !!p.configured,
      connected,
      healthy: state === 'live',
      state,
      adapter: p.id,
      auth_type: 'api_key',
      required_secrets: [],
      capabilities: {
        can_ingest_news: p.id !== 'fred',
        can_ingest_macro: p.id === 'fred',
      },
      roles: [],
      safety: {},
      docs_url: null,
      notes: p.error ?? null,
      status: { ...p },
      next_actions: p.configured
        ? [{ kind: 'run_pipeline', label: 'Run the data pipeline to refresh this feed' }]
        : [{ kind: 'set_env', label: 'Add provider credentials in .env' }],
    };
  });

  const aiRows: ConnectHubConnector[] = [
    { id: 'rules', label: 'Rules engine', roles: ['fast_classifier'], capabilities: { advisory_only: true, local_first: true } },
    { id: 'fin_sentiment', label: 'FinBERT sentiment', roles: ['sentiment_classifier'], capabilities: { advisory_only: true, local_first: true } },
    { id: 'local_reasoning', label: 'Local LLM', roles: ['reasoning_model', 'fallback_model'], capabilities: { advisory_only: true, local_first: true } },
  ].map((row) => ({
    id: row.id,
    label: row.label,
    category: 'ai_providers',
    enabled: true,
    configured: true,
    connected: true,
    healthy: true,
    state: 'ready',
    adapter: row.id,
    auth_type: row.id === 'local_reasoning' ? 'local_endpoint' : 'none',
    required_secrets: [],
    capabilities: row.capabilities,
    roles: row.roles,
    safety: {},
    docs_url: null,
    notes: 'Fallback status derived from /system/status while backend Connect Hub endpoint is unavailable.',
    status: {},
    next_actions: [],
  }));

  const categories: ConnectHubResponse['categories'] = {
    brokers: brokerRows,
    information_feeds: feedRows,
    ai_providers: aiRows,
    treasury_accounts: [],
  };
  const summary: ConnectHubResponse['summary'] = {};
  for (const [category, rows] of Object.entries(categories)) {
    summary[category] = {
      total: rows.length,
      enabled: rows.filter((r) => r.enabled).length,
      configured: rows.filter((r) => r.configured).length,
      connected: rows.filter((r) => r.connected).length,
      healthy: rows.filter((r) => r.healthy).length,
      ids: rows.map((r) => r.id),
      connected_ids: rows.filter((r) => r.connected).map((r) => r.id),
    };
  }
  return {
    generated_at: new Date().toISOString(),
    categories,
    summary,
    capability_flags: {
      can_trade: brokerRows.some((r) => r.connected && r.capabilities.can_trade),
      has_information_feed: feedRows.some((r) => r.configured),
      has_ai_provider: aiRows.some((r) => r.configured),
      has_treasury_account: false,
      can_auto_transfer: false,
    },
  };
}

export function useLiveSystem(): LiveData {
  // ────── core state ──────
  const [backendState, setBackendState] = useState<BackendSystemState>('off');
  const [killSwitch, setKillSwitch] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const [snapshotFetchFailed, setSnapshotFetchFailed] = useState(false);
  const [lastStartError, setLastStartError] = useState<string | null>(null);
  const [activeBrokers, setActiveBrokers] = useState<string[]>([]);
  const [brokersRaw, setBrokersRaw] = useState<
    Record<string, { configured: boolean; connected: boolean; balance_ready?: boolean; error?: string | null }>
  >({});
  const [coverageRaw, setCoverageRaw] = useState<unknown>(null);
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
  const [newsSourceStats, setNewsSourceStats] = useState<Record<string, NewsSourceStat>>({});
  const [newsDataProviders, setNewsDataProviders] = useState<NewsDataProviderRow[]>(
    () => defaultNewsDataProviderRows(),
  );
  const [connectHub, setConnectHub] = useState<ConnectHubResponse | null>(null);
  const [orders, setOrders] = useState<ApiOrderRow[]>([]);
  const [intelligence, setIntelligence] = useState<IntelligenceSignalsResponse | null>(null);
  const [universeIntel, setUniverseIntel] = useState<IntelligenceUniverseResponse | null>(null);
  const [runtimeDemand, setRuntimeDemand] = useState<Record<string, unknown> | null>(null);
  const [runtimeMetaLabeling, setRuntimeMetaLabeling] = useState<Record<string, unknown> | null>(null);
  const [routingQuality, setRoutingQuality] = useState<RoutingQualityResponse | null>(null);
  const [deployment, setDeployment] = useState<DeploymentReadiness | null>(null);
  const [strategyMix, setStrategyMix] = useState<StrategyCandidateMixResponse | null>(null);
  const [loadedStrategies, setLoadedStrategies] = useState<
    Array<{ name: string; enabled: boolean; kind?: string }>
  >([]);
  const [wsEvents, setWsEvents] = useState<WsTickEvent[]>([]);
  const [orderEvents, setOrderEvents] = useState<LiveEvent[]>([]);
  // Rolling intraday NAV buffer. Pushed on every refresh while the system is
  // live so the hero equity curve moves in real time instead of showing a
  // single-point flat line from DailyPnL.
  const [liveNavSamples, setLiveNavSamples] = useState<Array<{ t: number; value: number }>>([]);
  // Order-derived daily realised P&L series (from /pnl/realised-curve).
  const [realisedSeries, setRealisedSeries] = useState<
    Array<{ date: string; realised: number; cumulative: number }>
  >([]);
  // Rolling intraday buffer of today's realised P&L so the "Today" view of
  // the graph animates as fills close, instead of being a single point.
  const [realisedTodaySamples, setRealisedTodaySamples] = useState<
    Array<{ t: number; value: number }>
  >([]);
  // The UTC day the realised-today buffer belongs to; reset the buffer at the
  // day boundary so "Today" always starts from zero at midnight UTC.
  const realisedTodayDay = useRef<string>('');

  // ────── refs ──────
  const refreshLock = useRef(false);
  const refreshPending = useRef(false);
  const lastHttpRefresh = useRef(0);
  const lastIntelRefresh = useRef(0);
  const lastUniverseRefresh = useRef(0);
  const stateRef = useRef(backendState);
  useEffect(() => { stateRef.current = backendState; }, [backendState]);
  const shutdownInFlight = useRef(false);

  const commitBackendState = useCallback((next: BackendSystemState) => {
    if (shutdownInFlight.current) {
      if (next === 'running' || next === 'starting') return;
      if (next === 'off' || next === 'error') shutdownInFlight.current = false;
    }
    setBackendState(next);
  }, []);
  // Guards for the capital-allocation slider against two distinct races
  // with the refresh loop:
  //
  //   1. `pendingCapitalWrites` — a counter bumped around the PUT. While
  //      > 0 the refresh must not adopt `sys.capital_pct`; the optimistic
  //      value is newer than anything the backend has yet observed.
  //
  //   2. `capitalWriteGen` — a generation counter bumped the instant a
  //      local write begins. Every refresh captures the generation at
  //      its start; if the generation changed by the time results are
  //      processed, its status read was initiated before the write and
  //      therefore reflects the pre-write world. Without this, a tick
  //      that fires 1–2 ms before the drag release would land with a
  //      stale `capital_pct` ~30 ms later — after pendingCapitalWrites
  //      has already dropped back to 0 if the PUT completes first —
  //      and snap the slider back for one poll cycle.
  const pendingCapitalWrites = useRef(0);
  const capitalWriteGen = useRef(0);

  // ────── clear ephemeral live data when we're off ──────
  const clearLive = useCallback(() => {
    setSnapshot(null);
    setPositionsRaw(null);
    setEquitySeries([]);
    setNews(null);
    setNewsSourceStats({});
    setNewsDataProviders(defaultNewsDataProviderRows());
    setConnectHub(null);
    setOrders([]);
    setIntelligence(null);
    setUniverseIntel(null);
    setRuntimeDemand(null);
    setRuntimeMetaLabeling(null);
    setRoutingQuality(null);
    setDeployment(null);
    setStrategyMix(null);
    setWsEvents([]);
    setOrderEvents([]);
    setPnl(null);
    setActiveBrokers([]);
    setCoverageRaw(null);
    setSnapshotFetchFailed(false);
    setKillSwitch(false);
    setLiveNavSamples([]);
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
    lastHttpRefresh.current = Date.now();
    // Snapshot the capital-write generation at the moment the refresh
    // begins. If it changes before we finish processing results, at
    // least one local capital write happened *during* this refresh and
    // the `sys.capital_pct` below must be considered stale.
    const capitalGenAtStart = capitalWriteGen.current;
    try {
      const res = await Promise.allSettled([
        api.getPnl(),
        api.getPnlHistory(90),
        api.getPositions(),
        api.getStatus(),
        api.getSystemStatus(),
        api.getNews(30, false),
        api.getDashboardSnapshot(),
        api.getOrders(50),
        api.getRoutingQuality(),
        api.getStrategyCandidateMix(24),
        api.getSystemMode(),
        api.getRealisedCurve(400),
        api.getConnectHub(),
        api.getDeploymentReadiness(),
      ]);
      const pnlRes = res[0].status === 'fulfilled' ? res[0].value : null;
      const histRes = res[1].status === 'fulfilled' ? res[1].value : null;
      const posRes = res[2].status === 'fulfilled' ? res[2].value : null;
      const statusRes = res[3].status === 'fulfilled' ? res[3].value : null;
      const sysRes = res[4].status === 'fulfilled' ? res[4].value : null;
      const newsRes = res[5].status === 'fulfilled' ? res[5].value : null;
      const snapRes = res[6].status === 'fulfilled' ? res[6].value : null;
      const ordRes = res[7].status === 'fulfilled' ? res[7].value : null;
      const routingRes = res[8].status === 'fulfilled' ? res[8].value : null;
      const mixRes = res[9].status === 'fulfilled' ? res[9].value : null;
      const modeHttpRes = res[10].status === 'fulfilled' ? res[10].value : null;
      const realisedCurveRes = res[11].status === 'fulfilled' ? res[11].value : null;
      const connectHubRes = res[12].status === 'fulfilled' ? res[12].value : null;
      const deploymentRes = res[13].status === 'fulfilled' ? res[13].value : null;
      setDeployment(deploymentRes);
      if (modeHttpRes?.mode) {
        const m = String(modeHttpRes.mode).toLowerCase();
        if (m === 'defender' || m === 'trader' || m === 'hunter') {
          setModeState(m as TradingMode);
        }
      }

      if (sysRes) {
        const newState: BackendSystemState = sysRes.state ?? 'off';
        commitBackendState(newState);
        setLastStartError(
          typeof sysRes.last_start_error === 'string' && sysRes.last_start_error.trim()
            ? sysRes.last_start_error.trim() : null,
        );
        if (Array.isArray(sysRes.active_brokers)) setActiveBrokers(sysRes.active_brokers);
        if (sysRes.brokers) setBrokersRaw(
          sysRes.brokers as Record<
            string,
            { configured: boolean; connected: boolean; balance_ready?: boolean; error?: string | null }
          >,
        );
        setCoverageRaw(sysRes.coverage ?? null);
        const ai = sysRes.trading?.ai;
        if (ai && typeof ai === 'object' && ai.news_source_stats && typeof ai.news_source_stats === 'object') {
          const stats: Record<string, NewsSourceStat> = {};
          for (const [k, raw] of Object.entries(ai.news_source_stats as Record<string, unknown>)) {
            if (!raw || typeof raw !== 'object') continue;
            const rr = raw as Record<string, unknown>;
            stats[k] = {
              fresh_rows_in_window: Number.isFinite(Number(rr.fresh_rows_in_window))
                ? Number(rr.fresh_rows_in_window)
                : 0,
              latest_age_hours: Number.isFinite(Number(rr.latest_age_hours))
                ? Number(rr.latest_age_hours)
                : null,
              stale: !!rr.stale,
              latest_published_at: typeof rr.latest_published_at === 'string' ? rr.latest_published_at : null,
              latest_fetched_at: typeof rr.latest_fetched_at === 'string' ? rr.latest_fetched_at : null,
            };
          }
          setNewsSourceStats(stats);
        } else {
          setNewsSourceStats({});
        }
        if (Array.isArray(sysRes.news_data_providers) && sysRes.news_data_providers.length > 0) {
          const valid: NewsDataProviderRow['state'][] = ['live', 'stale', 'never', 'off', 'error'];
          setNewsDataProviders(
            sysRes.news_data_providers.map((p) => {
              const st = String(p.state ?? 'off');
              return {
                id: String(p.id ?? ''),
                label: String(p.label ?? p.id ?? ''),
                configured: !!p.configured,
                state: (valid.includes(st as NewsDataProviderRow['state']) ? st : 'off') as NewsDataProviderRow['state'],
                lastIngestAt: typeof p.last_ingest_at === 'string' ? p.last_ingest_at : null,
                ageLabel: typeof p.age_label === 'string' ? p.age_label : '—',
                ok: p.ok !== false,
                error: typeof p.error === 'string' ? p.error : null,
              };
            }),
          );
        } else {
          setNewsDataProviders(defaultNewsDataProviderRows());
        }
        setConnectHub(connectHubRes ?? sysRes.connect_hub ?? fallbackConnectHubFromStatus(sysRes));
        if (Array.isArray(sysRes.loaded_strategies)) {
          setLoadedStrategies(
            sysRes.loaded_strategies
              .filter((x) => x && typeof x.name === 'string' && x.name.trim())
              .map((x) => ({ name: x.name.trim(), enabled: !!x.enabled, kind: x.kind })),
          );
        } else {
          setLoadedStrategies([]);
        }
        if (
          typeof sysRes.capital_pct === 'number'
          && Number.isFinite(sysRes.capital_pct)
          // Don't reconcile while a local write is in flight — the PUT
          // is the source of truth for the next few hundred ms and a
          // concurrent status read may still show the pre-write value.
          && pendingCapitalWrites.current === 0
          // Don't reconcile if a local write *started* during this
          // refresh: its status fetch was already in flight when the
          // write happened, so the result reflects the pre-write world.
          // The next refresh (post-write) will carry the fresh value.
          && capitalGenAtStart === capitalWriteGen.current
        ) {
          const c = Math.max(0, Math.min(1, sysRes.capital_pct));
          setCapitalPctState(c);
          try { localStorage.setItem('mytbot_capital_pct', String(c)); } catch { /* ignore */ }
        }
      } else {
        setNewsDataProviders(defaultNewsDataProviderRows());
        setConnectHub(null);
      }
      const effectiveState = shutdownInFlight.current ? 'stopping' : (sysRes?.state ?? stateRef.current);
      const feedsLive = effectiveState === 'running';

      if (statusRes) setKillSwitch(!!statusRes.kill_switch);
      if (statusRes?.runtime && typeof statusRes.runtime === 'object') {
        const rt = statusRes.runtime as Record<string, unknown>;
        const ex = (rt.extra && typeof rt.extra === 'object') ? (rt.extra as Record<string, unknown>) : {};
        setRuntimeDemand((ex.demand && typeof ex.demand === 'object') ? (ex.demand as Record<string, unknown>) : null);
        setRuntimeMetaLabeling(
          (ex.meta_labeling && typeof ex.meta_labeling === 'object')
            ? (ex.meta_labeling as Record<string, unknown>)
            : null,
        );
      } else {
        setRuntimeDemand(null);
        setRuntimeMetaLabeling(null);
      }
      if (routingRes) setRoutingQuality(routingRes);
      if (mixRes != null) {
        setStrategyMix(mixRes);
      }

      if (feedsLive) {
        if (pnlRes) {
          setPnl(pnlRes);
          // Sample live NAV into the intraday rolling buffer so the hero
          // equity curve actually moves while the system is running. Backend
          // only persists one DailyPnL row per day — without this sample
          // stream the chart would be a flat line for the first 24h.
          const liveNav = toNumber(pnlRes.today?.portfolio_value, -1);
          if (liveNav > 0) {
            const now = Date.now();
            setLiveNavSamples((prev) => {
              // Always append on a fresh refresh — even if the value didn't
              // change we still want a sample so the curve renders as a
              // genuine time-series. Only suppress rapid duplicate pushes
              // (e.g. WS + poll overlap) within ~1s.
              const last = prev[prev.length - 1];
              if (last && last.value === liveNav && now - last.t < 1_000) {
                return prev;
              }
              const next = [...prev, { t: now, value: liveNav }];
              return next.length > MAX_LIVE_NAV_SAMPLES
                ? next.slice(next.length - MAX_LIVE_NAV_SAMPLES)
                : next;
            });
          }
        }
        if (pnlRes) {
          // Sample today's REALISED P&L into a rolling intraday buffer so the
          // "Today" view of the graph animates as trades close. Reset at the
          // UTC day boundary so it always starts from zero at midnight.
          const todayRealised = toNumber(pnlRes.today?.realised, 0);
          const utcDay = new Date().toISOString().slice(0, 10);
          const now = Date.now();
          setRealisedTodaySamples((prev) => {
            const dayChanged = realisedTodayDay.current !== utcDay;
            realisedTodayDay.current = utcDay;
            const base = dayChanged ? [] : prev;
            const last = base[base.length - 1];
            if (last && last.value === todayRealised && now - last.t < 1_000) {
              return base;
            }
            const next = [...base, { t: now, value: todayRealised }];
            return next.length > MAX_REALISED_TODAY_SAMPLES
              ? next.slice(next.length - MAX_REALISED_TODAY_SAMPLES)
              : next;
          });
        }
        if (realisedCurveRes) {
          const rc = (realisedCurveRes as ApiRealisedCurveResponse).series || [];
          const rcSeries = rc
            .map((x) => ({
              date: String(x.date ?? ''),
              realised: toNumber(x.realised, 0),
              cumulative: toNumber(x.cumulative, 0),
            }))
            .filter((x) => x.date);
          setRealisedSeries(rcSeries);
        }
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

      // Throttled intelligence fetch (mode is synced every HTTP refresh above).
      if (feedsLive && Date.now() - lastIntelRefresh.current > INTEL_THROTTLE_MS) {
        lastIntelRefresh.current = Date.now();
        // Fetch the endpoint's full window (max 50) so secondary strategies
        // whose signals are older than the newest 16 (e.g. momentum_breakout
        // during a mean-reversion-dominant regime) still appear in the
        // Strategy Mix card.
        try {
          const sig = await api.getIntelligenceSignals(50);
          setIntelligence(sig);
        } catch { /* keep last known */ }
      }

      // Slow-cadence prefetch of the Universe snapshot. The endpoint is
      // gated to running/starting on the UI, and the payload changes on
      // the pipeline cadence — once per minute is plenty for the tab to
      // feel instant on switch while keeping broker catalogue calls
      // off the fast refresh path.
      if (feedsLive && Date.now() - lastUniverseRefresh.current > UNIVERSE_THROTTLE_MS) {
        lastUniverseRefresh.current = Date.now();
        try {
          const u = await api.getIntelligenceUniverse();
          setUniverseIntel(u);
        } catch { /* keep last known */ }
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
          const sys = msg.payload.system as Record<string, unknown> | undefined;
          const wsState =
            sys?.state && typeof sys.state === 'string'
              ? (sys.state as BackendSystemState)
              : stateRef.current;
          const effective = shutdownInFlight.current ? 'stopping' : wsState;
          const feedsLiveWs = effective === 'running';

          // Never paint bus/control lines into the Live feed while trading is
          // stopped — `/ws` ticks every 2s even when `off`, and applying those
          // events here fought `clearLive()` on HTTP refresh (empty ↔ lines).
          const evs = msg.payload.events;
          if (feedsLiveWs) {
            if (evs && evs.length) setWsEvents(evs.slice(-40));
          } else {
            setWsEvents((prev) => (prev.length ? [] : prev));
          }

          const wsKill = !!msg.payload.status?.kill_switch;
          setKillSwitch(wsKill);
          if (sys?.state && typeof sys.state === 'string') commitBackendState(sys.state as BackendSystemState);
          if (sys?.active_brokers && Array.isArray(sys.active_brokers)) setActiveBrokers(sys.active_brokers as string[]);
          if (sys?.brokers && typeof sys.brokers === 'object') setBrokersRaw(
            sys.brokers as Record<
              string,
              { configured: boolean; connected: boolean; balance_ready?: boolean; error?: string | null }
            >,
          );
          if (sys?.coverage !== undefined) setCoverageRaw(sys.coverage);
          if (Date.now() - lastHttpRefresh.current > REFRESH_INTERVAL_MS) {
            void refresh();
          }
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
  }, [commitBackendState, refresh]);

  // ────── Actions ──────
  const start = useCallback(async () => {
    try {
      shutdownInFlight.current = false;
      commitBackendState('starting');
      const r = await api.systemStart();
      if (r.state) commitBackendState(r.state);
      if (Array.isArray(r.active_brokers)) setActiveBrokers(r.active_brokers);
    } catch {
      shutdownInFlight.current = false;
      commitBackendState('error');
    }
  }, [commitBackendState]);

  const stop = useCallback(async () => {
    shutdownInFlight.current = true;
    commitBackendState('stopping');
    clearLive();
    try {
      const r = await api.systemStop();
      if (r.state) commitBackendState(r.state);
      else {
        shutdownInFlight.current = false;
        commitBackendState('off');
      }
      clearLive();
    } catch {
      /* ignore — status poll will eventually reflect reality */
    }
  }, [clearLive, commitBackendState]);

  // Capital-ceiling writer. Optimistically updates local state so the slider
  // stays in sync with the cursor, then reconciles with whatever the backend
  // confirms (it may clamp, or the server-side policy may reject outright).
  // On failure we revert to the previous value and rethrow so callers can
  // surface the error — `CapitalPanel` uses this to suppress its "committed"
  // banner when the write never took effect.
  //
  // While the PUT is in flight we bump `pendingCapitalWrites`, which
  // suppresses `refresh()` from overwriting our optimistic value with a
  // stale `sys.capital_pct` read (WS ticks fire refresh every 1–2 s, so
  // without this guard the thumb visibly snaps back for a cycle or two
  // before the next refresh catches up to the committed value).
  const setCapitalPct = useCallback(async (p: number) => {
    const c = Math.max(0, Math.min(1, p));
    const prev = capitalPct;
    // Bump the generation FIRST so any already-in-flight refresh knows
    // its status read is now stale. Then flip the optimistic value and
    // start the PUT. Order matters: the generation bump must happen
    // before the refresh could possibly observe the new local state.
    capitalWriteGen.current += 1;
    setCapitalPctState(c);
    try { localStorage.setItem('mytbot_capital_pct', String(c)); } catch { /* ignore */ }
    pendingCapitalWrites.current += 1;
    try {
      const r = await api.setCapitalAllocation(c);
      if (typeof r.capital_pct === 'number' && Number.isFinite(r.capital_pct)) {
        const confirmed = Math.max(0, Math.min(1, r.capital_pct));
        if (confirmed !== c) {
          setCapitalPctState(confirmed);
          try { localStorage.setItem('mytbot_capital_pct', String(confirmed)); } catch { /* ignore */ }
        }
      }
    } catch (err) {
      setCapitalPctState(prev);
      try { localStorage.setItem('mytbot_capital_pct', String(prev)); } catch { /* ignore */ }
      throw err;
    } finally {
      pendingCapitalWrites.current = Math.max(0, pendingCapitalWrites.current - 1);
    }
  }, [capitalPct]);

  // ────── derived views ──────
  const positionChanges = useMemo(() => toPositionChanges(positionsRaw), [positionsRaw]);

  // Coverage is derived BEFORE nav because the active-provider NAV branch below
  // reads ``coverage.full``. Declaring it here keeps it out of the temporal
  // dead zone — a ``const`` referenced before its declaration throws
  // "Cannot access 'coverage' before initialization" at render time.
  const coverage = useMemo<Coverage>(() => {
    const mapped = mapCoverage(coverageRaw);
    if (mapped) return mapped;
    // Old backend without coverage — infer from brokersRaw so the UI still
    // renders correctly rather than defaulting to "full" (which would hide
    // partial-NAV errors on a mixed-version deployment).
    const configured: string[] = [];
    const included: string[] = [];
    const excluded: Coverage['excluded'] = [];
    for (const [name, b] of Object.entries(brokersRaw)) {
      if (!b.configured) continue;
      configured.push(name);
      if (b.connected && b.balance_ready !== false) {
        included.push(name);
      } else {
        excluded.push({
          name,
          connected: !!b.connected,
          balance_ready: !!b.balance_ready,
          reason: (typeof b.error === 'string' && b.error.trim()) || 'not ready',
        });
      }
    }
    return {
      full: configured.length > 0 && excluded.length === 0,
      configured,
      included,
      excluded,
    };
  }, [coverageRaw, brokersRaw]);

  const nav = useMemo(() => {
    const portfolio = snapshot?.portfolio && typeof snapshot.portfolio === 'object'
      ? snapshot.portfolio as Record<string, unknown>
      : null;
    const snapshotNav = toNumber(portfolio?.nav, 0);
    const scope = String(portfolio?.scope ?? portfolio?.dashboard_scope ?? '');
    if (
      scope === 'active_providers'
      && !coverage.full
      && Number.isFinite(snapshotNav)
      && snapshotNav > 0
    ) {
      return snapshotNav;
    }
    const v = toNumber(pnl?.today?.portfolio_value, 0);
    return Math.max(0, v);
  }, [pnl, snapshot, coverage.full]);

  // Long-term daily equity history (one point per calendar day). Forward-fill
  // away zero/invalid ``portfolio_value`` rows so the sparkline does not dip to 0.
  const dailyEquityValues = useMemo(
    () => forwardFillNavSeries(
      (equitySeries ?? []).map((x) => toNumber(
        x != null && typeof x === 'object' && 'value' in x ? (x as { value: unknown }).value : undefined,
        0,
      )),
    ),
    [equitySeries],
  );
  // What the hero chart renders: blend the long-term daily history (one
  // point per day) with the intraday rolling buffer (one sample per poll).
  // As soon as a single live sample arrives the tail of the curve starts
  // moving — the backend NAV updates on every /pnl call thanks to live
  // broker prices feeding _compute_live_unrealised_mtm.
  const equityValues = useMemo(() => {
    const live = liveNavSamples.map((s) => (s && typeof s === 'object' ? s.value : undefined)).filter(
      (v): v is number => typeof v === 'number' && Number.isFinite(v),
    );
    if (live.length === 0) return dailyEquityValues;
    if (dailyEquityValues.length <= 1) return live;
    // Drop the persisted "today" row so we don't double-plot the first
    // live sample on top of it.
    const historyTrunc = dailyEquityValues.slice(0, -1);
    return [...historyTrunc, ...live];
  }, [liveNavSamples, dailyEquityValues]);
  const navPeak = useMemo(() => equityPeak(equityValues, nav), [equityValues, nav]);
  const navOpen = useMemo(
    () => estimateNavOpen(nav, pnl, equitySeries),
    [nav, pnl, equitySeries],
  );

  const positions = useMemo(() => mapPositions(positionsRaw, nav), [positionsRaw, nav]);
  const localCapitalAtWork = useMemo(
    () => capitalAtWork(positions, orders),
    [positions, orders],
  );
  const scopedCapitalAtWork = useMemo(() => {
    const portfolio = snapshot?.portfolio && typeof snapshot.portfolio === 'object'
      ? snapshot.portfolio as Record<string, unknown>
      : null;
    const deployed = toNumber(portfolio?.cash_deployed, Number.NaN);
    // Backend ([api/server.py]) now publishes both ratios on the snapshot:
    //   - ``cash_deployed_pct``    = cash_deployed / full_book_nav (loop basis)
    //   - ``active_exposure_pct``  = cash_deployed / active_nav (operator view)
    // Surface both so the capital card can show the operator view while
    // diagnostics can compare against the loop's deployment-pressure basis.
    const cashDeployedPctRaw = toNumber(portfolio?.cash_deployed_pct, Number.NaN);
    const activeExposurePctRaw = toNumber(portfolio?.active_exposure_pct, Number.NaN);
    const cashDeployedPct = Number.isFinite(cashDeployedPctRaw) ? cashDeployedPctRaw : null;
    const activeExposurePct = Number.isFinite(activeExposurePctRaw) ? activeExposurePctRaw : null;
    if (Number.isFinite(deployed) && deployed >= 0) {
      return {
        deployed,
        pending: 0,
        working: deployed,
        source: 'dashboard_snapshot' as const,
        cashDeployedPct,
        activeExposurePct,
      };
    }
    return {
      ...localCapitalAtWork,
      source: 'positions_orders' as const,
      cashDeployedPct,
      activeExposurePct,
    };
  }, [snapshot, localCapitalAtWork]);
  const conviction = useMemo(() => mapConviction(snapshot, positionChanges), [snapshot, positionChanges]);
  const { approved, rejected } = useMemo(() => mapApprovedRejected(intelligence), [intelligence]);
  const executionRejections = useMemo(() => mapExecutionRejections(orders), [orders]);
  const strategies = useMemo(
    () => mergeStrategiesWithSignals(mapStrategies(snapshot), intelligence, loadedStrategies, strategyMix),
    [snapshot, intelligence, loadedStrategies, strategyMix],
  );
  const exposure = useMemo(() => {
    const mapped = mapExposure(snapshot, pnl);
    if (nav <= 0 || positions.length === 0) return mapped;
    const { working: capitalAtWorkValue } = localCapitalAtWork;
    const grossNotional = positions.reduce((sum, p) => sum + Math.abs(p.notional || 0), 0);
    const netNotional = Math.abs(
      positions.reduce((sum, p) => {
        const sign = p.qty < 0 ? -1 : 1;
        return sum + sign * Math.abs(p.notional || 0);
      }, 0),
    );
    if (capitalAtWorkValue <= 0 && grossNotional <= 0) return mapped;
    // Exposure is raw notional / NAV. It is deliberately different from
    // Book "capital at work", which is cash/margin deployed after asset-class
    // factors. Keep both visible, but do not blend the accounting bases.
    const gross = grossNotional / nav;
    const net = netNotional / nav;
    const grossClamped = Math.max(0, gross);
    const netClamped = Math.max(0, Math.min(grossClamped, net));
    return {
      ...mapped,
      gross: grossClamped,
      net: netClamped,
      cash: Math.max(0, 1 - grossClamped),
    };
  }, [snapshot, pnl, positions, localCapitalAtWork, nav]);
  const newsRows = useMemo(() => mapNews(news), [news]);
  const pnlRollups = useMemo(
    () => mapPnlRollups(pnl, nav, equitySeries),
    [pnl, nav, equitySeries],
  );
  // (``coverage`` is declared earlier — before ``nav`` — to avoid a
  // temporal-dead-zone reference. ``brokers`` consumes it for excluded-wallet
  // tooltips, e.g. the NAV card footnote.)
  const brokers = useMemo(() => {
    const excludedSet = new Set(coverage.excluded.map((e) => e.name));
    const orchestratorIdle = backendState === 'off' || backendState === 'stopping';
    return mapBrokers(brokersRaw, excludedSet, orchestratorIdle);
  }, [brokersRaw, coverage, backendState]);

  const navStatus = pnl?.today?.nav_status;
  const navMissing = useMemo(
    () => (Array.isArray(navStatus?.missing)
      ? navStatus.missing.filter((x): x is string => typeof x === 'string' && x.trim().length > 0)
      : []),
    [navStatus],
  );
  const navReady = backendState === 'running'
    && coverage.included.length > 0
    && nav > 0
    && navStatus?.complete !== false
    && navMissing.length === 0;

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

  const baseUiState = mapSystemState(backendState, killSwitch);
  // Keep the UI in "warming up" until the orchestrator has published data the
  // user can actually see on the dashboard. Backend reports `running` as soon
  // as the loop starts, but the first iteration may still be in flight — in
  // that window NAV, conviction and the live feed are empty. We flip to
  // `running` as soon as any of the visible surfaces has real content OR the
  // first full allocator publish has landed (non-heartbeat snapshot).
  const hasVisibleData =
    conviction.length > 0
    || strategies.length > 0
    || positions.length > 0
    || events.length > 0
    || (snapshot != null && snapshot.heartbeat_only === false)
    || loopIteration > 0;
  const uiState: DesignSystemState =
    baseUiState === 'running' && !hasVisibleData ? 'starting' : baseUiState;

  return {
    backendState,
    uiState,
    killSwitch,
    wsConnected,
    snapshotFetchFailed,
    lastStartError,

    brokers,
    activeBrokers,
    coverage,

    nav: navReady ? nav : 0,
    navReady,
    navMissing,
    navOpen: navReady ? navOpen : 0,
    navPeak: navReady ? navPeak : 0,
    pnl,
    pnlRollups,
    tradableCapital: navReady ? tradableCapital : null,
    capitalPct,
    capitalAtWork: scopedCapitalAtWork,

    exposure,
    equity: equityValues,
    equitySeries,
    realisedSeries,
    realisedTodaySamples,

    snapshot,
    conviction,
    positions,
    approved,
    rejected,
    executionRejections,
    strategies,
    events,
    eventLines,
    news: newsRows,
    newsSourceStats,
    newsDataProviders,
    connectHub,
    orders,
    intelligence,
    universeIntel,
    runtimeDemand,
    runtimeMetaLabeling,
    routingQuality,
    deployment,

    loopIteration,
    path,
    mode,

    start,
    stop,
    setCapitalPct,
    refresh: () => { void refresh(); },
  };
}
