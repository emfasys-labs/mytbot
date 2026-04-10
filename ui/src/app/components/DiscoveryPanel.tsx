import { motion } from 'motion/react';
import type { DiscoverySummaryResponse, DiscoveryAnomaliesResponse } from '../lib/api';

interface DiscoveryPanelProps {
  summary: DiscoverySummaryResponse | null;
  anomalies: DiscoveryAnomaliesResponse | null;
}

function FunnelBar({ label, value, max, color }: { label: string; value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.min(1, value / max) : 0;
  return (
    <div className="flex items-center gap-3">
      <div className="w-16 text-right text-[11px] text-gray-500 shrink-0">{label}</div>
      <div className="flex-1 h-1.5 bg-white/5 rounded-full overflow-hidden">
        <motion.div
          className={`h-full rounded-full ${color}`}
          initial={{ width: 0 }}
          animate={{ width: `${pct * 100}%` }}
          transition={{ duration: 0.8, ease: 'easeOut' }}
        />
      </div>
      <div className="w-12 text-[11px] text-gray-400 tabular-nums">{value.toLocaleString()}</div>
    </div>
  );
}

export function DiscoveryPanel({ summary, anomalies }: DiscoveryPanelProps) {
  const u = summary?.universe;
  const stats = summary?.last_24h;
  const totalBroker = u?.total_broker_instruments ?? 0;
  const totalTiered = u?.total_tiered ?? 0;
  const core = u?.core ?? 0;
  const scan = u?.scan ?? 0;
  const symbolsAnalysed = stats?.symbols_analysed ?? 0;
  const anomalyList = anomalies?.anomalies ?? [];

  const updatedAt = u?.tiers_updated_at
    ? (() => {
        const diff = Math.round((Date.now() - new Date(u.tiers_updated_at!).getTime()) / 60000);
        return diff < 1 ? 'just now' : diff < 60 ? `${diff}m ago` : `${Math.round(diff / 60)}h ago`;
      })()
    : null;

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="text-[10px] uppercase tracking-widest text-gray-600">Discovery</span>
          <span className="text-[10px] text-gray-700">· universe coverage</span>
        </div>
        {updatedAt && (
          <span className="text-[10px] text-gray-700">{updatedAt}</span>
        )}
      </div>

      {/* Universe funnel */}
      <div className="space-y-2">
        <FunnelBar label="available" value={totalBroker} max={totalBroker || 1} color="bg-gray-600" />
        <FunnelBar label="tiered" value={totalTiered} max={totalBroker || 1} color="bg-gray-500" />
        <FunnelBar label="core" value={core} max={totalBroker || 1} color="bg-amber-500/70" />
        <FunnelBar label="scan" value={scan} max={totalBroker || 1} color="bg-amber-500/30" />
        <FunnelBar label="signalled" value={symbolsAnalysed} max={totalBroker || 1} color="bg-emerald-500/60" />
      </div>

      {/* 24h counts */}
      <div className="grid grid-cols-3 gap-2 pt-1">
        {[
          { label: 'anomalies', value: stats?.anomalies_detected ?? 0, hot: (stats?.anomalies_detected ?? 0) > 0, title: 'Price/volume anomalies in 24h' },
          { label: 'theses', value: stats?.theses_generated ?? 0, hot: false, title: 'AI-generated opportunity theses in 24h' },
          { label: 'signals', value: stats?.signals_produced ?? 0, hot: (stats?.signals_produced ?? 0) > 0, title: 'Signals generated in 24h (inc. risk-vetoed)' },
        ].map(({ label, value, hot, title }) => (
          <div key={label} className="bg-white/3 rounded-lg px-3 py-2 text-center" title={title}>
            <div className={`text-lg font-light tabular-nums ${hot && value > 0 ? 'text-amber-400' : 'text-gray-300'}`}>
              {value}
            </div>
            <div className="text-[10px] text-gray-600 mt-0.5">{label}</div>
          </div>
        ))}
      </div>

      {/* Top anomalies */}
      {anomalyList.length > 0 && (
        <div className="space-y-1.5 pt-1">
          <div className="text-[10px] uppercase tracking-widest text-gray-600 mb-2">Top anomalies</div>
          {anomalyList.slice(0, 5).map((a) => {
            const z = parseFloat(a.price_z_score);
            const move = parseFloat(a.price_move_pct);
            const isUp = a.direction === 'up';
            const hot = Math.abs(z) >= 2;
            return (
              <motion.div
                key={a.id}
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                className="flex items-center gap-2 rounded-md bg-white/3 px-3 py-2"
              >
                <span className={`text-xs font-medium w-16 truncate ${hot ? 'text-amber-300' : 'text-gray-300'}`}>
                  {a.symbol}
                </span>
                <span className={`text-xs tabular-nums ${isUp ? 'text-emerald-400' : 'text-rose-400'}`}>
                  {isUp ? '+' : ''}{move.toFixed(1)}%
                </span>
                <span className="text-[10px] text-gray-500 ml-auto tabular-nums">z={z.toFixed(1)}</span>
                {hot && <span className="text-[10px]">🔥</span>}
              </motion.div>
            );
          })}
        </div>
      )}
    </div>
  );
}
