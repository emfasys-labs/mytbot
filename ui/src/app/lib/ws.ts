import { api } from './api';
import { prettySymbol } from './symbol';

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
    const rawSym = String(p.symbol ?? '');
    if (!rawSym) return null;
    const sym = prettySymbol(rawSym);
    const side = String(p.side ?? '').toLowerCase();
    const strat = String(p.strategy ?? '');
    return `${sym} ${side} · ${strat || 'signal'}`;
  }
  if (ev.type === 'order_filled') {
    const rawSym = String(p.symbol ?? '');
    if (!rawSym) return null;
    const sym = prettySymbol(rawSym);
    const st = String(p.status ?? '');
    return `${sym} filled · ${st || 'filled'}`;
  }
  if (ev.type === 'kill_activated') {
    return `Kill switch · activated${p.killed != null ? ` (${p.killed ? 'on' : 'off'})` : ''}`;
  }
  if (ev.type === 'kill_reset') {
    return `Kill switch · reset${p.killed != null ? ` (${p.killed ? 'on' : 'off'})` : ''}`;
  }
  if (ev.type === 'command_completed') {
    const t = String((p as { type?: string }).type ?? '');
    const param = (p as { parameter?: string }).parameter;
    if (param) return `Control · set ${param}`;
    return t ? `Control · ${t.replace(/_/g, ' ')}` : 'Control · completed';
  }
  if (ev.type === 'command_failed') {
    const err = String((p as { error?: string }).error ?? '').slice(0, 120);
    return err ? `Control · failed · ${err}` : 'Control · failed';
  }
  const raw = ev.type?.trim();
  if (raw) return `Bus · ${raw.replace(/_/g, ' ')}`;
  return null;
}

export function eventTimestamp(ev: WsTickEvent): string | null {
  if (ev.ts && typeof ev.ts === 'string') return ev.ts;
  const p = ev.payload;
  if (p && typeof p.timestamp === 'string') return p.timestamp;
  return null;
}
