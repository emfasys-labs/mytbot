import type {
  DashboardSnapshot,
  DiscoverySummaryResponse,
  IntelligenceRegimeResponse,
  SystemState,
} from '../../lib/api';

type BrokerRow = { configured: boolean; connected: boolean; balance_ready?: boolean };

function fmtMoney(n: number, currency = '£'): string {
  const sign = n < 0 ? '−' : '';
  return `${sign}${currency}${Math.abs(Math.round(n)).toLocaleString()}`;
}

type Props = {
  totalCapital: number;
  dailyPnL: number;
  weekPnL: number;
  monthPnL: number;
  systemState: SystemState;
  modeLabel: string;
  allBrokers: Record<string, BrokerRow>;
  intelligenceRegime: IntelligenceRegimeResponse | null;
  snapshot: DashboardSnapshot | null;
  discoverySummary: DiscoverySummaryResponse | null;
  snapshotStale: boolean;
  isFlattened: boolean;
};

export function LiveStrip({
  totalCapital,
  dailyPnL,
  weekPnL,
  monthPnL,
  systemState,
  modeLabel,
  allBrokers,
  intelligenceRegime,
  snapshot,
  discoverySummary,
  snapshotStale,
  isFlattened,
}: Props) {
  const snapRegime = snapshot?.regime?.regime_label;
  const macroLabel = intelligenceRegime?.regime?.label ?? '—';
  const regimeLabel = snapRegime ?? macroLabel;
  const comps = snapshot?.regime?.components ?? {};
  const compEntries = Object.entries(comps).slice(0, 6);
  const path = snapshot?.path ?? '—';
  const loopIt = snapshot?.loop_iteration;

  const d24 = discoverySummary?.last_24h;

  return (
    <div className="shrink-0 border-b border-white/10 bg-black/40 px-3 py-2 md:px-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-[11px] md:text-xs text-zinc-300">
        <div className="flex items-baseline gap-2">
          <span className="text-zinc-500 uppercase tracking-wider">NAV</span>
          <span className="text-lg font-light text-white tabular-nums">
            {isFlattened ? '—' : fmtMoney(totalCapital)}
          </span>
        </div>
        <div className="h-4 w-px bg-white/10 hidden sm:block" />
        <div className="flex flex-wrap gap-3">
          <span>
            <span className="text-zinc-500">Today </span>
            <span className={dailyPnL >= 0 ? 'text-emerald-300' : 'text-rose-300'} tabular-nums>
              {isFlattened ? '—' : `${dailyPnL >= 0 ? '+' : ''}${fmtMoney(dailyPnL)}`}
            </span>
          </span>
          <span>
            <span className="text-zinc-500">Week </span>
            <span className={weekPnL >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'} tabular-nums>
              {isFlattened ? '—' : `${weekPnL >= 0 ? '+' : ''}${fmtMoney(weekPnL)}`}
            </span>
          </span>
          <span>
            <span className="text-zinc-500">Month </span>
            <span className={monthPnL >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'} tabular-nums>
              {isFlattened ? '—' : `${monthPnL >= 0 ? '+' : ''}${fmtMoney(monthPnL)}`}
            </span>
          </span>
        </div>
        <div className="h-4 w-px bg-white/10 hidden md:block" />
        <div className="flex flex-wrap gap-2 items-center">
          <span className="rounded border border-white/10 px-1.5 py-0.5 text-[10px] uppercase text-zinc-400">
            {modeLabel}
          </span>
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
        <div className="hidden lg:flex flex-wrap gap-2 items-center ml-auto">
          <span className="text-zinc-500">Regime</span>
          <span className="text-white/90">{regimeLabel}</span>
          {compEntries.map(([k, v]) => (
            <span key={k} className="text-zinc-500">
              {k.replace(/_/g, ' ')} <span className="text-zinc-300 tabular-nums">{v}</span>
            </span>
          ))}
        </div>
      </div>
      {d24 && !isFlattened ? (
        <div className="mt-1 text-[10px] text-zinc-600">
          24h discovery · signals {d24.signals_produced ?? '—'} · anomalies {d24.anomalies_detected ?? '—'}
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
