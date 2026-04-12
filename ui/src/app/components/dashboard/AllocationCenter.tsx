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

  return (
    <div className="flex flex-col gap-2 min-h-0">
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

      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-2.5">
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
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-[11px]">
            {isGlobalEdge && !allocRec ? (
              <div className="md:col-span-2 text-[9px] text-zinc-500 leading-snug">
                Book snapshot — global_edge path does not publish D015 allocator targets. Gross/net below reflect live
                portfolio state.
              </div>
            ) : null}
            <div>
              <div className="text-zinc-500 mb-0.5 text-[10px]">
                {allocRec ? 'Exposure targets' : 'Exposure (book)'}
              </div>
              <div className="text-zinc-300 font-mono text-[10px] space-y-0.5">
                <div>
                  gross <span className="text-white/90">{grossDisplay}</span>
                </div>
                <div>
                  net <span className="text-white/90">{netDisplay}</span>
                </div>
                {navDisplay != null ? (
                  <div>
                    nav <span className="text-white/90">{navDisplay}</span>
                  </div>
                ) : null}
              </div>
            </div>
            <div>
              <div className="text-zinc-500 mb-0.5 text-[10px]">{weightsSub}</div>
              <div className="space-y-0.5 max-h-[88px] overflow-y-auto">
                {weightRows.slice(0, 8).map((t, i) => (
                  <div key={i} className="flex justify-between gap-2 font-mono text-[10px]">
                    <span className="truncate text-white/90">{String(t.symbol ?? '')}</span>
                    <span className="text-emerald-300/80 tabular-nums">{fmtDashNum(t.target_weight)}</span>
                  </div>
                ))}
                {weightRows.length === 0 ? <span className="text-zinc-600">—</span> : null}
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-2 min-h-0">
        <div className="rounded-xl border border-white/5 bg-white/[0.02] p-2.5 min-h-[120px]">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1.5">Top opportunities</div>
          {dormant ? (
            <div className="text-xs text-zinc-600">System off</div>
          ) : usingOppFallback ? (
            <>
              <div className="text-[10px] text-zinc-500 mb-1">
                No allocator rank this tick (threshold ~{OPPORTUNITY_THRESHOLD_HINT}) — next-best from accumulator /
                book.
              </div>
              <ScrollArea className="h-[150px]">
                <table className="w-full text-[11px] font-mono">
                  <thead>
                    <tr className="text-left text-zinc-500 text-[10px]">
                      <th className="pb-1">Sym</th>
                      <th className="pb-1">Score</th>
                      <th className="pb-1 hidden sm:table-cell">Stance</th>
                      <th className="pb-1 hidden md:table-cell">Urgency</th>
                    </tr>
                  </thead>
                  <tbody>
                    {opps.slice(0, 12).map((o, i) => {
                      const d = rowDisplay01(o);
                      const raw = parseOpportunityRowScore(o);
                      const isFirst = i === 0;
                      return (
                        <motion.tr
                          key={i}
                          title={rowTooltip(o)}
                          className={`border-t border-white/5 ${isFirst ? 'bg-emerald-500/[0.07]' : ''}`}
                          initial={isFirst ? { backgroundColor: 'rgba(16,185,129,0.12)' } : undefined}
                          animate={{ backgroundColor: isFirst ? 'rgba(16,185,129,0.07)' : 'transparent' }}
                          transition={{ duration: 0.6 }}
                        >
                          <td className={`py-1 text-white/90 ${isFirst ? 'font-semibold' : ''}`}>
                            {String(o.symbol ?? '')}
                          </td>
                          <td
                            className={`py-1 tabular-nums ${convictionTextClass(d, true)}`}
                          >
                            {d.toFixed(2)} {arrowForRaw(raw)}
                          </td>
                          <td className="py-1 text-zinc-400 truncate max-w-[100px] hidden sm:table-cell">
                            {opportunityTagsLine(o)}
                          </td>
                          <td className="py-1 text-zinc-500 text-[10px] hidden md:table-cell">
                            {urgencyLabel(d)} · {bandFromDisplay01(d)}
                          </td>
                        </motion.tr>
                      );
                    })}
                  </tbody>
                </table>
              </ScrollArea>
            </>
          ) : opps.length === 0 ? (
            <div className="text-xs text-zinc-600">No opportunities in snapshot.</div>
          ) : (
            <ScrollArea className="h-[150px]">
              <table className="w-full text-[11px] font-mono">
                <thead>
                  <tr className="text-left text-zinc-500 text-[10px]">
                    <th className="pb-1">Sym</th>
                    <th className="pb-1">Score</th>
                    <th className="pb-1 hidden sm:table-cell">Tags</th>
                    <th className="pb-1 hidden md:table-cell">Urgency</th>
                  </tr>
                </thead>
                <tbody>
                  {opps.slice(0, 12).map((o, i) => {
                    const d = rowDisplay01(o);
                    const raw = parseOpportunityRowScore(o);
                    const isFirst = i === 0;
                    return (
                      <motion.tr
                        key={i}
                        title={rowTooltip(o)}
                        className={`border-t border-white/5 ${isFirst ? 'bg-emerald-500/[0.07]' : ''}`}
                          initial={isFirst ? { backgroundColor: 'rgba(16,185,129,0.1)' } : undefined}
                          animate={{ backgroundColor: isFirst ? 'rgba(16,185,129,0.07)' : 'transparent' }}
                      >
                        <td className={`py-1 text-white/90 ${isFirst ? 'font-semibold' : ''}`}>
                          {String(o.symbol ?? '')}
                        </td>
                        <td className={`py-1 tabular-nums ${convictionTextClass(d, true)}`}>
                          {d.toFixed(2)} {arrowForRaw(raw)}
                        </td>
                        <td className="py-1 text-zinc-500 truncate max-w-[120px] hidden sm:table-cell">
                          {opportunityTagsLine(o)}
                        </td>
                        <td className="py-1 text-zinc-500 text-[10px] hidden md:table-cell">
                          {urgencyLabel(d)}
                        </td>
                      </motion.tr>
                    );
                  })}
                </tbody>
              </table>
            </ScrollArea>
          )}
        </div>

        <div className="rounded-xl border border-white/5 bg-white/[0.02] p-2.5 min-h-[120px]">
          <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1.5">Hold pressure</div>
          {dormant ? (
            <div className="text-xs text-zinc-600">System off</div>
          ) : usingHoldFallback ? (
            <>
              <div className="text-[10px] text-zinc-500 mb-1">
                Allocator weak list empty — spread from book P&amp;L proxy (hold vs exit tension).
              </div>
              <ScrollArea className="h-[150px]">
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
            <ScrollArea className="h-[150px]">
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
      </div>

      <div className="rounded-xl border border-white/5 bg-white/[0.02] p-2.5">
        <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-1.5">Next actions (allocator)</div>
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
          <ScrollArea className="h-[100px]">
            <ul className="space-y-0.5 font-mono text-[11px] text-zinc-300">
              {instr.slice(0, 14).map((x, i) => (
                <li key={i} className="flex flex-wrap gap-x-2 border-b border-white/5 pb-0.5">
                  <span className="text-white/90">{formatCoordinatorKind(String(x.action ?? x.kind ?? ''))}</span>
                  <span>{String(x.symbol ?? '')}</span>
                  {x.strategy_name != null && String(x.strategy_name).trim() ? (
                    <span className="text-zinc-500 truncate max-w-[140px]">{String(x.strategy_name)}</span>
                  ) : null}
                  <span className="text-zinc-500">{String(x.side ?? '')}</span>
                  <span className="text-emerald-300/80 tabular-nums">{fmtDashNum(x.capital ?? x.target_notional)}</span>
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}
      </div>
    </div>
  );
}
