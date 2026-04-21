import { useMemo, useState } from 'react';
import { EquityLine } from '../EquityLine';
import type { ApiOrderRow, ApiPnlResponse } from '../../lib/api';
import { toNumber } from '../../lib/api';
import { buildEquityTradeMarkers } from '../../lib/dashboardFallbacks';
import { fmtDashMoneySigned } from '../../lib/dashboardFormat';

type Horizon = 'today' | 'week' | 'month' | 'all';

type Props = {
  totalCapital: number;
  dailyPnL: number;
  pnl: ApiPnlResponse | null;
  equityHistory: number[];
  /** Date-aligned series (same order as equity history from API) — used for trade markers on the curve. */
  equitySeries?: Array<{ date: string; value: number }>;
  recentOrders?: ApiOrderRow[];
  tradesToday?: number;
  lastTradeMinutes?: number;
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
  equitySeries = [],
  recentOrders = [],
  tradesToday = 0,
  lastTradeMinutes = 0,
  trendState,
  isActive,
  isFlattened,
}: Props) {
  const [horizon, setHorizon] = useState<Horizon>('all');

  const weekN = periodPnL(pnl?.week);
  const monthN = periodPnL(pnl?.month);
  const todayN = periodPnL(pnl?.today);
  const winRate = pnl?.metrics?.win_rate_days;
  const maxDd = pnl?.metrics?.max_drawdown_pct;

  const chartValues = useMemo(() => {
    const h = [...equityHistory].filter((v) => Number.isFinite(v));
    if (h.length <= 1) return h;
    if (horizon === 'all') return h.slice(-120);
    if (horizon === 'month') return h.slice(-31);
    if (horizon === 'week') return h.slice(-8);
    return h.slice(-3);
  }, [equityHistory, horizon]);

  const chartSeries = useMemo(() => {
    const s = [...equitySeries].filter((x) => Number.isFinite(x.value));
    if (s.length <= 1) return s;
    if (horizon === 'all') return s.slice(-120);
    if (horizon === 'month') return s.slice(-31);
    if (horizon === 'week') return s.slice(-8);
    return s.slice(-3);
  }, [equitySeries, horizon]);

  const tradeMarkers = useMemo(() => {
    if (chartSeries.length < 2 || !recentOrders.length) return undefined;
    const raw = buildEquityTradeMarkers(chartSeries, recentOrders);
    const n = chartSeries.length;
    const startIdx = n > 80 ? n - 80 : 0;
    const effLen = Math.min(80, n);
    return raw
      .map((m) => ({ ...m, index: m.index - startIdx }))
      .filter((m) => m.index >= 0 && m.index < effLen);
  }, [chartSeries, recentOrders]);

  const tabs: { id: Horizon; label: string }[] = [
    { id: 'today', label: 'Today' },
    { id: 'week', label: 'Week' },
    { id: 'month', label: 'Month' },
    { id: 'all', label: 'All' },
  ];

  return (
    <div className="rounded-xl border border-white/5 bg-white/[0.02] p-2.5 shrink-0">
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
          historyValues={chartValues.length >= 1 ? chartValues : undefined}
          tradeMarkers={tradeMarkers}
        />
      </div>
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-zinc-500">
        <span>
          PnL today{' '}
          <span className={todayN >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'}>
            {isFlattened ? '—' : `${todayN >= 0 ? '+' : ''}${fmtDashMoneySigned(todayN)}`}
          </span>
        </span>
        <span>
          Trades today <span className="text-zinc-300 tabular-nums">{isFlattened ? '—' : tradesToday}</span>
        </span>
        <span>
          Last trade{' '}
          <span className="text-zinc-300 tabular-nums">
            {isFlattened ? '—' : lastTradeMinutes < 1 ? '<1m' : `${lastTradeMinutes}m ago`}
          </span>
        </span>
        <span className="text-zinc-600">Dots on curve = fill days (green/red ≈ day portfolio Δ)</span>
      </div>
      {!isFlattened ? (
        <p className="mb-2 text-[9px] leading-snug text-zinc-600">
          P&amp;L uses realised + MTM unrealised. Week/month rollups now include the same live MTM for today as the
          header. &quot;Trades today&quot; is the daily ledger fill count — you can have MTM P&amp;L with zero fills.
        </p>
      ) : null}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] font-mono text-zinc-400">
        <div>
          <div className="text-[10px] text-zinc-600">Week Σ</div>
          <div className={weekN >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'}>
            {isFlattened ? '—' : `${weekN >= 0 ? '+' : ''}${fmtDashMoneySigned(weekN)}`}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-zinc-600">Month Σ</div>
          <div className={monthN >= 0 ? 'text-emerald-300/90' : 'text-rose-300/90'}>
            {isFlattened ? '—' : `${monthN >= 0 ? '+' : ''}${fmtDashMoneySigned(monthN)}`}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-zinc-600">Win days</div>
          <div className="text-zinc-300">
            {winRate == null
              ? '—'
              : `${(winRate * 100).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}%`}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-zinc-600">Max DD</div>
          <div className="text-zinc-300">
            {maxDd == null ? '—' : `${maxDd.toFixed(2)}%`}
          </div>
        </div>
      </div>
    </div>
  );
}
