import { useEffect, useRef, useState } from 'react';

export interface TickerItem {
  text: string;
  source?: string;
  time?: string;
  sentiment?: 'positive' | 'negative' | 'neutral';
}

interface NewsTickerProps {
  items: TickerItem[];
  paused?: boolean;
}

function formatAge(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.round(ms / 60_000);
  if (mins < 1) return 'now';
  if (mins < 60) return `${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.round(hrs / 24)}d`;
}

export function NewsTicker({ items, paused = false }: NewsTickerProps) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [offset, setOffset] = useState(0);
  const speed = 0.5; // px per frame

  const hasItems = items.length > 0;

  useEffect(() => {
    if (!hasItems || paused) return;
    let raf: number;
    let lastTs = 0;

    const step = (ts: number) => {
      if (lastTs) {
        const dt = Math.min(ts - lastTs, 50);
        setOffset((prev) => {
          const el = trackRef.current;
          if (!el) return prev;
          const half = el.scrollWidth / 2;
          if (half <= 0) return 0;
          const next = prev + speed * (dt / 16.67);
          return next >= half ? next - half : next;
        });
      }
      lastTs = ts;
      raf = requestAnimationFrame(step);
    };

    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [hasItems, paused]);

  if (!hasItems) {
    return (
      <div className="w-full overflow-hidden border-b border-white/[0.04] bg-white/[0.015] py-2.5">
        <div className="text-center text-[11px] font-light text-gray-600/60 tracking-wide">
          Awaiting market intelligence...
        </div>
      </div>
    );
  }

  const renderItem = (item: TickerItem, i: number) => {
    const dotColor =
      item.sentiment === 'positive'
        ? 'bg-emerald-400'
        : item.sentiment === 'negative'
          ? 'bg-rose-400'
          : 'bg-gray-500';

    return (
      <span key={i} className="inline-flex items-center gap-2 shrink-0">
        <span className={`inline-block h-[5px] w-[5px] rounded-full ${dotColor} opacity-60`} />
        {item.source && (
          <span className="text-[10px] font-semibold uppercase tracking-wider text-white/30">
            {item.source}
          </span>
        )}
        <span className="text-[11px] font-light text-gray-400/90">
          {item.text}
        </span>
        {item.time && (
          <span className="text-[9px] text-gray-600 tabular-nums">
            {formatAge(item.time)}
          </span>
        )}
        <span className="text-gray-700/40 mx-2">·</span>
      </span>
    );
  };

  const doubled = [...items, ...items];

  return (
    <div className="w-full overflow-hidden border-b border-white/[0.04] bg-white/[0.015] py-2.5">
      <div
        ref={trackRef}
        className="flex whitespace-nowrap will-change-transform"
        style={{ transform: `translateX(-${offset}px)` }}
      >
        {doubled.map(renderItem)}
      </div>
    </div>
  );
}
