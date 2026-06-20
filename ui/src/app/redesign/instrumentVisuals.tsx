import type { CSSProperties } from 'react';
import { prettySymbol } from './mapping';
import { TOKENS } from './tokens';

export interface InstrumentVisual {
  sym: string;
  name?: string | null;
  description?: string | null;
  category?: string | null;
  logoUrl?: string | null;
  logoKind?: string | null;
  assetClass?: string | null;
  exchange?: string | null;
  currency?: string | null;
}

const KIND_COLORS: Record<string, [string, string]> = {
  equity: ['#1f2937', '#64748b'],
  etf: ['#0f766e', '#22c55e'],
  fund: ['#0f766e', '#22c55e'],
  forex: ['#1d4ed8', '#38bdf8'],
  crypto: ['#6d28d9', '#f59e0b'],
  commodity: ['#92400e', '#facc15'],
  future: ['#7c2d12', '#fb923c'],
  index: ['#312e81', '#818cf8'],
};

function cleanKind(kind?: string | null, assetClass?: string | null): string {
  const raw = String(kind || assetClass || 'equity').toLowerCase();
  if (raw.includes('forex') || raw === 'fx') return 'forex';
  if (raw.includes('crypto')) return 'crypto';
  if (raw.includes('commodity')) return 'commodity';
  if (raw.includes('future')) return 'future';
  if (raw.includes('index')) return 'index';
  if (raw.includes('etf') || raw.includes('fund')) return 'fund';
  return raw || 'equity';
}

function initials(pos: InstrumentVisual): string {
  const name = (pos.name || prettySymbol(pos.sym) || pos.sym).trim();
  const words = name.split(/[\s/.-]+/).filter(Boolean);
  if (words.length >= 2) return `${words[0][0]}${words[1][0]}`.toUpperCase();
  return name.slice(0, Math.min(3, name.length)).toUpperCase();
}

export function instrumentDisplayName(pos: InstrumentVisual): string {
  return pos.name?.trim() || prettySymbol(pos.sym);
}

export function instrumentSubtitle(pos: InstrumentVisual): string {
  const parts = [
    pos.sym,
    pos.category,
    pos.exchange,
    pos.currency,
  ].filter((v, i, arr) => {
    const s = String(v || '').trim();
    return s && arr.findIndex((x) => String(x || '').trim().toLowerCase() === s.toLowerCase()) === i;
  });
  return parts.slice(0, 3).join(' · ');
}

export function InstrumentAvatar({
  pos,
  size = 34,
  style,
}: {
  pos: InstrumentVisual;
  size?: number;
  style?: CSSProperties;
}) {
  const kind = cleanKind(pos.logoKind, pos.assetClass);
  const [from, to] = KIND_COLORS[kind] || KIND_COLORS.equity;
  const label = initials(pos);
  return (
    <div
      title={instrumentDisplayName(pos)}
      style={{
        width: size,
        height: size,
        borderRadius: Math.max(7, Math.round(size * 0.24)),
        flexShrink: 0,
        overflow: 'hidden',
        display: 'grid',
        placeItems: 'center',
        background: `linear-gradient(135deg, ${from}, ${to})`,
        border: `1px solid rgba(255,255,255,0.16)`,
        boxShadow: 'inset 0 0 0 1px rgba(0,0,0,0.16)',
        color: TOKENS.ink0,
        fontFamily: TOKENS.sans,
        fontWeight: 700,
        fontSize: Math.max(9, Math.round(size * 0.28)),
        lineHeight: 1,
        ...style,
      }}
    >
      <span style={{ gridArea: '1 / 1' }}>{label}</span>
      {pos.logoUrl ? (
        <img
          src={pos.logoUrl}
          alt=""
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block', gridArea: '1 / 1' }}
          onError={(event) => {
            event.currentTarget.style.display = 'none';
          }}
        />
      ) : null}
    </div>
  );
}
