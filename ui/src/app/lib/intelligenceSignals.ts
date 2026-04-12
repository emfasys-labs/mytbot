import type { IntelligenceSignalsResponse } from './api';

/**
 * One row per (symbol, strategy, side), newest first. Matches server-side dedupe so the
 * dashboard stays readable even if an older API process is still running.
 */
export function dedupeIntelligenceSignals(
  raw: IntelligenceSignalsResponse['signals'] | undefined,
  max: number,
): NonNullable<IntelligenceSignalsResponse['signals']> {
  const list = [...(raw ?? [])].sort((a, b) => {
    const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
    const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
    return tb - ta;
  });
  const seen = new Set<string>();
  const out: NonNullable<IntelligenceSignalsResponse['signals']> = [];
  for (const s of list) {
    const k = `${(s.symbol || '').toUpperCase()}|${(s.strategy || '').toLowerCase().trim()}|${(s.side || '').toLowerCase().trim()}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(s);
    if (out.length >= max) break;
  }
  return out;
}
