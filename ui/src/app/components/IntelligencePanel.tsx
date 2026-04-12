import { motion } from 'motion/react';
import type { IntelligenceRegimeResponse, IntelligenceSignalsResponse } from '../lib/api';

interface IntelligencePanelProps {
  regime: IntelligenceRegimeResponse | null;
  signals: IntelligenceSignalsResponse | null;
}

function RegimeChip({ label, confidence }: { label: string; confidence: number }) {
  const lower = label.toLowerCase();
  const colors: Record<string, string> = {
    bull: 'text-emerald-300 border-emerald-400/30 bg-emerald-400/8',
    bullish: 'text-emerald-300 border-emerald-400/30 bg-emerald-400/8',
    bear: 'text-rose-300 border-rose-400/30 bg-rose-400/8',
    bearish: 'text-rose-300 border-rose-400/30 bg-rose-400/8',
    neutral: 'text-amber-200 border-amber-300/30 bg-amber-300/8',
    unknown: 'text-gray-500 border-gray-600/30 bg-gray-600/8',
  };
  const key = Object.keys(colors).find((k) => lower.includes(k)) ?? 'neutral';
  return (
    <div className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 ${colors[key]}`}>
      <span className="text-sm font-light capitalize">{label || 'unknown'}</span>
      <span className="text-[10px] opacity-60">{Math.round(confidence * 100)}% conf</span>
    </div>
  );
}

export function IntelligencePanel({ regime, signals }: IntelligencePanelProps) {
  const reg = regime?.regime;
  const movers = regime?.top_movers ?? [];
  const signalList = signals?.signals ?? [];

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="text-[10px] uppercase tracking-widest text-gray-600">Intelligence</span>
          <span className="text-[10px] text-gray-700">· regime &amp; signals</span>
        </div>
        {reg?.updated_at && (
          <span className="text-[10px] text-gray-700">
            {(() => {
              const diff = Math.round((Date.now() - new Date(reg.updated_at).getTime()) / 60000);
              return diff < 1 ? 'just now' : diff < 60 ? `${diff}m ago` : `${Math.round(diff / 60)}h ago`;
            })()}
          </span>
        )}
      </div>

      {/* Macro regime */}
      {reg && (
        <div className="space-y-2">
          <RegimeChip label={reg.label ?? 'unknown'} confidence={reg.confidence ?? 0} />
          {reg.rationale && (
            <p className="text-[11px] text-gray-500 leading-relaxed line-clamp-2">{reg.rationale}</p>
          )}
        </div>
      )}

      {/* Top news movers — only show symbols with a real score */}
      {(() => {
        const scored = movers.filter((m) => Number.isFinite(m.score) && Math.abs(m.score) > 0.001);
        return (
          <div className="space-y-1.5">
            <div className="text-[10px] uppercase tracking-widest text-gray-600 mb-2">News movers</div>
            {scored.length === 0 ? (
              <div className="text-[11px] text-gray-700 italic leading-relaxed">
                No directional scores in the last 48h (or only neutral / no headline match). Run the trading loop or{' '}
                <code className="text-gray-500">run_m3</code> so the AI pipeline persists scores to the database.
              </div>
            ) : (
              scored.slice(0, 5).map((m) => {
                const positive = m.score > 0;
                return (
                  <div key={m.symbol} className="flex items-center gap-2">
                    <span className="text-xs font-medium text-gray-300 w-16 truncate">{m.symbol}</span>
                    <div className="flex-1 h-1 bg-white/5 rounded-full overflow-hidden">
                      <motion.div
                        className={`h-full rounded-full ${positive ? 'bg-emerald-500/70' : 'bg-rose-500/70'}`}
                        initial={{ width: 0 }}
                        animate={{ width: `${Math.min(100, Math.abs(m.score) * 100)}%` }}
                        transition={{ duration: 0.6, ease: 'easeOut' }}
                      />
                    </div>
                    <span className={`text-[11px] tabular-nums w-10 text-right ${positive ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {positive ? '+' : ''}{m.score.toFixed(2)}
                    </span>
                  </div>
                );
              })
            )}
          </div>
        );
      })()}

      {/* Signal queue — always show heading; empty state explains missing rows */}
      <div className="space-y-1.5">
        <div className="text-[10px] uppercase tracking-widest text-gray-600 mb-2">Signal queue</div>
        {signalList.length === 0 ? (
          <div className="text-[11px] text-gray-700 italic leading-relaxed">
            No recent signals in the database (last 6h). Signals appear after the runner evaluates strategies and logs them.
          </div>
        ) : (
          signalList.slice(0, 6).map((s) => {
            const approved = s.verdict === 'approved';
            const buyColor = s.side === 'buy' ? 'text-emerald-400' : 'text-rose-400';
            const age = s.timestamp
              ? (() => {
                  const diff = Math.round((Date.now() - new Date(s.timestamp).getTime()) / 60000);
                  return diff < 1 ? 'now' : diff < 60 ? `${diff}m` : `${Math.round(diff / 60)}h`;
                })()
              : '';
            const quality = s.quality_score ?? null;
            const qualityColor =
              quality === null ? 'text-gray-600' :
              quality >= 0.60 ? 'text-emerald-400' :
              quality >= 0.35 ? 'text-amber-400' : 'text-rose-400';
            const checks = s.checks_failed ?? [];
            const reasonLabel = checks.length > 0
              ? checks[0].replace(/_/g, ' ')
              : s.risk_reason.replace(/^Failed:\s*/i, '').split(/[,;]/)[0].slice(0, 30);

            return (
              <div key={s.id} className={`rounded border px-2 py-1.5 space-y-0.5 ${approved ? 'border-white/5 bg-white/2' : 'border-rose-900/20 bg-rose-950/10'}`}>
                <div className="flex items-center gap-1.5 text-[11px]">
                  <span className={`font-semibold w-3 shrink-0 ${buyColor}`}>{s.side === 'buy' ? '▲' : '▼'}</span>
                  <span className="text-gray-200 font-medium w-16 truncate">{s.symbol}</span>
                  {quality !== null && (
                    <span className={`tabular-nums text-[10px] font-medium ${qualityColor}`}>
                      Q{Math.round(quality * 100)}
                    </span>
                  )}
                  <span className="text-gray-600 flex-1 truncate text-[10px]">{s.strategy.replace(/_/g, ' ')}</span>
                  <span className="text-gray-700 tabular-nums text-[10px] shrink-0">{age}</span>
                  <span className={`w-2 h-2 rounded-full shrink-0 ${approved ? 'bg-emerald-500/80' : 'bg-rose-500/60'}`} />
                </div>
                {!approved && reasonLabel && (
                  <div className="text-[10px] text-rose-400/50 pl-3.5 truncate">
                    ✕ {reasonLabel}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
