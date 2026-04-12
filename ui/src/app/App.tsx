import { useCallback, useEffect, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { NewsTicker, type TickerItem } from './components/NewsTicker';
import { EquityLine } from './components/EquityLine';
import { ModeSelector } from './components/ModeSelector';
import { CapitalSlider } from './components/CapitalSlider';
import { MasterControl } from './components/MasterControl';
import { PositionChips } from './components/PositionChips';
import { SystemHeartbeat } from './components/SystemHeartbeat';
import { HapticFeedback, useHaptic } from './components/HapticFeedback';
import { DiscoveryPanel } from './components/DiscoveryPanel';
import { IntelligencePanel } from './components/IntelligencePanel';
import { OpportunityTicker } from './components/OpportunityTicker';
import {
  api,
  toNumber,
  type SystemState,
  type TradingMode,
  type DiscoverySummaryResponse,
  type DiscoveryAnomaliesResponse,
  type IntelligenceRegimeResponse,
  type IntelligenceSignalsResponse,
} from './lib/api';
import { eventTimestamp, getWsUrl, type WsTickMessage } from './lib/ws';

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
  const [discoveryAnomalies, setDiscoveryAnomalies] = useState<DiscoveryAnomaliesResponse | null>(null);
  const [intelligenceRegime, setIntelligenceRegime] = useState<IntelligenceRegimeResponse | null>(null);
  const [intelligenceSignals, setIntelligenceSignals] = useState<IntelligenceSignalsResponse | null>(null);
  const lastIntelRefresh = useRef<number>(0);

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
    setDiscoveryAnomalies(null);
    setIntelligenceRegime(null);
    setIntelligenceSignals(null);
    setEquityHistory([]);
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
        api.getPositions(16),
        api.getSignals(20),
        api.getStatus(),
        api.getSystemStatus(),
        api.getNews(30),
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
        const historyValues = (hist.history || [])
          .map((x) => toNumber(x.portfolio_value, 0))
          .filter((v) => v > 0);
        setEquityHistory(historyValues.length > 1 ? historyValues : []);
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
        if (tickerItems.length > 0) setNewsItems(tickerItems);
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
          const [ds, da, ir, is_, modeRes] = await Promise.allSettled([
            api.getDiscoverySummary(),
            api.getDiscoveryAnomalies(8),
            api.getIntelligenceRegime(),
            api.getIntelligenceSignals(8),
            api.getSystemMode(),
          ]);
          if (ds.status === 'fulfilled') setDiscoverySummary(ds.value);
          if (da.status === 'fulfilled') setDiscoveryAnomalies(da.value);
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

  return (
    <div className="min-h-screen overflow-hidden bg-[#0a0a0a] text-white relative">
      <HapticFeedback />

      <div className="w-full h-screen flex flex-col">
        {/* Top news ticker */}
        <NewsTicker items={newsItems} paused={isFlattened} />

        {/* Three-tier main area */}
        <div className="flex flex-1 overflow-hidden">

          {/* ── DISCOVERY (left) ─────────────────────────────────── */}
          <aside className="hidden xl:flex flex-col w-72 shrink-0 border-r border-white/5 overflow-y-auto px-5 py-6">
            <DiscoveryPanel
              summary={discoverySummary}
              anomalies={discoveryAnomalies}
              dormant={systemState !== 'running'}
            />
          </aside>

          {/* ── CENTER (equity + controls) ───────────────────────── */}
          <div className="flex flex-1 flex-col justify-between px-6 pb-3 pt-4 md:px-8 md:pb-5 md:pt-5 overflow-y-auto">
            {/* Top bar: mode + brokers + master control */}
            <div className="flex items-start justify-between">
              <div className="pt-6">
                <ModeSelector
                  selectedMode={mode}
                  onModeChange={setMode}
                  onHaptic={() => triggerHaptic('medium')}
                  inactiveVisual={isFlattened}
                />
              </div>

              <div className="pt-2 flex items-start gap-2">
                {systemState !== 'off' && Object.keys(allBrokers).length > 0 && (
                  <div className="flex gap-1 flex-wrap justify-end pt-2.5">
                    {Object.entries(allBrokers)
                      .sort(([a], [b]) => a.localeCompare(b))
                      .map(([name, v]) => {
                        const err = 'error' in v && v.error ? String(v.error) : '';
                        const warmingUp = v.configured && v.connected && v.balance_ready === false;
                        const cls = !v.configured
                          ? 'bg-zinc-800/40 text-zinc-600'
                          : !v.connected
                            ? 'bg-amber-400/10 text-amber-200/60'
                            : v.balance_ready === false
                              ? 'bg-amber-400/10 text-amber-200/60'
                              : 'bg-emerald-400/10 text-emerald-300/70';
                        const defaultTitle = v.configured
                          ? v.connected
                            ? warmingUp
                              ? 'Connected — loading account balance'
                              : 'Connected'
                            : 'Configured, not connected'
                          : 'Not configured';
                        return (
                          <span
                            key={name}
                            title={err || defaultTitle}
                            className={`rounded-full px-2 py-0.5 text-[9px] font-medium uppercase tracking-wider transition-colors duration-500 ${cls}`}
                          >
                            {name}
                          </span>
                        );
                      })}
                  </div>
                )}
                <MasterControl
                  currentState={controlState}
                  systemState={systemState}
                  onStateChange={handleControlStateChange}
                  onSystemStart={handleSystemStart}
                  onSystemStop={handleSystemStop}
                  onHaptic={() => triggerHaptic('light')}
                />
              </div>
            </div>

            {/* Equity + balance */}
            <div className="relative flex flex-1 items-center justify-center">
              <div className="absolute right-0 top-1/2 -translate-y-1/2">
                <CapitalSlider
                  totalCapital={totalCapital}
                  pct={capitalPct}
                  onPctChange={handlePctChange}
                  onHaptic={() => triggerHaptic('light')}
                  dormant={isFlattened}
                />
              </div>

              <div className="w-full max-w-4xl">
                <EquityLine
                  balance={totalCapital}
                  dailyPnL={dailyPnL}
                  state={getTrendState()}
                  isActive={isActive}
                  isFlattened={isFlattened}
                  historyValues={equityHistory}
                />

                <div className="mt-8 text-center">
                  {systemState === 'running' ? (
                    <>
                      <motion.div
                        className="text-6xl font-light tracking-tight"
                        animate={{ scale: [1.02, 1] }}
                        transition={{ duration: 0.3 }}
                      >
                        £{Math.round(totalCapital).toLocaleString()}
                      </motion.div>
                      {tradableCapital != null && totalCapital > 0 && capitalPct < 0.999 ? (
                        <div className="mt-2 text-sm font-light text-gray-400">
                          £{Math.round(tradableCapital).toLocaleString()} available for trading ·{' '}
                          {Math.round(capitalPct * 100)}%
                        </div>
                      ) : null}
                      <motion.div
                        className={`mt-3 text-xl font-light ${dailyPnL >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}
                        animate={{ opacity: 1, y: 0 }}
                        transition={{ duration: 0.3 }}
                      >
                        {dailyPnL >= 0 ? '+' : ''}£{Math.round(dailyPnL).toLocaleString()} today
                      </motion.div>
                    </>
                  ) : (
                    <div className="text-gray-600 text-sm font-light">
                      Start the system to see live balance
                    </div>
                  )}

                  <div className="mt-3 flex justify-center">
                    <SystemHeartbeat
                      isActive={isActive}
                      controlState={controlState}
                      tradesCount={tradesToday}
                      lastTradeMinutes={lastTradeMinutes}
                    />
                  </div>
                </div>
              </div>
            </div>

            {/* Positions + mobile Discovery/Intelligence accordion */}
            <div className="pb-2 space-y-4">
              <PositionChips
                positions={positions}
                isFlattened={isFlattened}
                onHaptic={() => triggerHaptic('light')}
              />

              {/* On screens < xl, show compact Discovery + Intelligence inline */}
              <div className="xl:hidden grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div className="rounded-xl border border-white/5 bg-white/2 px-4 py-4">
                  <DiscoveryPanel
                    summary={discoverySummary}
                    anomalies={discoveryAnomalies}
                    dormant={systemState !== 'running'}
                  />
                </div>
                <div className="rounded-xl border border-white/5 bg-white/2 px-4 py-4">
                  <IntelligencePanel
                    regime={intelligenceRegime}
                    signals={intelligenceSignals}
                    dormant={systemState !== 'running'}
                  />
                </div>
              </div>
            </div>
          </div>

          {/* ── INTELLIGENCE (right) ─────────────────────────────── */}
          <aside className="hidden xl:flex flex-col w-64 shrink-0 border-l border-white/5 overflow-y-auto px-5 py-6">
            <IntelligencePanel
              regime={intelligenceRegime}
              signals={intelligenceSignals}
              dormant={systemState !== 'running'}
            />
          </aside>
        </div>

        {/* Opportunity ticker at bottom */}
        <OpportunityTicker signals={intelligenceSignals} regime={intelligenceRegime} />
      </div>
    </div>
  );
}

export default App;
