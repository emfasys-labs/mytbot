import { motion } from 'motion/react';
import { ScrollArea } from '../ui/scroll-area';
import type { DashboardSnapshot } from '../../lib/api';
import {
  OPPORTUNITY_THRESHOLD_HINT,
  buildFallbackHoldPressure,
  buildFallbackOpportunities,
  parseOpportunityRowScore,
} from '../../lib/dashboardFallbacks';
import {
  arrowForRaw,
  bandFromDisplay01,
  convictionTextClass,
  displayConviction01,
  fmtRawScore,
} from '../../lib/scoreDisplay';
import { formatCoordinatorKind } from '../../lib/coordinatorLabels';
import { fmtDashNum } from '../../lib/dashboardFormat';

type Props = {
  snapshot: DashboardSnapshot | null;
  dormant: boolean;
  snapshotFetchFailed?: boolean;
  positions?: Array<{ symbol: string; change: number }>;
};

function rowDisplay01(o: Record<string, unknown>): number {
  if (typeof o.display01 === 'number' && Number.isFinite(o.display01)) return o.display01;
  return displayConviction01(parseOpportunityRowScore(o));
}

function rowTooltip(o: Record<string, unknown>): string {
  const parts: string[] = [];
  const raw =
    typeof o.raw_score === 'number'
      ? o.raw_score
      : parseOpportunityRowScore(o);
  parts.push(`Raw: ${fmtRawScore(raw)}`);
  if (o.strategy_name != null && String(o.strategy_name).trim()) {
    parts.push(`strategy: ${String(o.strategy_name)}`);
  }
  const comp = o.components;
  if (comp && typeof comp === 'object') {
    for (const [k, v] of Object.entries(comp as Record<string, string>)) {
      parts.push(`${k.replace(/_/g, ' ')}: ${v}`);
    }
  }
  if (o.confidence != null) parts.push(`confidence: ${String(o.confidence)}`);
  return parts.join('\n');
}

function holdRowsAllZero(rows: Array<Record<string, unknown>>): boolean {
  if (!rows.length) return false;
  return rows.every((w) => {
    const h = parseFloat(String(w.hold_score ?? '0'));
    const e = parseFloat(String(w.exit_pressure ?? '0'));
    return (Number.isFinite(h) ? h : 0) === 0 && (Number.isFinite(e) ? e : 0) === 0;
  });
}

/** Tags column: prefer snapshot tags, else strategy/side so global_edge rows are never blank. */
function opportunityTagsLine(o: Record<string, unknown>): string {
  const tags = Array.isArray(o.tags) ? (o.tags as string[]).filter(Boolean) : [];
  const joined = tags.slice(0, 3).join(' · ');
  if (joined) return joined;
  const sn = o.strategy_name != null && String(o.strategy_name).trim();
  if (sn) return String(o.strategy_name);
  const side = o.side != null && String(o.side).trim();
  if (side) return String(o.side);
  return '—';
}

function urgencyLabel(d: number): string {
  if (d >= 0.7) return 'HIGH';
  if (d >= 0.45) return 'MEDIUM';
  return 'LOW';
}

function instructionStatus(v: string): 'pending' | 'waiting risk' | 'executing' {
  const s = v.toLowerCase();
  if (s.includes('approve') || s.includes('risk')) return 'waiting risk';
  if (s.includes('open') || s.includes('buy') || s.includes('sell') || s.includes('close')) return 'executing';
  return 'pending';
}

