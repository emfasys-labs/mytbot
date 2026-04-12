import { ScrollArea } from '../ui/scroll-area';
import type { DashboardSnapshot } from '../../lib/api';
import type { WsTickEvent } from '../../lib/ws';
import { formatWsEventLine } from '../../lib/ws';

type Props = {
  snapshot: DashboardSnapshot | null;
  events: WsTickEvent[];
  dormant: boolean;
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
  return 'text-zinc-300';
}

export function SignalBrain({ snapshot, events, dormant }: Props) {
  const acc = snapshot?.accumulator;
  const rows = (acc?.top_by_magnitude ?? []).slice(0, 8) as Array<Record<string, unknown>>;

  const lines: string[] = [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const line = formatWsEventLine(events[i]!);
    if (line) lines.push(line);
    if (lines.length >= 18) break;
  }

  return (
    <div className="flex flex-col h-full min-h-0 rounded-xl border border-white/5 bg-white/[0.02] p-3">
      <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Signal brain</div>
      <div className="text-[11px] text-zinc-400 mb-2">Conviction (accumulator)</div>
      <div className="space-y-1 mb-3 font-mono text-[11px]">
        {dormant ? (
          <div className="text-zinc-600">Off — no live memory</div>
        ) : rows.length === 0 ? (
          <div className="text-zinc-600">No symbols yet</div>
        ) : (
          rows.map((r, i) => (
            <div key={`${String(r.symbol)}-${i}`} className="flex justify-between gap-2">
              <span className="text-white/90 truncate">{String(r.symbol ?? '')}</span>
              <span className={`${scoreClass(r)} shrink-0 tabular-nums`}>{scoreCell(r)}</span>
            </div>
          ))
        )}
      </div>
      <div className="text-[11px] text-zinc-400 mb-1">Live stream</div>
      <ScrollArea className="h-[180px] rounded border border-white/5 bg-black/20">
        <div className="p-2 space-y-1 font-mono text-[10px] text-zinc-400">
          {lines.length === 0 ? (
            <div className="text-zinc-600">Waiting for events…</div>
          ) : (
            lines.map((ln, i) => (
              <div key={`${i}-${ln}`} className="truncate border-b border-white/5 pb-1">
                {ln}
              </div>
            ))
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
