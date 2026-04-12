import { motion } from 'motion/react';
import { ScrollArea } from '../ui/scroll-area';
import type { DashboardSnapshot } from '../../lib/api';
import type { WsTickEvent } from '../../lib/ws';
import { bullishBearishFromSnapshot, parseAccumulatorScore } from '../../lib/dashboardFallbacks';
import { formatWsEventLine } from '../../lib/ws';
import {
  arrowForRaw,
  convictionTextClass,
  displayConviction01,
  fmtRawScore,
} from '../../lib/scoreDisplay';

type Props = {
  snapshot: DashboardSnapshot | null;
  events: WsTickEvent[];
  dormant: boolean;
  snapshotFetchFailed?: boolean;
  positions?: Array<{ symbol: string; change: number }>;
};

function scoreLine(row: Record<string, unknown>): string {
  const raw = parseAccumulatorScore(row);
  const d = displayConviction01(raw);
  const arrow = arrowForRaw(raw);
  return `${d.toFixed(2)} ${arrow}`;
}

export function SignalBrain({
  snapshot,
  events,
  dormant,
  snapshotFetchFailed = false,
  positions = [],
}: Props) {
  let { bullish, bearish } = bullishBearishFromSnapshot(snapshot);
  const usingPositionFallback = !dormant && bullish.length === 0 && bearish.length === 0 && positions.length > 0;
  if (usingPositionFallback) {
    const posRows = positions.slice(0, 8).map((p) => ({
      symbol: p.symbol,
      score: String((p.change / 100).toFixed(4)),
      direction: p.change >= 0 ? 'long' : 'short',
    })) as Array<Record<string, unknown>>;
    bullish = posRows.filter((r) => parseAccumulatorScore(r) >= 0).slice(0, 6);
    bearish = posRows.filter((r) => parseAccumulatorScore(r) < 0).slice(0, 6);
  }

  const lines: string[] = [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const line = formatWsEventLine(events[i]!);
    if (line) lines.push(line);
    if (lines.length >= 18) break;
  }

  const Row = ({ row, positive }: { row: Record<string, unknown>; positive: boolean }) => {
    const raw = parseAccumulatorScore(row);
    const d = displayConviction01(raw);
    return (
      <div
        className="flex justify-between gap-2 items-baseline"
        title={`Raw net: ${fmtRawScore(raw)} · display: ${d.toFixed(2)}`}
      >
        <span className="text-white/90 truncate">{String(row.symbol ?? '')}</span>
        <span className={`${convictionTextClass(d, positive)} shrink-0 tabular-nums font-medium`}>
          {scoreLine(row)}
        </span>
      </div>
    );
  };

  return (
    <div className="flex flex-col h-full min-h-0 rounded-xl border border-white/5 bg-white/[0.02] p-2.5">
      <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Signal brain</div>
      {snapshotFetchFailed ? (
        <div className="text-[10px] text-amber-400/90 mb-2 leading-snug">
          Snapshot unavailable — fix read token (banner above) to load conviction.
        </div>
      ) : null}

      <div className="text-[10px] uppercase tracking-wider text-emerald-600/90 mb-1">Top conviction</div>
      <div className="space-y-0.5 mb-2 font-mono text-[11px] border-b border-white/5 pb-2 min-h-[4.5rem]">
        {dormant ? (
          <div className="text-zinc-600">System off — no live memory</div>
        ) : bullish.length === 0 ? (
          <div className="text-zinc-600">No bullish edge in view.</div>
        ) : (
          bullish.map((r, i) => (
            <motion.div
              key={`bull-${String(r.symbol)}-${i}`}
              initial={{ opacity: 0, x: -4 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.03 }}
            >
              <Row row={r} positive />
            </motion.div>
          ))
        )}
      </div>

      <div className="text-[10px] uppercase tracking-wider text-rose-600/90 mb-1">Weakest</div>
      <div className="space-y-0.5 mb-3 font-mono text-[11px] border-b border-white/5 pb-2 min-h-[4.5rem]">
        {dormant ? (
          <div className="text-zinc-600">—</div>
        ) : bearish.length === 0 ? (
          <div className="text-zinc-600">No bearish edge in view.</div>
        ) : (
          bearish.map((r, i) => (
            <motion.div
              key={`bear-${String(r.symbol)}-${i}`}
              initial={{ opacity: 0, x: -4 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.03 }}
            >
              <Row row={r} positive={false} />
            </motion.div>
          ))
        )}
      </div>
      {usingPositionFallback ? (
        <div className="text-[9px] text-zinc-600 mb-2">Book proxy — accumulator empty.</div>
      ) : null}

      <div className="text-[10px] uppercase tracking-wider text-zinc-600 mb-1">Live flow</div>
      <ScrollArea className="h-[min(180px,26vh)] rounded border border-white/5 bg-black/20">
        <div className="p-2 space-y-1 font-mono text-[10px] text-zinc-400">
          {lines.length === 0 ? (
            <div className="text-zinc-600">
              {dormant ? 'Quiet — system not running.' : 'Waiting for signals, fills, and bus events…'}
            </div>
          ) : (
            lines.map((ln, i) => (
              <div key={`${i}-${ln.slice(0, 24)}`} className="border-b border-white/5 pb-1 leading-snug">
                {ln}
              </div>
            ))
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
