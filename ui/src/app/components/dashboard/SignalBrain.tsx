import { ScrollArea } from '../ui/scroll-area';
import type { DashboardSnapshot } from '../../lib/api';
import type { WsTickEvent } from '../../lib/ws';
import { formatWsEventLine } from '../../lib/ws';
import { convictionRowsFromSnapshot, parseAccumulatorScore } from '../../lib/dashboardFallbacks';

type Props = {
  snapshot: DashboardSnapshot | null;
  events: WsTickEvent[];
  dormant: boolean;
  snapshotFetchFailed?: boolean;
  /** When conviction is empty, show position-based stand-ins. */
  positions?: Array<{ symbol: string; change: number }>;
};

function scoreCell(row: Record<string, unknown>): string {
  const s = row.score;
  if (typeof s === 'string') return s;
  if (typeof s === 'number') return String(s);
  return '—';
}

function scoreClass(row: Record<string, unknown>): string {
  const d = String(row.direction ?? '').toLowerCase();
  if (d === 'bullish' || d === 'long') return 'text-emerald-300/90';
  if (d === 'bearish' || d === 'short') return 'text-rose-300/90';
  const v = parseAccumulatorScore(row);
  if (v > 0.05) return 'text-emerald-300/90';
  if (v < -0.05) return 'text-rose-300/90';
  return 'text-zinc-300';
}

function arrowForRow(row: Record<string, unknown>): string {
  const d = String(row.direction ?? '').toLowerCase();
  if (d === 'bullish' || d === 'long') return '↑';
  if (d === 'bearish' || d === 'short') return '↓';
  const v = parseAccumulatorScore(row);
  if (v > 0.02) return '↑';
  if (v < -0.02) return '↓';
  return '·';
}

export function SignalBrain({
  snapshot,
  events,
  dormant,
  snapshotFetchFailed = false,
  positions = [],
}: Props) {
  let rows = convictionRowsFromSnapshot(snapshot);
  const usingPositionFallback = !dormant && rows.length === 0 && positions.length > 0;
  if (usingPositionFallback) {
    rows = positions.slice(0, 8).map((p) => ({
      symbol: p.symbol,
      score: (p.change / 100).toFixed(3),
      direction: p.change >= 0 ? 'long' : 'short',
    })) as Array<Record<string, unknown>>;
  }

  const lines: string[] = [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const line = formatWsEventLine(events[i]!);
    if (line) lines.push(line);
    if (lines.length >= 18) break;
  }

  return (
    <div className="flex flex-col h-full min-h-0 rounded-xl border border-white/5 bg-white/[0.02] p-2.5">
      <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Signal brain</div>
      {snapshotFetchFailed ? (
        <div className="text-[10px] text-amber-400/90 mb-2 leading-snug">
          Snapshot unavailable — fix read token (banner above) to load conviction.
        </div>
      ) : null}

      <div className="text-[10px] uppercase tracking-wider text-zinc-600 mb-1">Top conviction</div>
      <div className="space-y-0.5 mb-3 font-mono text-[11px] border-b border-white/5 pb-2">
        {dormant ? (
          <div className="text-zinc-600">System off — no live memory</div>
        ) : rows.length === 0 ? (
          <div className="text-zinc-600">No ranked conviction yet — start the loop or check data pipeline.</div>
        ) : (
          rows.map((r, i) => (
            <div key={`${String(r.symbol)}-${i}`} className="flex justify-between gap-2 items-baseline">
              <span className="text-white/90 truncate">{String(r.symbol ?? '')}</span>
              <span className={`${scoreClass(r)} shrink-0 tabular-nums`}>
                {scoreCell(r)} {arrowForRow(r)}
              </span>
            </div>
          ))
        )}
        {usingPositionFallback ? (
          <div className="text-[9px] text-zinc-600 pt-1">Showing positions as proxy — accumulator empty.</div>
        ) : null}
      </div>

      <div className="text-[10px] uppercase tracking-wider text-zinc-600 mb-1">Live flow</div>
      <ScrollArea className="h-[min(200px,28vh)] rounded border border-white/5 bg-black/20">
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
