import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { NewsTicker, type TickerItem } from './components/NewsTicker';
import { CapitalSlider } from './components/CapitalSlider';
import { PositionChips } from './components/PositionChips';
import { SystemHeartbeat } from './components/SystemHeartbeat';
import { HapticFeedback, useHaptic } from './components/HapticFeedback';
import { OpportunityTicker } from './components/OpportunityTicker';
import { LiveStrip } from './components/dashboard/LiveStrip';
import { AllocationCenter } from './components/dashboard/AllocationCenter';
import { RiskGate } from './components/dashboard/RiskGate';
import { PerformancePanel } from './components/dashboard/PerformancePanel';
import { DashboardAuthBanner } from './components/dashboard/DashboardAuthBanner';
import {
  api,
  setDashboardReadToken,
  toNumber,
  type ApiOrderRow,
  type ApiPnlResponse,
  type DashboardSnapshot,
  type SystemState,
  type TradingMode,
  type DiscoverySummaryResponse,
  type IntelligenceRegimeResponse,
  type IntelligenceSignalsResponse,
} from './lib/api';
import { buildWatchlistRanked } from './lib/dashboardFallbacks';
import { eventTimestamp, getWsUrl, type WsTickEvent, type WsTickMessage } from './lib/ws';

type Mode = TradingMode;
type ControlState = 'live' | 'pause' | 'flatten';
type TrendState = 'positive' | 'mixed' | 'drawdown';

interface Position {
  symbol: string;
  change: number;
}

