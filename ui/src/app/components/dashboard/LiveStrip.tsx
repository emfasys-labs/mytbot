import type { DashboardSnapshot, DiscoverySummaryResponse, SystemState, TradingMode } from '../../lib/api';
import { fmtDashMoneySigned } from '../../lib/dashboardFormat';
import { MasterControl } from '../MasterControl';
import { ModeSelector } from '../ModeSelector';

type BrokerRow = { configured: boolean; connected: boolean; balance_ready?: boolean };
type ControlState = 'live' | 'pause' | 'flatten';

type Props = {
  /** Headline NAV from GET /pnl (broker / configured / DB max). */
  totalCapital: number;
  /** Optional allocator book NAV from dashboard snapshot — shown when it differs from headline. */
  bookNav?: number | null;
  dailyPnL: number;
  weekPnL: number;
  monthPnL: number;
  systemState: SystemState;
  controlState: ControlState;
  onControlStateChange: (s: ControlState) => void;
  onSystemStart: () => Promise<void>;
  onSystemStop: () => Promise<void>;
  onControlHaptic?: () => void;
  mode: TradingMode;
  onModeChange: (m: TradingMode) => void;
  onModeHaptic?: () => void;
  modeInactiveVisual: boolean;
  modeDisabled: boolean;
  allBrokers: Record<string, BrokerRow>;
  snapshot: DashboardSnapshot | null;
  discoverySummary: DiscoverySummaryResponse | null;
  snapshotStale: boolean;
  isFlattened: boolean;
  /** Completed loop iterations from /system/status trading.iterations */
  tradingIterations?: number;
  loopIntervalSec?: number;
  lastStartError?: string | null;
};

