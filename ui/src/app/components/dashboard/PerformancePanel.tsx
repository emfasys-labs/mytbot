import { useMemo, useState } from 'react';
import { EquityLine } from '../EquityLine';
import type { ApiPnlResponse } from '../../lib/api';
import { toNumber } from '../../lib/api';

type Horizon = 'today' | 'week' | 'month' | 'all';

type Props = {
  totalCapital: number;
  dailyPnL: number;
  pnl: ApiPnlResponse | null;
  equityHistory: number[];
  trendState: 'positive' | 'mixed' | 'drawdown';
  isActive: boolean;
  isFlattened: boolean;
};

function periodPnL(period: { realised?: string; unrealised?: string } | undefined): number {
  if (!period) return 0;
  return toNumber(period.realised, 0) + toNumber(period.unrealised, 0);
}

export function PerformancePanel({
  totalCapital,
  dailyPnL,
  pnl,
  equityHistory,
  trendState,
  isActive,
  isFlattened,
}: Props) {
  const [horizon, setHorizon] = useState<Horizon>('all');

  const weekN = periodPnL(pnl?.week);
  const monthN = periodPnL(pnl?.month);
  const winRate = pnl?.metrics?.win_rate_days;
  const maxDd = pnl?.metrics?.max_drawdown_pct;

  const chartValues = useMemo(() => {
    const h = [...equityHistory].filter((v) => v > 0);
    if (h.length <= 1) return h;
    if (horizon === 'all') return h.slice(-120);
    if (horizon === 'month') return h.slice(-31);
    if (horizon === 'week') return h.slice(-8);
    return h.slice(-3);
  }, [equityHistory, horizon]);

  const tabs: { id: Horizon; label: string }[] = [
    { id: 'today', label: 'Today' },
    { id: 'week', label: 'Week' },
    { id: 'month', label: 'Month' },
    { id: 'all', label: 'All' },
  ];

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-3 shrink-0">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
        <div className="text-[10px] uppercase tracking-widest text-zinc-500">Performance</div>
        <div className="flex gap-1">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setHorizon(t.id)}
              className={`rounded px-2 py-0.5 text-[10px] uppercase ${
                horizon === t.id ? 'bg-white/10 text-white' : 'text-zinc-500 hover:text-zinc-300'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div className="h-[148px] w-full mb-2 overflow-hidden rounded-md bg-black/30 isolate">
        <EquityLine
          compact
          balance={totalCapital}
          dailyPnL={dailyPnL}
          state={trendState}
          isActive={isActive}
          isFlattened={isFlattened}
          historyValues={chartValues.length > 1 ? chartValues : undefined}
        />
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] font-mono text-zinc-400">
        <div>
          <div className="text-[10px] text-zinc-600">Week Σ</div>
          <div className={weekN >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'}>
            {isFlattened ? '—' : `${weekN >= 0 ? '+' : ''}£${Math.round(weekN).toLocaleString()}`}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-zinc-600">Month Σ</div>
          <div className={monthN >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'}>
            {isFlattened ? '—' : `${monthN >= 0 ? '+' : ''}£${Math.round(monthN).toLocaleString()}`}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-zinc-600">Win days</div>
          <div className="text-zinc-300">
            {winRate == null ? '—' : `${Math.round(winRate * 100)}%`}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-zinc-600">Max DD</div>
          <div className="text-zinc-300">
            {maxDd == null ? '—' : `${maxDd.toFixed(1)}%`}
          </div>
        </div>
      </div>
    </div>
  );
}
