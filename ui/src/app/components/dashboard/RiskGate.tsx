import { ScrollArea } from '../ui/scroll-area';
import type { IntelligenceSignalsResponse } from '../../lib/api';

type Props = {
  signals: IntelligenceSignalsResponse | null;
  dormant: boolean;
};

const REASON_LABEL: Record<string, string> = {
  asset_class_limit: 'Would break asset-class allocation limit',
  max_exposure: 'Would exceed max exposure for this name or book',
  max_position: 'Would exceed max position size',
  max_orders: 'Order rate or count limit',
  kill_switch: 'Kill switch active',
  news_veto: 'News filter veto',
  liquidity: 'Liquidity / spread check failed',
  correlation: 'Correlation / concentration limit',
  options_trading: 'Options trading policy',
  default: 'Risk check failed',
};

function normalizeReason(raw: string | undefined): string {
  if (!raw) return '';
  return raw.replace(/^Failed:\s*/i, '').replace(/^rejected:\s*/i, '').trim();
}

function humanizeReason(code: string): string {
  const k = code.toLowerCase().replace(/\s+/g, '_');
  return REASON_LABEL[k] ?? REASON_LABEL[code] ?? REASON_LABEL.default;
}

export function RiskGate({ signals, dormant }: Props) {
  const rows = signals?.signals ?? [];
  const approved = rows.filter((s) => (s.verdict ?? '').toLowerCase() === 'approved');
  const rejected = rows.filter((s) => (s.verdict ?? '').toLowerCase() !== 'approved');

  return (
    <div className="flex flex-col h-full min-h-0 rounded-xl border border-white/5 bg-white/[0.02] p-2.5">
      <div className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">Risk gate</div>
      {dormant ? (
        <div className="text-xs text-zinc-600">System off</div>
      ) : (
        <ScrollArea className="flex-1 min-h-0">
          <div className="space-y-3 pr-2">
            <div>
              <div className="text-[10px] text-emerald-500/90 mb-1 uppercase">Approved</div>
              {approved.length === 0 ? (
                <div className="text-[11px] text-zinc-600">None recent</div>
              ) : (
                <ul className="space-y-2">
                  {approved.map((s) => (
                    <li key={s.id} className="text-[11px] border-b border-white/5 pb-2">
                      <div className="text-white/90">
                        {s.symbol}{' '}
                        <span className="text-zinc-500">{s.side}</span>
                      </div>
                      <div className="text-zinc-500 text-[10px]">
                        conf {(s.confidence * 100).toFixed(0)}%
                        {s.quality_score != null ? ` · q ${s.quality_score.toFixed(2)}` : ''}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <div className="text-[10px] text-rose-500/90 mb-1 uppercase">Rejected / blocked</div>
              {rejected.length === 0 ? (
                <div className="text-[11px] text-zinc-600">None recent</div>
              ) : (
                <ul className="space-y-2">
                  {rejected.map((s) => {
                    const raw = s.risk_reason || s.verdict || '';
                    const code = normalizeReason(raw) || raw;
                    const explain = humanizeReason(code);
                    return (
                      <li key={s.id} className="text-[11px] border-b border-white/5 pb-2">
                        <div className="text-white/90">
                          <span className="text-rose-200/90">Rejected:</span>{' '}
                          <span className="uppercase">{s.side}</span> {s.symbol}
                        </div>
                        <div className="text-zinc-300 text-[10px] mt-0.5 leading-snug">{explain}</div>
                        <div className="text-rose-300/70 text-[10px] font-mono">{raw}</div>
                        {s.checks_failed?.length ? (
                          <div className="text-zinc-500 text-[10px]">{s.checks_failed.join(', ')}</div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </div>
        </ScrollArea>
      )}
    </div>
  );
}