export function LiveStrip({
  totalCapital,
  bookNav = null,
  dailyPnL,
  weekPnL,
  monthPnL,
  systemState,
  controlState,
  onControlStateChange,
  onSystemStart,
  onSystemStop,
  onControlHaptic,
  mode,
  onModeChange,
  onModeHaptic,
  modeInactiveVisual,
  modeDisabled,
  allBrokers,
  snapshot,
  discoverySummary,
  snapshotStale,
  isFlattened,
  tradingIterations = 0,
  loopIntervalSec = 120,
  lastStartError = null,
}: Props) {
  const pctFromUnknown = (raw: unknown): number | null => {
    if (raw == null || raw === '') return null;
    const n = typeof raw === 'number' ? raw : Number.parseFloat(String(raw));
    if (!Number.isFinite(n) || n < 0) return null;
    const pct = n > 1 ? n : n * 100;
    return Math.max(0, Math.min(100, pct));
  };

  const path = snapshot?.path ?? '—';
  const loopIt = snapshot?.loop_iteration;
  const instructions = (snapshot?.execution_plan?.instructions ?? []) as Array<Record<string, unknown>>;
  const next = instructions[0] ?? null;
  const deployedPct = pctFromUnknown(snapshot?.portfolio?.gross_exposure);
  const freePct = deployedPct == null ? null : Math.max(0, 100 - deployedPct);

  const d24 = discoverySummary?.last_24h;
  const showFirstCycleWait =
    systemState === 'running' &&
    !isFlattened &&
    typeof tradingIterations === 'number' &&
    tradingIterations === 0;

  const navMismatch =
    bookNav != null &&
    Number.isFinite(bookNav) &&
    bookNav > 0 &&
    Number.isFinite(totalCapital) &&
    totalCapital > 0 &&
    Math.abs(bookNav - totalCapital) / totalCapital > 0.0005;

  const liveNow = (() => {
    if (systemState === 'error') return 'ERROR';
    if (systemState === 'starting') return 'BOOTING';
    if (systemState === 'stopping') return 'STOPPING';
    if (systemState !== 'running') return 'IDLE';
    if (instructions.length > 0) return 'EXECUTION PREP';
    return 'SCANNING';
  })();

  const nextActionLine = (() => {
    if (systemState !== 'running') return 'none';
    if (!next) return 'waiting for high-conviction setup';
    const action = String(next.action ?? next.kind ?? 'act').toUpperCase();
    const side = String(next.side ?? '').toUpperCase();
    const sym = String(next.symbol ?? '').toUpperCase();
    const cap = next.capital ?? next.target_notional;
    const capTxt = cap != null && String(cap) !== '' ? ` · ${fmtDashMoneySigned(cap)}` : '';
    return `${action}${side ? ` ${side}` : ''}${sym ? ` ${sym}` : ''}${capTxt}`;
  })();

  return (
    <div className="shrink-0 border-b border-white/10 bg-gradient-to-b from-zinc-950 to-black px-3 py-2.5 md:px-4">
      <div className="mb-2 rounded-xl border border-sky-500/25 bg-sky-950/20 px-3 py-2">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <div className="flex items-center gap-2">
            <span className={`h-2.5 w-2.5 rounded-full ${systemState === 'running' ? 'animate-pulse bg-emerald-400 shadow-[0_0_12px_rgba(52,211,153,0.8)]' : systemState === 'error' ? 'bg-rose-400' : 'bg-zinc-500'}`} />
            <span className="text-[10px] uppercase tracking-[0.2em] text-zinc-400">System</span>
            <span className="text-sm font-semibold tracking-wide text-white">{liveNow}</span>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px] font-mono">
            <span className="text-zinc-400">MODE <span className="text-zinc-100">{String(mode).toUpperCase()}</span></span>
            <span className="text-zinc-600">|</span>
            <span className="text-zinc-400">DEPLOYED <span className="text-emerald-300">{isFlattened || deployedPct == null ? '—' : `${deployedPct.toFixed(0)}%`}</span></span>
            <span className="text-zinc-600">|</span>
            <span className="text-zinc-400">NEXT <span className="text-zinc-100">{isFlattened ? 'off' : nextActionLine}</span></span>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-3 text-[11px] md:text-xs text-zinc-300">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-5 gap-y-2">
          <div className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-2">
            <div className="flex items-baseline gap-2">
              <span className="text-zinc-500 uppercase tracking-wider">NAV</span>
              <span className="text-2xl font-semibold text-white tabular-nums">
                {isFlattened ? '—' : fmtDashMoneySigned(totalCapital)}
              </span>
            </div>
            {navMismatch && !isFlattened ? (
              <span className="text-[9px] leading-tight text-zinc-500 sm:max-w-[14rem]">
                Book <span className="font-mono text-zinc-400">{fmtDashMoneySigned(bookNav!)}</span>
              </span>
            ) : null}
          </div>

          <div className="h-4 w-px bg-white/10 hidden sm:block" />
          <div className="flex flex-wrap gap-3">
            <span>
              <span className="text-zinc-500">Today </span>
              <span className={dailyPnL >= 0 ? 'text-emerald-300' : 'text-rose-300'} tabular-nums>
                {isFlattened ? '—' : `${dailyPnL >= 0 ? '+' : ''}${fmtDashMoneySigned(dailyPnL)}`}
              </span>
            </span>
            <span>
              <span className="text-zinc-500">Week </span>
              <span className={weekPnL >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'} tabular-nums>
                {isFlattened ? '—' : `${weekPnL >= 0 ? '+' : ''}${fmtDashMoneySigned(weekPnL)}`}
              </span>
            </span>
            <span>
              <span className="text-zinc-500">Month </span>
              <span className={monthPnL >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'} tabular-nums>
                {isFlattened ? '—' : `${monthPnL >= 0 ? '+' : ''}${fmtDashMoneySigned(monthPnL)}`}
              </span>
            </span>
          </div>
          <div className="h-4 w-px bg-white/10 hidden sm:block" />
          <div className="flex flex-wrap gap-2 items-center font-mono text-[10px]">
            <span className="text-zinc-500">CAPITAL</span>
            <span className="text-emerald-300">{isFlattened || deployedPct == null ? '—' : `${deployedPct.toFixed(0)}% deployed`}</span>
            <span className="text-zinc-500">{isFlattened || freePct == null ? '' : `| ${freePct.toFixed(0)}% free`}</span>
            <span className="text-zinc-600">·</span>
            <span className="text-zinc-500">path {path}</span>
            {loopIt != null ? <span className="text-zinc-600">#{loopIt}</span> : null}
            {snapshotStale ? (
              <span className="text-amber-400/90 text-[10px] uppercase">snapshot stale</span>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-3 md:ml-auto">
          <MasterControl
            currentState={controlState}
            systemState={systemState}
            onStateChange={onControlStateChange}
            onSystemStart={onSystemStart}
            onSystemStop={onSystemStop}
            onHaptic={onControlHaptic}
          />
          <span className="hidden sm:inline text-[10px] uppercase tracking-wider text-zinc-600 pr-1">
            Risk mode
          </span>
          <ModeSelector
            variant="horizontal"
            selectedMode={mode}
            onModeChange={onModeChange}
            onHaptic={onModeHaptic}
            inactiveVisual={modeInactiveVisual}
            disabled={modeDisabled}
          />
        </div>
      </div>
      {d24 && !isFlattened ? (
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-zinc-600">
          <span>
            24h discovery · signals {d24.signals_produced ?? '—'} · anomalies {d24.anomalies_detected ?? '—'}
          </span>
        </div>
      ) : null}
      {Object.keys(allBrokers).length > 0 ? (
        <div className="mt-2 flex flex-wrap gap-1">
          {Object.entries(allBrokers)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([name, v]) => {
              const cls = !v.configured
                ? 'bg-zinc-800/40 text-zinc-600'
                : !v.connected
                  ? 'bg-amber-400/10 text-amber-200/60'
                  : v.balance_ready === false
                    ? 'bg-amber-400/10 text-amber-200/60'
                    : 'bg-emerald-400/10 text-emerald-300/70';
              return (
                <span
                  key={name}
                  className={`rounded-full px-2 py-0.5 text-[9px] font-medium uppercase tracking-wider ${cls}`}
                >
                  {name}
                </span>
              );
            })}
        </div>
      ) : null}
      {systemState === 'error' && lastStartError ? (
        <div className="mt-1.5 text-[10px] text-rose-300/90 max-w-2xl leading-snug font-mono">
          Last start: {lastStartError}
        </div>
      ) : null}
      {showFirstCycleWait ? (
        <div className="mt-1.5 text-[10px] text-amber-200/85 max-w-2xl leading-snug">
          First loop cycle in progress (interval ≈ {loopIntervalSec}s). Allocator strip fills after iteration 1; heartbeat
          may show before full D015/global-edge publish.
        </div>
      ) : null}
    </div>
  );
}
