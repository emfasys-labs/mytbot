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
}: Props) {
  const path = snapshot?.path ?? '—';
  const loopIt = snapshot?.loop_iteration;

  const d24 = discoverySummary?.last_24h;
  const navMismatch =
    bookNav != null &&
    Number.isFinite(bookNav) &&
    bookNav > 0 &&
    Number.isFinite(totalCapital) &&
    totalCapital > 0 &&
    Math.abs(bookNav - totalCapital) / totalCapital > 0.0005;

  return (
    <div className="shrink-0 border-b border-white/10 bg-black/40 px-3 py-2 md:px-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3 text-[11px] md:text-xs text-zinc-300">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-2">
          <div className="flex items-baseline gap-2">
            <span className="text-zinc-500 uppercase tracking-wider">NAV</span>
            <span className="text-lg font-light text-white tabular-nums">
              {isFlattened ? '—' : fmtDashMoneySigned(totalCapital)}
            </span>
          </div>
          {navMismatch && !isFlattened ? (
            <span className="text-[9px] leading-tight text-zinc-500 sm:max-w-[14rem]">
              Book (allocator){' '}
              <span className="font-mono text-zinc-400">{fmtDashMoneySigned(bookNav!)}</span>
              {' — '}
              can differ from broker headline
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
        <div className="h-4 w-px bg-white/10 hidden md:block" />
        <div className="flex flex-wrap gap-2 items-center">
          <span className="text-zinc-500">System</span>
          <span
            className={
              systemState === 'running'
                ? 'text-emerald-400'
                : systemState === 'error'
                  ? 'text-rose-400'
                  : 'text-zinc-400'
            }
          >
            {systemState}
          </span>
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
    </div>
  );
}
