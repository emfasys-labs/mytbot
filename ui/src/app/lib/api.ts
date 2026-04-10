const CONFIGURED_BASE = import.meta.env.VITE_API_BASE || '';
const CANDIDATE_PORTS = [8000, 8001, 8002, 8003, 8004];

let _resolvedBase: string | null = null;

async function resolveApiBase(): Promise<string> {
  if (_resolvedBase) return _resolvedBase;

  if (CONFIGURED_BASE) {
    _resolvedBase = CONFIGURED_BASE;
    return _resolvedBase;
  }

  const host = window.location.hostname || 'localhost';
  for (const port of CANDIDATE_PORTS) {
    const candidate = `http://${host}:${port}`;
    try {
      const r = await fetch(`${candidate}/status`, {
        signal: AbortSignal.timeout(1500),
      });
      if (r.ok) {
        _resolvedBase = candidate;
        console.log(`[api] connected to ${candidate}`);
        return _resolvedBase;
      }
    } catch {
      /* port not reachable, try next */
    }
  }

  _resolvedBase = `http://${host}:8000`;
  console.warn('[api] no reachable API found — falling back to :8000');
  return _resolvedBase;
}

function getBase(): string {
  return _resolvedBase ?? (CONFIGURED_BASE || `http://${window.location.hostname || 'localhost'}:8000`);
}

export type ApiPnlResponse = {
  today?: {
    realised?: string;
    unrealised?: string;
    fees?: string;
    trades?: number;
    portfolio_value?: string;
  };
};

export type ApiPnlHistoryResponse = {
  history?: Array<{
    date: string;
    portfolio_value?: string;
  }>;
};

export type ApiPositionsResponse = {
  positions?: Array<{
    symbol: string;
    unrealised_pnl?: string;
    avg_entry_price?: string;
    current_price?: string;
  }>;
};

export type ApiSignalsResponse = {
  signals?: Array<{
    symbol: string;
    side: string;
    strategy: string;
    timestamp?: string | null;
    metadata?: Record<string, unknown>;
  }>;
};

export type ApiStatusResponse = {
  kill_switch?: boolean;
  status?: string;
  system_state?: string;
  connected_brokers?: string[];
};

export type SystemState = 'off' | 'starting' | 'running' | 'stopping' | 'error';

export type SystemStatusResponse = {
  state: SystemState;
  state_changed_at?: string;
  paper_mode?: boolean;
  active_brokers?: string[];
  brokers?: Record<string, { configured: boolean; connected: boolean; error?: string | null }>;
  infrastructure?: Record<string, { healthy: boolean; error?: string | null }>;
  trading?: { running: boolean; iterations?: number; last_error?: string | null };
  errors?: string[];
  pipeline_running?: boolean;
};

async function getJson<T>(path: string): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`);
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return (await r.json()) as T;
}

async function postJson<T>(path: string): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, { method: 'POST' });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return (await r.json()) as T;
}

export const api = {
  get base() {
    return getBase();
  },
  init: resolveApiBase,
  getPnl: () => getJson<ApiPnlResponse>('/pnl'),
  getPnlHistory: (limit = 90) => getJson<ApiPnlHistoryResponse>(`/pnl/history?limit=${limit}`),
  getPositions: (limit = 20) => getJson<ApiPositionsResponse>(`/positions?limit=${limit}`),
  getSignals: (limit = 20) => getJson<ApiSignalsResponse>(`/signals?limit=${limit}`),
  getStatus: () => getJson<ApiStatusResponse>('/status'),
  getSystemStatus: () => getJson<SystemStatusResponse>('/system/status'),
  systemStart: () => postJson<SystemStatusResponse>('/system/start'),
  systemStop: () => postJson<SystemStatusResponse>('/system/stop'),
};

export function toNumber(v: unknown, fallback = 0): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number.parseFloat(v);
    if (Number.isFinite(n)) return n;
  }
  return fallback;
}