function App() {
  const [mode, setMode] = useState<Mode>('trader');
  const [controlState, setControlState] = useState<ControlState>('flatten');
  const [systemState, setSystemState] = useState<SystemState>('off');
  const [totalCapital, setTotalCapital] = useState(0);
  const [tradableCapital, setTradableCapital] = useState<number | null>(null);
  const [capitalPct, setCapitalPct] = useState(() => {
    try {
      const v = parseFloat(localStorage.getItem('mytbot_capital_pct') ?? '');
      return Number.isFinite(v) && v >= 0 && v <= 1 ? v : 1;
    } catch { return 1; }
  });
  const [dailyPnL, setDailyPnL] = useState(0);
  const [equityHistory, setEquityHistory] = useState<number[]>([]);
  const [equityHistorySeries, setEquityHistorySeries] = useState<Array<{ date: string; value: number }>>([]);
  const [recentOrders, setRecentOrders] = useState<ApiOrderRow[]>([]);
  const [newsItems, setNewsItems] = useState<TickerItem[]>([]);
  const [tradesToday, setTradesToday] = useState(0);
  const [lastTradeMinutes, setLastTradeMinutes] = useState(0);
  const lastTradeTs = useRef<number>(0);
  const [livePositions, setLivePositions] = useState<Position[]>([]);
  const [activeBrokers, setActiveBrokers] = useState<string[]>([]);
  const [allBrokers, setAllBrokers] = useState<
    Record<string, { configured: boolean; connected: boolean; balance_ready?: boolean }>
  >({});

  // Discovery & Intelligence state
  const [discoverySummary, setDiscoverySummary] = useState<DiscoverySummaryResponse | null>(null);
  const [intelligenceRegime, setIntelligenceRegime] = useState<IntelligenceRegimeResponse | null>(null);
  const [intelligenceSignals, setIntelligenceSignals] = useState<IntelligenceSignalsResponse | null>(null);
  const lastIntelRefresh = useRef<number>(0);
  const [pnlSnapshot, setPnlSnapshot] = useState<ApiPnlResponse | null>(null);
  const [dashboardSnapshot, setDashboardSnapshot] = useState<DashboardSnapshot | null>(null);
  const [snapshotFetchFailed, setSnapshotFetchFailed] = useState(false);
  const [, setWsEvents] = useState<WsTickEvent[]>([]);
  /** Loop sleep drives snapshot cadence — stale threshold must be > ~2× this or we spuriously warn. */
  const [loopIntervalSec, setLoopIntervalSec] = useState(120);
  /** From GET /system/status trading.snapshot_published_at (same clock as dashboard.snapshot.updated_at when synced). */
  const [snapshotPublishedAt, setSnapshotPublishedAt] = useState<string | null>(null);
  const [tradingIterations, setTradingIterations] = useState(0);
  const [lastStartError, setLastStartError] = useState<string | null>(null);
  const [newsSourceStats, setNewsSourceStats] = useState<
    Record<string, { fresh_rows_in_window?: number; latest_age_hours?: number | null; stale?: boolean }>
  >({});

  const triggerHaptic = useHaptic();

  const positions: Position[] = livePositions;

  const getTrendState = (): TrendState => {
    if (dailyPnL > 50) return 'positive';
    if (dailyPnL < -50) return 'drawdown';
    return 'mixed';
  };

  const handleControlStateChange = (state: ControlState) => {
    setControlState(state);
    if (state === 'live') {
      triggerHaptic('medium');
    } else if (state === 'pause') {
      triggerHaptic('medium');
    } else if (state === 'flatten') {
      triggerHaptic('heavy');
    }
  };

  const handleSystemStart = async () => {
    triggerHaptic('heavy');
    try {
      const result = await api.systemStart();
      const newState = result.state ?? 'starting';
      setSystemState(newState);
      if (result.active_brokers) setActiveBrokers(result.active_brokers);
      if (newState === 'running' || newState === 'starting') {
        setControlState('live');
      }
    } catch {
      setSystemState('error');
    }
  };

  /** Clears anything that should not look “live” when the orchestrator is off (side panels, tickers, headline equity). */
  const clearLiveData = useCallback(() => {
    setActiveBrokers([]);
    setNewsItems([]);
    setLivePositions([]);
    setTradesToday(0);
    setLastTradeMinutes(0);
    lastTradeTs.current = 0;
    setDiscoverySummary(null);
    setIntelligenceRegime(null);
    setIntelligenceSignals(null);
    setPnlSnapshot(null);
    setDashboardSnapshot(null);
    setSnapshotFetchFailed(false);
    setWsEvents([]);
    setLoopIntervalSec(120);
    setSnapshotPublishedAt(null);
    setTradingIterations(0);
    setLastStartError(null);
    setNewsSourceStats({});
    setEquityHistory([]);
    setEquityHistorySeries([]);
    setRecentOrders([]);
    setTotalCapital(0);
    setDailyPnL(0);
    setTradableCapital(null);
  }, []);

  const handleSystemStop = async () => {
    triggerHaptic('heavy');
    try {
      const result = await api.systemStop();
      setSystemState(result.state ?? 'off');
      clearLiveData();
    } catch {
      // Keep current state
    }
  };

  const isActive = controlState === 'live' && systemState === 'running';
  /** Pause tickers / dormant chrome while shutting down or flat — avoids run/stop flicker from WS+HTTP races. */
  const isFlattened =
    controlState === 'flatten' ||
    systemState === 'off' ||
    systemState === 'stopping' ||
    systemState === 'error';

  const handlePctChange = useCallback((pct: number) => {
    const clamped = Math.max(0, Math.min(1, pct));
    setCapitalPct(clamped);
    try { localStorage.setItem('mytbot_capital_pct', String(clamped)); } catch {}
  }, []);

  useEffect(() => {
    api.setCapitalAllocation(capitalPct).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [wsConnected, setWsConnected] = useState(false);
  const wsConnectedRef = useRef(false);
  useEffect(() => {
    wsConnectedRef.current = wsConnected;
  }, [wsConnected]);

  const systemStateRef = useRef<SystemState>(systemState);
  useEffect(() => {
    systemStateRef.current = systemState;
  }, [systemState]);

  const refreshLock = useRef(false);
  const refreshPending = useRef(false);

  const refresh = useCallback(async () => {
    if (refreshLock.current) {
      refreshPending.current = true;
      return;
    }
    refreshLock.current = true;
    try {
      const results = await Promise.allSettled([
        api.getPnl(),
        api.getPnlHistory(90),
        api.getPositions(),
        api.getSignals(20),
        api.getStatus(),
        api.getSystemStatus(),
        api.getNews(30, true),
        api.getDashboardSnapshot(),
        api.getOrders(50),
      ]);

      const pnl = results[0].status === 'fulfilled' ? results[0].value : null;
      const hist = results[1].status === 'fulfilled' ? results[1].value : null;
      const pos = results[2].status === 'fulfilled' ? results[2].value : null;
      const sig = results[3].status === 'fulfilled' ? results[3].value : null;
      const status = results[4].status === 'fulfilled' ? results[4].value : null;
      const sysStatus = results[5].status === 'fulfilled' ? results[5].value : null;
      const news = results[6].status === 'fulfilled' ? results[6].value : null;

      if (sysStatus) {
        const newState = sysStatus.state ?? 'off';
        setSystemState(newState);
        const lse =
          typeof sysStatus.last_start_error === 'string' && sysStatus.last_start_error.trim()
            ? sysStatus.last_start_error.trim()
            : null;
        setLastStartError(lse);
        const tr = sysStatus.trading;
        if (tr && typeof tr === 'object') {
          const li =
            typeof tr.loop_interval_sec === 'number' && Number.isFinite(tr.loop_interval_sec) && tr.loop_interval_sec > 0
              ? tr.loop_interval_sec
              : 120;
          setLoopIntervalSec(li);
          const spa =
            typeof tr.snapshot_published_at === 'string' && tr.snapshot_published_at.trim()
              ? tr.snapshot_published_at.trim()
              : null;
          setSnapshotPublishedAt(spa);
          const it = tr.iterations;
          setTradingIterations(typeof it === 'number' && Number.isFinite(it) && it >= 0 ? Math.trunc(it) : 0);
          const ai = tr.ai;
          if (ai && typeof ai === 'object' && ai.news_source_stats && typeof ai.news_source_stats === 'object') {
            setNewsSourceStats(
              ai.news_source_stats as Record<
                string,
                { fresh_rows_in_window?: number; latest_age_hours?: number | null; stale?: boolean }
              >,
            );
          } else {
            setNewsSourceStats({});
          }
        } else {
          setTradingIterations(0);
          setSnapshotPublishedAt(null);
          setNewsSourceStats({});
        }
        if (sysStatus.active_brokers) setActiveBrokers(sysStatus.active_brokers);
        if (sysStatus.brokers)
          setAllBrokers(
            sysStatus.brokers as Record<
              string,
              { configured: boolean; connected: boolean; balance_ready?: boolean }
            >,
          );
        if (typeof sysStatus.capital_pct === 'number' && Number.isFinite(sysStatus.capital_pct)) {
          const c = Math.max(0, Math.min(1, sysStatus.capital_pct));
          setCapitalPct(c);
          try { localStorage.setItem('mytbot_capital_pct', String(c)); } catch { /* ignore */ }
        }
      }

      const sysState = (sysStatus?.state as SystemState | undefined) ?? systemStateRef.current;
      const feedsLive = sysState === 'running';

      if (!feedsLive) {
        setSnapshotFetchFailed(false);
      }

      if (results[7].status === 'fulfilled') {
        setSnapshotFetchFailed(false);
        const raw = results[7].value;
        if (feedsLive && raw != null && typeof raw === 'object') {
          setDashboardSnapshot(raw as DashboardSnapshot);
        }
      } else if (feedsLive) {
        setSnapshotFetchFailed(true);
      }

      if (results[8].status === 'fulfilled' && feedsLive) {
        const or = results[8].value as { orders?: ApiOrderRow[] };
        setRecentOrders(or.orders ?? []);
      } else if (!feedsLive) {
        setRecentOrders([]);
      }

      const killActive = !!status?.kill_switch;
      if (killActive) {
        setControlState('flatten');
      } else {
        if (sysState === 'running') {
          setControlState((prev) => (prev === 'flatten' ? 'live' : prev));
        } else if (sysState === 'off' || sysState === 'error') {
          setControlState('flatten');
          clearLiveData();
        } else if (sysState === 'stopping') {
          setControlState('flatten');
        }
      }

      if (feedsLive && pnl) {
        setPnlSnapshot(pnl);
        const portfolioValue = toNumber(pnl.today?.portfolio_value, 0);
        const realised = toNumber(pnl.today?.realised, 0);
        const unrealised = toNumber(pnl.today?.unrealised, 0);
        const todayTrades = Math.max(0, Math.trunc(toNumber(pnl.today?.trades, 0)));
        setTotalCapital(Math.max(0, portfolioValue));
        setDailyPnL(realised + unrealised);
        setTradesToday(todayTrades);
        const tc = toNumber(pnl.today?.tradable_capital, -1);
        setTradableCapital(tc >= 0 ? tc : null);
      }

      if (feedsLive && hist) {
        const series = (hist.history || [])
          .map((x) => ({ date: String(x.date ?? ''), value: toNumber(x.portfolio_value, 0) }))
          .filter((x) => x.date && Number.isFinite(x.value));
        setEquityHistory(series.length >= 1 ? series.map((x) => x.value) : []);
        setEquityHistorySeries(series.length >= 1 ? series : []);
      }

      if (feedsLive && pos) {
        const mappedPositions: Position[] = (pos.positions || []).slice(0, 8).map((p) => {
          const entry = toNumber(p.avg_entry_price, 0);
          const current = toNumber(p.current_price, 0);
          const unreal = toNumber(p.unrealised_pnl, 0);
          const pctMove = entry > 0 ? ((current - entry) / entry) * 100 : unreal >= 0 ? 0.5 : -0.5;
          return { symbol: p.symbol, change: Number.isFinite(pctMove) ? pctMove : 0 };
        });
        setLivePositions(mappedPositions);
      }

      if (feedsLive && news) {
        const headlines = news.headlines ?? [];
        const aiMap = new Map<string, { score: number; sentiment: 'positive' | 'negative' | 'neutral' }>();
        for (const ai of news.ai_scores ?? []) {
          if (ai.symbol && ai.score != null) {
            const s = parseFloat(ai.score);
            if (Number.isFinite(s)) {
              aiMap.set(ai.symbol.toUpperCase(), {
                score: s,
                sentiment: s > 0.2 ? 'positive' : s < -0.2 ? 'negative' : 'neutral',
              });
            }
          }
        }
        const tickerItems: TickerItem[] = headlines.slice(0, 20).map((h) => {
          let sentiment: TickerItem['sentiment'] = 'neutral';
          for (const [, v] of aiMap) { sentiment = v.sentiment; break; }
          return { text: h.title, source: h.source, time: h.published_at ?? undefined, sentiment };
        });
        if (tickerItems.length > 0) {
          setNewsItems(tickerItems);
        } else {
          // Fallback: if impactful-only is empty this cycle, keep ticker alive with all-news feed.
          try {
            const allNews = await api.getNews(20, false);
            const allHeadlines = allNews.headlines ?? [];
            const fallbackItems: TickerItem[] = allHeadlines.slice(0, 20).map((h) => ({
              text: h.title,
              source: h.source,
              time: h.published_at ?? undefined,
              sentiment: 'neutral',
            }));
            setNewsItems(fallbackItems);
          } catch {
            setNewsItems([]);
          }
        }
      }

      if (feedsLive && sig) {
        const signalRows = sig.signals || [];
        const lastSignalTs = signalRows.find((s) => s.timestamp)?.timestamp;
        if (lastSignalTs) {
          const tsMs = new Date(lastSignalTs).getTime();
          if (tsMs > lastTradeTs.current) {
            lastTradeTs.current = tsMs;
            setLastTradeMinutes(Math.max(0, Math.round((Date.now() - tsMs) / 60000)));
          }
        }
      }

      // Discovery + intelligence: only while RUNNING so “off” matches a quiet dashboard (no stale DB snapshots masquerading as live).
      if (feedsLive) {
        const now = Date.now();
        if (now - lastIntelRefresh.current > 12_000) {
          lastIntelRefresh.current = now;
          const [ds, ir, is_, modeRes] = await Promise.allSettled([
            api.getDiscoverySummary(),
            api.getIntelligenceRegime(),
            api.getIntelligenceSignals(12),
            api.getSystemMode(),
          ]);
          if (ds.status === 'fulfilled') setDiscoverySummary(ds.value);
          if (ir.status === 'fulfilled') setIntelligenceRegime(ir.value);
          if (is_.status === 'fulfilled') setIntelligenceSignals(is_.value);
          if (modeRes.status === 'fulfilled' && modeRes.value.mode) setMode(modeRes.value.mode);
        }
      }
    } catch {
      // Keep UI running with last known data.
    } finally {
      refreshLock.current = false;
      if (refreshPending.current) {
        refreshPending.current = false;
        void refresh();
      }
    }
  }, [clearLiveData]);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const t = params.get('dashboard_token')?.trim();
    if (!t) return;
    setDashboardReadToken(t);
    params.delete('dashboard_token');
    const q = params.toString();
    window.history.replaceState(
      {},
      '',
      `${window.location.pathname}${q ? `?${q}` : ''}${window.location.hash}`,
    );
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one-time token bootstrap from URL
  }, []);

  useEffect(() => {
    if (wsConnected) return;
    const t = window.setInterval(() => { void refresh(); }, 10000);
    return () => window.clearInterval(t);
  }, [wsConnected, refresh]);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
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
          if (evs && evs.length > 0) {
            setWsEvents(evs.slice(-40));
          }

          const sysPayload = msg.payload.system as Record<string, unknown> | undefined;
          const wsKill = !!msg.payload.status?.kill_switch;

          if (sysPayload?.state && typeof sysPayload.state === 'string') {
            setSystemState(sysPayload.state as SystemState);
          }
          if (sysPayload?.active_brokers && Array.isArray(sysPayload.active_brokers)) {
            setActiveBrokers(sysPayload.active_brokers as string[]);
          }
          if (sysPayload?.brokers && typeof sysPayload.brokers === 'object') {
            setAllBrokers(
              sysPayload.brokers as Record<
                string,
                { configured: boolean; connected: boolean; balance_ready?: boolean }
              >,
            );
          }

          if (wsKill) {
            setControlState('flatten');
          } else if (sysPayload?.state === 'running') {
            setControlState((prev) => (prev === 'flatten' ? 'live' : prev));
          } else if (sysPayload?.state === 'stopping') {
            setControlState('flatten');
          } else if (sysPayload?.state === 'off' || sysPayload?.state === 'error') {
            setControlState('flatten');
            clearLiveData();
          }

          if (sysPayload?.state === 'running') {
            const events = msg.payload.events ?? [];
            for (let i = events.length - 1; i >= 0; i -= 1) {
              const ts = eventTimestamp(events[i]);
              if (ts) {
                const tsMs = new Date(ts).getTime();
                if (tsMs > lastTradeTs.current) {
                  lastTradeTs.current = tsMs;
                  setLastTradeMinutes(Math.max(0, Math.round((Date.now() - tsMs) / 60000)));
                }
                break;
              }
            }
          }
          void refresh();
        } catch {
          // ignore malformed frames
        }
      };

      ws.onerror = () => { /* onclose will run */ };
      ws.onclose = () => {
        setWsConnected(false);
        ws = null;
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (stopped) return;
      const delay = Math.min(30000, 800 * 2 ** attempt);
      attempt += 1;
      reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, delay);
    };

    connect();
    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
      setWsConnected(false);
    };
  }, [refresh, clearLiveData]);

  const weekPnL = pnlSnapshot
    ? toNumber(pnlSnapshot.week?.realised, 0) + toNumber(pnlSnapshot.week?.unrealised, 0)
    : 0;
  const monthPnL = pnlSnapshot
    ? toNumber(pnlSnapshot.month?.realised, 0) + toNumber(pnlSnapshot.month?.unrealised, 0)
    : 0;

  const snapshotStale = useMemo(() => {
    if (systemState !== 'running') return false;
    const fromSnap = dashboardSnapshot?.updated_at ? Date.parse(dashboardSnapshot.updated_at) : NaN;
    const fromStatus = snapshotPublishedAt ? Date.parse(snapshotPublishedAt) : NaN;
    const snapTs = Math.max(
      Number.isFinite(fromSnap) ? fromSnap : 0,
      Number.isFinite(fromStatus) ? fromStatus : 0,
    );
    if (!(snapTs > 0)) return false;
    const loopMs = Math.max(10_000, loopIntervalSec * 1000);
    // Snapshots publish once per iteration; the loop then sleeps ~loop_interval_sec (default 120s).
    // A fixed 120s age threshold matched that sleep and always showed STALE between healthy ticks.
    const minAgeForStale = Math.max(180_000, 2 * loopMs + 90_000);
    return Date.now() - snapTs > minAgeForStale;
  }, [systemState, dashboardSnapshot?.updated_at, snapshotPublishedAt, loopIntervalSec]);

  const watchlistRanked = useMemo(
    () => buildWatchlistRanked(dashboardSnapshot, intelligenceSignals, positions),
    [dashboardSnapshot, intelligenceSignals, positions],
  );

  const bookNavFromSnapshot = useMemo(() => {
    const raw = dashboardSnapshot?.portfolio?.nav;
    if (raw == null || raw === '') return null;
    const n = typeof raw === 'number' ? raw : parseFloat(String(raw).replace(/,/g, ''));
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [dashboardSnapshot?.portfolio]);

  return (
    <div className="min-h-screen overflow-hidden bg-[#0a0a0a] text-white relative">
      <HapticFeedback />

      <div className="w-full h-screen flex flex-col min-h-0">
        <DashboardAuthBanner
          visible={snapshotFetchFailed && systemState === 'running'}
          onTokenSaved={() => {
            void refresh();
          }}
        />

        <LiveStrip
          totalCapital={totalCapital}
          bookNav={bookNavFromSnapshot}
          dailyPnL={dailyPnL}
          weekPnL={weekPnL}
          monthPnL={monthPnL}
          systemState={systemState}
          controlState={controlState}
          onControlStateChange={handleControlStateChange}
          onSystemStart={handleSystemStart}
          onSystemStop={handleSystemStop}
          onControlHaptic={() => triggerHaptic('light')}
          mode={mode}
          onModeChange={setMode}
          onModeHaptic={() => triggerHaptic('medium')}
          modeInactiveVisual={isFlattened}
          modeDisabled={systemState === 'off'}
          allBrokers={allBrokers}
          snapshot={dashboardSnapshot}
          discoverySummary={discoverySummary}
          snapshotStale={snapshotStale}
          isFlattened={isFlattened}
          tradingIterations={tradingIterations}
          loopIntervalSec={loopIntervalSec}
          lastStartError={lastStartError}
        />

        <div className="flex flex-1 min-h-0 flex-col">
          <div className="flex flex-1 min-h-0 flex-col gap-2 overflow-hidden p-2 lg:flex-row lg:items-stretch">
            <main className="relative z-0 isolate flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-y-auto xl:pr-14">
              <div className="pointer-events-none hidden xl:block absolute right-0 top-0 z-10 h-96">
                <div className="pointer-events-auto">
                  <CapitalSlider
                    totalCapital={totalCapital}
                    pct={capitalPct}
                    onPctChange={handlePctChange}
                    onHaptic={() => triggerHaptic('light')}
                    dormant={isFlattened}
                  />
                </div>
              </div>
              <AllocationCenter
                snapshot={dashboardSnapshot}
                dormant={systemState !== 'running'}
                snapshotFetchFailed={snapshotFetchFailed}
                positions={positions}
              />
              <PerformancePanel
                totalCapital={totalCapital}
                dailyPnL={dailyPnL}
                pnl={pnlSnapshot}
                equityHistory={equityHistory}
                equitySeries={equityHistorySeries}
                recentOrders={recentOrders}
                tradesToday={tradesToday}
                lastTradeMinutes={lastTradeMinutes}
                trendState={getTrendState()}
                isActive={isActive}
                isFlattened={isFlattened}
              />
              <div className="flex w-full shrink-0 flex-wrap items-start gap-3 border-t border-white/5 pt-3 mt-1">
                <PositionChips
                  positions={positions}
                  isFlattened={isFlattened}
                  onHaptic={() => triggerHaptic('light')}
                />
                <SystemHeartbeat
                  isActive={isActive}
                  controlState={controlState}
                  tradesCount={tradesToday}
                  lastTradeMinutes={lastTradeMinutes}
                />
                {tradableCapital != null && totalCapital > 0 && capitalPct < 0.999 ? (
                  <span className="text-[11px] text-zinc-500">
                    Tradable £{Math.round(tradableCapital).toLocaleString()} · {Math.round(capitalPct * 100)}%
                  </span>
                ) : null}
              </div>
            </main>

            <aside className="z-0 flex max-h-[42vh] min-h-0 shrink-0 flex-col lg:max-h-none lg:w-80">
              <RiskGate signals={intelligenceSignals} dormant={systemState !== 'running'} />
            </aside>
          </div>
        </div>

        <OpportunityTicker
          signals={intelligenceSignals}
          regime={intelligenceRegime}
          watchlist={watchlistRanked}
        />
        <div className="border-t border-white/5 bg-black/30 px-3 py-1.5 text-[10px] text-zinc-400">
          {Object.keys(newsSourceStats).length > 0 ? (
            Object.entries(newsSourceStats)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([src, st]) => {
                const fresh = typeof st.fresh_rows_in_window === 'number' ? st.fresh_rows_in_window : 0;
                const age = st.latest_age_hours;
                const stale = !!st.stale;
                return (
                  <span key={src} className="mr-3 inline-flex items-center gap-1.5">
                    <span className={`h-1.5 w-1.5 rounded-full ${stale ? 'bg-amber-400' : 'bg-emerald-400'}`} />
                    <span className="uppercase">{src}</span>
                    <span className="text-zinc-500">{fresh} fresh</span>
                    <span className="text-zinc-500">{age != null ? `${age.toFixed(1)}h` : 'n/a'}</span>
                  </span>
                );
              })
          ) : (
            <span className="text-zinc-500">news source health pending...</span>
          )}
        </div>
        <NewsTicker items={newsItems} paused={isFlattened} />
      </div>
    </div>
  );
}

export default App;