export function AllocationCenter({
  snapshot,
  dormant,
  snapshotFetchFailed = false,
  positions = [],
}: Props) {
  const pathKind = snapshot?.path ?? '';
  const isGlobalEdge = pathKind === 'global_edge';
  const portfolio = (snapshot?.portfolio ?? {}) as Record<string, unknown>;
  const oppsRaw = (snapshot?.opportunities ?? []) as Array<Record<string, unknown>>;
  const weakestRaw = (snapshot?.portfolio?.weakest_by_hold_score ?? []) as Array<Record<string, unknown>>;
  const instr = (snapshot?.execution_plan?.instructions ?? []) as Array<Record<string, unknown>>;
  const allocRec =
    snapshot?.allocation && typeof snapshot.allocation === 'object'
      ? (snapshot.allocation as Record<string, unknown>)
      : null;
  const targets = (allocRec?.allocation_targets as Array<Record<string, unknown>> | undefined) ?? [];
  const repl = (allocRec?.replacement_candidates ?? []) as Array<Record<string, unknown>>;
  const planRationale = snapshot?.execution_plan?.rationale;
  const allocRationale = allocRec?.rationale;

  const grossRaw =
    allocRec?.gross_exposure_target != null && String(allocRec.gross_exposure_target) !== ''
      ? allocRec.gross_exposure_target
      : portfolio.gross_exposure != null && String(portfolio.gross_exposure) !== ''
        ? portfolio.gross_exposure
        : null;
  const grossDisplay = grossRaw == null ? '—' : fmtDashNum(grossRaw);
  const netRaw =
    allocRec?.net_exposure_target != null && String(allocRec.net_exposure_target) !== ''
      ? allocRec.net_exposure_target
      : portfolio.net_exposure != null && String(portfolio.net_exposure) !== ''
        ? portfolio.net_exposure
        : null;
  const netDisplay = netRaw == null ? '—' : fmtDashNum(netRaw);
  const navRaw =
    portfolio.nav != null && String(portfolio.nav) !== '' ? portfolio.nav : null;
  const navDisplay = navRaw == null ? null : fmtDashNum(navRaw);

  const weightRowsFromOpps = oppsRaw.slice(0, 8).map((o) => ({
    symbol: o.symbol,
    target_weight: o.priority_score ?? o.opportunity_score ?? o.expected_edge,
  }));
  const weightRows = targets.length > 0 ? targets : isGlobalEdge ? weightRowsFromOpps : [];
  const weightsSub =
    targets.length > 0
      ? 'Weights (top)'
      : isGlobalEdge && weightRowsFromOpps.length > 0
        ? 'Strategy rank (priority)'
        : 'Weights (top)';

  const usingOppFallback = !dormant && !snapshotFetchFailed && oppsRaw.length === 0 && snapshot != null;
  const opps = usingOppFallback ? buildFallbackOpportunities(snapshot, positions) : oppsRaw;

  const usingHoldFallback =
    !dormant &&
    !snapshotFetchFailed &&
    positions.length > 0 &&
    (weakestRaw.length === 0 || holdRowsAllZero(weakestRaw));
  const weakest = usingHoldFallback ? buildFallbackHoldPressure(positions) : weakestRaw;

  const idleCopy =
    !dormant && instr.length === 0 && snapshot
      ? [
          'No allocator instructions this tick.',
          typeof planRationale === 'string' && planRationale.trim()
            ? planRationale.trim()
            : 'No single name cleared the replacement / risk gates for execution.',
          typeof allocRationale === 'string' && String(allocRationale).trim()
            ? String(allocRationale).trim()
            : planRationale === 'global_edge_coordinator'
              ? 'On global_edge, D015 allocation targets are not emitted — Capital shows book exposure. Risk gate may still block adds under configured caps.'
              : `Typical: no opportunity exceeds the replacement threshold (${OPPORTUNITY_THRESHOLD_HINT}) or portfolio is already near targets.`,
        ]
      : null;
  const showHoldPanel = weakest.length > 0 || usingHoldFallback;

  return (
    <div className="relative z-0 isolate flex min-h-0 w-full min-w-0 flex-col gap-2 overflow-x-hidden">
      {repl.length > 0 ? (
        <div className="rounded-xl border border-amber-500/25 bg-gradient-to-br from-amber-950/40 to-black/40 p-2.5 shadow-[0_0_24px_rgba(251,191,36,0.06)]">
          <div className="text-[10px] uppercase tracking-widest text-amber-200/90 mb-1.5">Replacement view</div>
          <ul className="space-y-1.5 text-[11px] font-mono text-zinc-200">
            {repl.slice(0, 6).map((r, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-x-2 border-b border-white/5 pb-1">
                <span className="text-rose-300/90">SELL</span>
                <span>{String(r.old_symbol ?? '')}</span>
                <span className="text-zinc-500">→</span>
                <span className="text-emerald-300/90">BUY</span>
                <span>{String(r.new_symbol ?? '')}</span>
                {r.recommended_action != null ? (
                  <span className="text-zinc-500 text-[10px]">({String(r.recommended_action)})</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="w-full min-w-0 rounded-xl border border-white/5 bg-white/[0.02] p-2.5">
        <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1.5">Capital & targets</div>
        {dormant ? (
          <div className="text-xs text-zinc-600">Start the system to load allocator data.</div>
        ) : snapshotFetchFailed ? (
          <div className="text-xs text-amber-200/90 leading-relaxed">
            Snapshot blocked — use the amber banner to set the read token. Until then, allocator fields stay blank.
          </div>
        ) : !snapshot ? (
          <div className="text-xs text-zinc-600">Loading allocator snapshot…</div>
        ) : (
          <div className="grid w-full grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 text-[11px] items-start">
            {isGlobalEdge && !allocRec ? (
              <div className="sm:col-span-2 text-[9px] text-zinc-500 leading-snug">
                Book snapshot — global_edge path does not publish D015 allocator targets. Gross/net below reflect live
                portfolio state.
              </div>
            ) : null}
            <div className="min-w-0">
              <div className="text-zinc-500 mb-0.5 text-[10px]">
                {allocRec ? 'Exposure targets' : 'Exposure (book)'}
              </div>
              <div className="text-zinc-300 font-mono text-[10px] space-y-0.5">
                <div>
                  gross <span className="text-white/90 tabular-nums">{grossDisplay}</span>
                </div>
                <div>
                  net <span className="text-white/90 tabular-nums">{netDisplay}</span>
                </div>
                {navDisplay != null ? (
                  <div>
                    nav <span className="text-white/90 tabular-nums">{navDisplay}</span>
                  </div>
                ) : null}
              </div>
            </div>
            <div className="min-w-0 sm:border-l sm:border-white/5 sm:pl-6">
              <div className="text-zinc-500 mb-0.5 text-[10px]">{weightsSub}</div>
              <div className="space-y-0.5 max-h-[88px] overflow-y-auto">
                {weightRows.slice(0, 8).map((t, i) => (
                  <div key={i} className="flex justify-between gap-3 font-mono text-[10px]">
                    <span className="truncate text-white/90">{String(t.symbol ?? '')}</span>
                    <span className="shrink-0 text-emerald-300/80 tabular-nums">{fmtDashNum(t.target_weight)}</span>
                  </div>
                ))}
                {weightRows.length === 0 ? <span className="text-zinc-600">—</span> : null}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className={`grid min-h-0 w-full min-w-0 grid-cols-1 gap-3 overflow-hidden ${showHoldPanel ? 'lg:grid-cols-2 lg:items-stretch' : ''}`}>
        <div className="flex min-h-0 min-w-0 flex-col gap-2 lg:min-w-0">
        <div className="rounded-xl border border-sky-500/20 bg-gradient-to-b from-sky-950/20 to-white/[0.02] p-2.5 min-h-0 shrink-0 overflow-hidden">
          <div className="text-[10px] uppercase tracking-widest text-zinc-400 mb-1.5">Active opportunities</div>
          {dormant ? (
            <div className="text-xs text-zinc-600">System off</div>
          ) : usingOppFallback ? (
            <>
              <div className="text-[10px] text-zinc-500 mb-1">
                No allocator rank this tick (threshold ~{OPPORTUNITY_THRESHOLD_HINT}) — next-best from accumulator /
                book.
              </div>
              <ScrollArea className="h-[150px]">
                <div className="space-y-1.5">
                    {opps.slice(0, 12).map((o, i) => {
                      const d = rowDisplay01(o);
                      const raw = parseOpportunityRowScore(o);
                      const isFirst = i === 0;
                      const width = `${Math.max(6, Math.round(d * 100))}%`;
                      return (
                        <motion.div
                          key={i}
                          title={rowTooltip(o)}
                          className={`rounded border border-white/5 px-2 py-1 ${isFirst ? 'bg-emerald-500/[0.07]' : 'bg-black/20'}`}
                          initial={isFirst ? { backgroundColor: 'rgba(16,185,129,0.12)' } : undefined}
                          animate={{ backgroundColor: isFirst ? 'rgba(16,185,129,0.07)' : 'transparent' }}
                          transition={{ duration: 0.6 }}
                        >
                          <div className="mb-1 flex items-center justify-between gap-2 text-[11px] font-mono">
                            <span className={`text-white/90 ${isFirst ? 'font-semibold' : ''}`}>{String(o.symbol ?? '')}</span>
                            <span className={`tabular-nums ${convictionTextClass(d, true)}`}>{d.toFixed(2)} {arrowForRaw(raw)}</span>
                            <span className="text-[10px] text-zinc-500">{urgencyLabel(d)}</span>
                          </div>
                          <div className="h-1.5 rounded bg-zinc-800">
                            <div className="h-full rounded bg-emerald-400/80" style={{ width }} />
                          </div>
                        </motion.div>
                      );
                    })}
                </div>
              </ScrollArea>
            </>
          ) : opps.length === 0 ? (
            <div className="text-xs text-zinc-600">No opportunities in snapshot.</div>
          ) : (
            <ScrollArea className="h-[150px]">
              <div className="space-y-1.5">
                  {opps.slice(0, 12).map((o, i) => {
                    const d = rowDisplay01(o);
                    const raw = parseOpportunityRowScore(o);
                    const isFirst = i === 0;
                    const width = `${Math.max(6, Math.round(d * 100))}%`;
                    return (
                      <motion.div
                        key={i}
                        title={rowTooltip(o)}
                        className={`rounded border border-white/5 px-2 py-1 ${isFirst ? 'bg-emerald-500/[0.07]' : 'bg-black/20'}`}
                          initial={isFirst ? { backgroundColor: 'rgba(16,185,129,0.1)' } : undefined}
                          animate={{ backgroundColor: isFirst ? 'rgba(16,185,129,0.07)' : 'transparent' }}
                      >
                        <div className="mb-1 flex items-center justify-between gap-2 text-[11px] font-mono">
                          <span className={`text-white/90 ${isFirst ? 'font-semibold' : ''}`}>{String(o.symbol ?? '')}</span>
                          <span className={`tabular-nums ${convictionTextClass(d, true)}`}>
                            {d.toFixed(2)} {arrowForRaw(raw)}
                          </span>
                          <span className="text-[10px] text-zinc-500">{urgencyLabel(d)}</span>
                        </div>
                        <div className="h-1.5 rounded bg-zinc-800">
                          <div className="h-full rounded bg-emerald-400/80" style={{ width }} />
                        </div>
                        <div className="mt-1 text-[10px] text-zinc-500 truncate">{opportunityTagsLine(o)}</div>
                      </motion.div>
                    );
                  })}
              </div>
            </ScrollArea>
          )}
        </div>

      <div className="rounded-xl border border-emerald-500/25 bg-gradient-to-b from-emerald-950/20 to-white/[0.02] p-2.5 min-h-0 flex flex-col flex-1 overflow-hidden">
        <div className="text-[10px] uppercase tracking-widest text-zinc-400 mb-1.5">What system is about to do</div>
        {!dormant && instr.length > 0 ? (
          <div className="mb-1.5 text-[9px] leading-snug text-zinc-500">
            Deployment intent from coordinator. Live status moves pending → waiting risk → executing.
          </div>
        ) : null}
        {dormant || instr.length === 0 ? (
          <div className="text-xs text-zinc-500 space-y-1.5">
            {dormant ? (
              <p>System off — no instructions.</p>
            ) : idleCopy ? (
              <ul className="list-disc pl-4 space-y-1 font-mono text-[10px] text-zinc-400">
                {idleCopy.map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            ) : (
              <p className="text-zinc-600">Waiting for allocator snapshot…</p>
            )}
          </div>
        ) : (
          <ScrollArea className="h-[120px] min-h-[120px] w-full">
            <ul className="space-y-1 font-mono text-[11px] text-zinc-300 pr-1">
              {instr.slice(0, 14).map((x, i) => (
                <li key={i} className="rounded border border-white/5 bg-black/20 px-2 py-1">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="text-white/90 shrink-0">{formatCoordinatorKind(String(x.action ?? x.kind ?? ''))}</span>
                    <span className="shrink-0">{String(x.symbol ?? '')}</span>
                    <span className="text-zinc-500 shrink-0">{String(x.side ?? '')}</span>
                    <span className="text-emerald-300/90 tabular-nums shrink-0">{fmtDashNum(x.capital ?? x.target_notional)}</span>
                  </div>
                  {x.strategy_name != null && String(x.strategy_name).trim() ? (
                    <div className="text-zinc-500 truncate max-w-[180px] text-[10px]">{String(x.strategy_name)}</div>
                  ) : null}
                  <div className="mt-0.5 flex items-center justify-between text-[10px]">
                    <span className="text-zinc-600">loop #{snapshot?.loop_iteration ?? '—'}</span>
                    <span className="uppercase text-sky-300/90">{instructionStatus(String(x.action ?? x.kind ?? ''))}</span>
                  </div>
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}
      </div>
        </div>

        {showHoldPanel ? (
        <div className="flex h-full min-h-[300px] min-w-0 flex-col rounded-xl border border-white/5 bg-white/[0.02] p-2.5 lg:min-h-0">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1.5 shrink-0">Hold pressure</div>
          {dormant ? (
            <div className="text-xs text-zinc-600">System off</div>
          ) : usingHoldFallback ? (
            <>
              <div className="text-[10px] text-zinc-500 mb-1">
                Allocator weak list empty — spread from book P&amp;L proxy (hold vs exit tension).
              </div>
              <ScrollArea className="min-h-0 w-full flex-1 lg:min-h-[200px]">
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
                        <td className="py-0.5 text-white/90">{String(w.symbol ?? '')}</td>
                        <td className="py-0.5 text-zinc-300 tabular-nums">{fmtDashNum(w.hold_score)}</td>
                        <td className="py-0.5 text-amber-300/80 tabular-nums">{fmtDashNum(w.exit_pressure)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollArea>
            </>
          ) : weakest.length === 0 ? (
            <div className="text-xs text-zinc-600">No weak holdings flagged.</div>
          ) : (
            <ScrollArea className="min-h-0 w-full flex-1 lg:min-h-[200px]">
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
                    <tr key={i} className="border-t border-white/5" title={String(w.symbol ?? '')}>
                      <td className="py-0.5 text-white/90">{String(w.symbol ?? '')}</td>
                      <td className="py-0.5 text-zinc-300 tabular-nums">{fmtDashNum(w.hold_score)}</td>
                      <td className="py-0.5 text-amber-300/80 tabular-nums">{fmtDashNum(w.exit_pressure)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollArea>
          )}
        </div>
        ) : null}
      </div>
    </div>
  );
}
