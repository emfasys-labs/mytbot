import { ScrollArea } from '../ui/scroll-area';
import type { DashboardSnapshot } from '../../lib/api';

type Props = {
  snapshot: DashboardSnapshot | null;
  dormant: boolean;
};

export function AllocationCenter({ snapshot, dormant }: Props) {
  const opps = (snapshot?.opportunities ?? []) as Array<Record<string, unknown>>;
  const weakest = (snapshot?.portfolio?.weakest_by_hold_score ?? []) as Array<Record<string, unknown>>;
  const instr = (snapshot?.execution_plan?.instructions ?? []) as Array<Record<string, unknown>>;
  const targets = (snapshot?.allocation?.allocation_targets as Array<Record<string, unknown>> | undefined) ?? [];
  const repl = (snapshot?.allocation?.replacement_candidates ?? []) as Array<Record<string, unknown>>;

  return (
    <div className="flex flex-col gap-3 min-h-0">
      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-3">
        <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Capital & targets</div>
        {dormant || !snapshot ? (
          <div className="text-xs text-zinc-600">Start the system for allocator snapshot</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-[11px]">
            <div>
              <div className="text-zinc-500 mb-1">Exposure targets</div>
              <div className="text-zinc-300 font-mono text-[10px] space-y-0.5">
                <div>
                  gross{' '}
                  <span className="text-white/90">{String(snapshot.allocation?.gross_exposure_target ?? '—')}</span>
                </div>
                <div>
                  net{' '}
                  <span className="text-white/90">{String(snapshot.allocation?.net_exposure_target ?? '—')}</span>
                </div>
              </div>
            </div>
            <div>
              <div className="text-zinc-500 mb-1">Weights (top)</div>
              <div className="space-y-1 max-h-[100px] overflow-y-auto">
                {targets.slice(0, 8).map((t, i) => (
                  <div key={i} className="flex justify-between gap-2 font-mono text-[10px]">
                    <span className="truncate text-white/90">{String(t.symbol ?? '')}</span>
                    <span className="text-emerald-300/80 tabular-nums">{String(t.target_weight ?? '')}</span>
                  </div>
                ))}
                {targets.length === 0 ? <span className="text-zinc-600">—</span> : null}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 min-h-0">
        <div className="rounded-xl border border-white/5 bg-white/[0.02] p-3 min-h-[140px]">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Top opportunities</div>
          {dormant || opps.length === 0 ? (
            <div className="text-xs text-zinc-600">No ranked opportunities</div>
          ) : (
            <ScrollArea className="h-[160px]">
              <table className="w-full text-[11px] font-mono">
                <thead>
                  <tr className="text-left text-zinc-500 text-[10px]">
                    <th className="pb-1">Sym</th>
                    <th className="pb-1">Score</th>
                    <th className="pb-1 hidden sm:table-cell">Tags</th>
                  </tr>
                </thead>
                <tbody>
                  {opps.slice(0, 12).map((o, i) => (
                    <tr key={i} className="border-t border-white/5">
                      <td className="py-1 text-white/90">{String(o.symbol ?? '')}</td>
                      <td className="py-1 text-emerald-300/90 tabular-nums">{String(o.opportunity_score ?? '')}</td>
                      <td className="py-1 text-zinc-500 truncate max-w-[120px] hidden sm:table-cell">
                        {Array.isArray(o.tags) ? (o.tags as string[]).slice(0, 3).join(' · ') : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollArea>
          )}
        </div>

        <div className="rounded-xl border border-white/5 bg-white/[0.02] p-3 min-h-[140px]">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Hold pressure</div>
          {dormant || weakest.length === 0 ? (
            <div className="text-xs text-zinc-600">No weak holdings flagged</div>
          ) : (
            <ScrollArea className="h-[160px]">
              <table className="w-full text-[11px] font-mono">
                <thead>
                  <tr className="text-left text-zinc-500 text-[10px]">
                    <th className="pb-1">Sym</th>
                    <th className="pb-1">Hold</th>
                    <th className="pb-1">Exit</th>
                  </tr>
                </thead>
                <tbody>
                  {weakest.slice(0, 10).map((w, i) => (
                    <tr key={i} className="border-t border-white/5">
                      <td className="py-1 text-white/90">{String(w.symbol ?? '')}</td>
                      <td className="py-1 text-zinc-300 tabular-nums">{String(w.hold_score ?? '')}</td>
                      <td className="py-1 text-amber-300/80 tabular-nums">{String(w.exit_pressure ?? '')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollArea>
          )}
        </div>
      </div>

      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-3">
        <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Planned actions (allocator)</div>
        {dormant || instr.length === 0 ? (
          <div className="text-xs text-zinc-600">No instructions this tick</div>
        ) : (
          <ScrollArea className="h-[120px]">
            <ul className="space-y-1 font-mono text-[11px] text-zinc-300">
              {instr.slice(0, 14).map((x, i) => (
                <li key={i} className="flex flex-wrap gap-x-2 border-b border-white/5 pb-1">
                  <span className="text-white/90">{String(x.action ?? '')}</span>
                  <span>{String(x.symbol ?? '')}</span>
                  <span className="text-zinc-500">{String(x.side ?? '')}</span>
                  <span className="text-emerald-300/80 tabular-nums">{String(x.target_notional ?? '')}</span>
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}
        {repl.length > 0 ? (
          <div className="mt-2 text-[10px] text-zinc-500">
            Replacements ·{' '}
            {repl.slice(0, 4).map((r, i) => (
              <span key={i} className="mr-2">
                {String(r.old_symbol)}→{String(r.new_symbol)} ({String(r.recommended_action ?? '')})
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
