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
    /** Total equity × capital_allocation_pct — order sizing budget, not a second balance. */
    tradable_capital?: string;
    capital_allocation_pct?: number;
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

export type NewsHeadline = {
  title: string;
  source: string;
  published_at: string | null;
  url: string;
  description?: string | null;
};

export type NewsAiScore = {
  symbol: string;
  score: string | null;
  confidence: string | null;
  event_type: string | null;
  rationale: string | null;
  scored_at: string | null;
};

export type ApiNewsResponse = {
  headlines?: NewsHeadline[];
  ai_scores?: NewsAiScore[];
};

export type DiscoverySummaryResponse = {
  universe?: {
    broker_totals?: Record<string, number>;
    total_broker_instruments?: number;
    core?: number;
    scan?: number;
    light?: number;
    total_tiered?: number;
    tiers_updated_at?: string | null;
  };
  last_24h?: {
    anomalies_detected?: number;
    theses_generated?: number;
    signals_produced?: number;
    symbols_analysed?: number;
  };
};

export type IntelligenceRegimeResponse = {
  regime?: {
    label?: string;
    confidence?: number;
    rationale?: string;
    updated_at?: string | null;
  };
  top_movers?: Array<{
    symbol: string;
    score: number;
    event_type: string;
    rationale: string;
    scored_at: string | null;
  }>;
};

export type IntelligenceSignalsResponse = {
  signals?: Array<{
    id: string;
    timestamp: string | null;
    symbol: string;
    side: string;
    strategy: string;
    confidence: number;
    asset_class: string;
    news_score: number | null;
    quality_score: number | null;
    volume_z: number | null;
    verdict: string;
    risk_reason: string;
    checks_failed: string[];
  }>;
};

export type DiscoveryAnomaliesResponse = {
  anomalies?: Array<{
    id: number;
    timestamp: string | null;
    symbol: string;
    direction: string;
    price_move_pct: string;
    price_z_score: string;
    anomaly_score: string;
    thesis_generated: boolean;
    signals_produced: number | null;
  }>;
};

export type TradingMode = 'defender' | 'trader' | 'hunter';

export type SystemModeResponse = {
  mode: TradingMode;
  label: string;
  description: string;
  applied?: Record<string, unknown>;
  live_engine_updated?: boolean;
};

export type SystemState = 'off' | 'starting' | 'running' | 'stopping' | 'error';

export type SystemStatusResponse = {
  state: SystemState;
  state_changed_at?: string;
  paper_mode?: boolean;
  active_brokers?: string[];
  brokers?: Record<
    string,
    { configured: boolean; connected: boolean; balance_ready?: boolean; error?: string | null }
  >;
  infrastructure?: Record<string, { healthy: boolean; error?: string | null }>;
  trading?: { running: boolean; iterations?: number; last_error?: string | null };
  errors?: string[];
  pipeline_running?: boolean;
  capital_pct?: number;
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

async function postJsonBody<T>(path: string, body: unknown): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return (await r.json()) as T;
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
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
  getNews: (limit = 30) => getJson<ApiNewsResponse>(`/news?limit=${limit}`),
  getStatus: () => getJson<ApiStatusResponse>('/status'),
  getSystemStatus: () => getJson<SystemStatusResponse>('/system/status'),
  systemStart: () => postJson<SystemStatusResponse>('/system/start'),
  systemStop: () => postJson<SystemStatusResponse>('/system/stop'),
  setCapitalAllocation: (pct: number) =>
    putJson<{ capital_pct: number }>('/system/capital-allocation', { pct }),
  getSystemMode: () => getJson<SystemModeResponse>('/system/mode'),
  setSystemMode: (mode: TradingMode) =>
    postJsonBody<SystemModeResponse>('/system/mode', { mode }),
  getDiscoverySummary: () => getJson<DiscoverySummaryResponse>('/discovery/summary'),
  getDiscoveryAnomalies: (limit = 8) => getJson<DiscoveryAnomaliesResponse>(`/discovery/anomalies?limit=${limit}`),
  getIntelligenceRegime: () => getJson<IntelligenceRegimeResponse>('/intelligence/regime'),
  getIntelligenceSignals: (limit = 8) => getJson<IntelligenceSignalsResponse>(`/intelligence/signals?limit=${limit}`),
};

export function toNumber(v: unknown, fallback = 0): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number.parseFloat(v);
    if (Number.isFinite(n)) return n;
  }
  return fallback;
}
