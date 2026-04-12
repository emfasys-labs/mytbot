import { api } from './api';

const DASHBOARD_TOKEN_KEY = 'dashboardReadToken';

export type WsTickEvent = {
  type: string;
  payload?: Record<string, unknown>;
  ts?: string;
};

export type WsDashboardHint = {
  updated_at?: string;
  fingerprint?: string;
  path?: string;
  loop_iteration?: number;
};

export type WsTickPayload = {
  status?: { kill_switch?: boolean; system_state?: string };
  system?: { state?: string; active_brokers?: string[]; errors?: string[] };
  events?: WsTickEvent[];
  dashboard?: WsDashboardHint | null;
};

export type WsTickMessage = {
  type: 'tick';
  payload: WsTickPayload;
  ts?: string;
};

function readDashboardToken(): string | null {
  if (typeof localStorage !== 'undefined') {
    const ls = localStorage.getItem(DASHBOARD_TOKEN_KEY);
    if (ls?.trim()) return ls.trim();
  }
  const env = import.meta.env.VITE_DASHBOARD_READ_TOKEN;
  if (typeof env === 'string' && env.trim()) return env.trim();
  return null;
}

/** Same contract as `dashboard/src/api.js` wsUrl(). */
export async function getWsUrl(): Promise<string> {
  const httpBase = await api.init();
  const base = httpBase.replace(/^http/, 'ws');
  const tok = readDashboardToken();
  const q = tok ? `?token=${encodeURIComponent(tok)}` : '';
  return `${base}/ws${q}`;
}

export function formatWsEventLine(ev: WsTickEvent): string | null {
  const p = ev.payload ?? {};
  if (ev.type === 'signal_generated') {
    const sym = String(p.symbol ?? '');
    const side = String(p.side ?? '').toUpperCase();
    const strat = String(p.strategy ?? '');
    if (!sym) return null;
    return `${sym} ${side} · ${strat || 'signal'}`;
  }
  if (ev.type === 'order_filled') {
    const sym = String(p.symbol ?? '');
    const st = String(p.status ?? '');
    if (!sym) return null;
    return `${sym} filled · ${st || 'filled'}`;
  }
  return null;
}

export function eventTimestamp(ev: WsTickEvent): string | null {
  if (ev.ts && typeof ev.ts === 'string') return ev.ts;
  const p = ev.payload;
  if (p && typeof p.timestamp === 'string') return p.timestamp;
  return null;
}
