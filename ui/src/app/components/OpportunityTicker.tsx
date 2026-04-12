import { useEffect, useRef, useState } from 'react';
import type { IntelligenceSignalsResponse, IntelligenceRegimeResponse } from '../lib/api';
import { dedupeIntelligenceSignals } from '../lib/intelligenceSignals';

interface TickerItem {
  key: string;
  symbol: string;
  side: 'buy' | 'sell';
  strategy: string;
  confidence: number;
  newsScore: number | null;
  qualityScore: number | null;
  verdict: string;
  timestamp: string | null;
}

interface OpportunityTickerProps {
  signals: IntelligenceSignalsResponse | null;
  regime: IntelligenceRegimeResponse | null;
  /** Ranked watchlist (conviction → signals → positions). When set, replaces symbol soup with decision-relevant order. */
  watchlist?: Array<{ symbol: string; score: number; note: string }>;
}

function buildItems(signals: IntelligenceSignalsResponse | null): TickerItem[] {
  const rows = dedupeIntelligenceSignals(signals?.signals, 12);
  if (!rows.length) return [];
  return rows.map((s) => ({
    key: s.id,
    symbol: s.symbol,
    side: s.side as 'buy' | 'sell',
    strategy: s.strategy,
    confidence: s.confidence,
    newsScore: s.news_score,
    qualityScore: s.quality_score ?? null,
    verdict: s.verdict,
    timestamp: s.timestamp,
  }));
}

function formatRankScore(s: number): string {
  if (!Number.isFinite(s)) return '—';
  if (Math.abs(s) <= 1) return `${s >= 0 ? '+' : ''}${s.toFixed(2)}`;
  return s.toFixed(2);
}

function TickerCell({ item }: { item: TickerItem }) {
  const approved = item.verdict === 'approved';
  const isUp = item.side === 'buy';
  const age = item.timestamp
    ? (() => {
        const diff = Math.round((Date.now() - new Date(item.timestamp).getTime()) / 60000);
        return diff < 1 ? 'now' : diff < 60 ? `${diff}m ago` : `${Math.round(diff / 60)}h ago`;
      })()
    : '';
  return (
    <span className="inline-flex items-center gap-1.5 px-4 text-[11px] whitespace-nowrap select-none">
      <span className={`font-semibold ${isUp ? 'text-emerald-400' : 'text-rose-400'}`}>
        {isUp ? '▲' : '▼'}
      </span>
      <span className="text-gray-200 font-medium">{item.symbol}</span>
      <span className="text-gray-500">·</span>
      <span className="text-gray-500 capitalize">{item.strategy.replace(/_/g, ' ')}</span>
      <span className="text-gray-500">·</span>
      <span className="text-gray-400 tabular-nums">conf {(item.confidence * 100).toFixed(0)}%</span>
      <span className="text-gray-500">·</span>
      <span className={`font-medium ${approved ? 'text-emerald-400/70' : 'text-rose-400/70'}`}>
        {approved ? '✓ approved' : '✕ vetoed'}
      </span>
      {item.qualityScore !== null && (
        <span className="text-gray-600 tabular-nums">
          Q{Math.round(item.qualityScore * 100)}
        </span>
      )}
      {age && <span className="text-gray-700">{age}</span>}
      <span className="text-gray-700 ml-3">·</span>
    </span>
  );
}

export function OpportunityTicker({ signals, regime, watchlist }: OpportunityTickerProps) {
  const items = buildItems(signals);
  const regimeLabel = regime?.regime?.label;
  const trackRef = useRef<HTMLDivElement>(null);
  const [translateX, setTranslateX] = useState(0);
  const animRef = useRef<number>(0);
  const startRef = useRef<number | null>(null);
  const speedPx = 40;

  useEffect(() => {
    if (watchlist?.length) return;
    if (!items.length) return;
    const track = trackRef.current;
    if (!track) return;

    const tick = (ts: number) => {
      if (startRef.current === null) startRef.current = ts;
      const elapsed = (ts - startRef.current) / 1000;
      const width = track.scrollWidth / 2;
      const x = (elapsed * speedPx) % width;
      setTranslateX(-x);
      animRef.current = requestAnimationFrame(tick);
    };
    animRef.current = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(animRef.current);
      startRef.current = null;
    };
  }, [items.length, watchlist?.length]);

  if (watchlist && watchlist.length > 0) {
    return (
      <div className="border-t border-white/5 py-2 px-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
        <span className="text-[10px] uppercase tracking-widest text-zinc-500 shrink-0">Watchlist (ranked)</span>
        <div className="flex flex-wrap gap-2 min-w-0">
          {watchlist.map((w, i) => (
            <span
              key={`${w.symbol}-${i}`}
              className="inline-flex items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.03] px-2 py-0.5 tabular-nums"
            >
              <span className="text-zinc-500 w-4 text-right">{i + 1}</span>
              <span className="text-zinc-100 font-medium">{w.symbol}</span>
              <span className="text-emerald-300/90">{formatRankScore(w.score)}</span>
              <span className="text-zinc-600 text-[10px] hidden sm:inline">{w.note}</span>
            </span>
          ))}
        </div>
      </div>
    );
  }

  if (!items.length) {
    return (
      <div className="border-t border-white/5 py-2 px-4 text-[11px] text-zinc-600 text-center tracking-wide">
        {regimeLabel ? `Macro: ${regimeLabel} · ` : ''}No ranked watchlist — waiting for conviction or signals
      </div>
    );
  }

  const doubled = [...items, ...items];

  return (
    <div className="border-t border-white/5 overflow-hidden" style={{ height: 28 }}>
      <div
        ref={trackRef}
        className="flex items-center h-full"
        style={{ transform: `translateX(${translateX}px)`, willChange: 'transform' }}
      >
        {doubled.map((item, i) => (
          <TickerCell key={`${item.key}-${i}`} item={item} />
        ))}
      </div>
    </div>
  );
}
