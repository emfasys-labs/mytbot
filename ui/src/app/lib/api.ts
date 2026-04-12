const CANDIDATE_PORTS = [8000, 8001, 8002, 8003, 8004];

/** Must be absolute `http(s)://host:port` — relative values break fetches (browser loads SPA HTML → JSON parse error). */
function getSafeConfiguredBase(): string {
  const raw = (import.meta.env.VITE_API_BASE || '').trim();
  if (!raw) return '';
  if (/^https?:\/\//i.test(raw)) return raw.replace(/\/$/, '');
  console.warn(
    `[api] VITE_API_BASE must be a full URL (e.g. http://127.0.0.1:8000). Ignoring invalid value: ${raw}`,
  );
  return '';
}

async function parseJsonBody<T>(r: Response, path: string): Promise<T> {
  const text = await r.text();
  const trimmed = text.trim();
  if (!trimmed) {
    throw new Error(`${path}: empty response`);
  }
  if (trimmed.startsWith('<!') || trimmed.toLowerCase().startsWith('<html')) {
    throw new Error(
      `${path}: API returned HTML, not JSON — usually the wrong base URL (UI talked to the static/Vite server). ` +
        `Set VITE_API_BASE=http://127.0.0.1:PORT in ui/.env.local (same port as FastAPI) and restart Vite.`,
    );
  }
  try {
    return JSON.parse(trimmed) as T;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    throw new Error(`${path}: invalid JSON (${msg})`);
  }
}

/** Same token source as `ws.ts` — required when API sets `DASHBOARD_READ_TOKEN`. */
const DASHBOARD_TOKEN_LS_KEY = 'dashboardReadToken';

/** Prefer localStorage so the banner paste wins over a baked-in VITE_DASHBOARD_READ_TOKEN. */
function readDashboardToken(): string | undefined {
  if (typeof localStorage !== 'undefined') {
    const ls = localStorage.getItem(DASHBOARD_TOKEN_LS_KEY);
    if (ls?.trim()) return ls.trim();
  }
  const env = import.meta.env.VITE_DASHBOARD_READ_TOKEN;
  if (typeof env === 'string' && env.trim()) return env.trim();
  return undefined;
}

function dashboardReadHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const tok = readDashboardToken();
  if (tok) headers['X-Dashboard-Token'] = tok;
  return headers;
}

/** Persist read token (same value as server `DASHBOARD_READ_TOKEN`) and clear API base cache so the next probe uses the header. */
export function setDashboardReadToken(token: string | null): void {
  try {
    if (token?.trim()) {
      localStorage.setItem(DASHBOARD_TOKEN_LS_KEY, token.trim());
    } else {
      localStorage.removeItem(DASHBOARD_TOKEN_LS_KEY);
    }
  } catch {
    /* ignore */
  }
  resetApiBaseCache();
}

export function resetApiBaseCache(): void {
  _resolvedBase = null;
}

let _resolvedBase: string | null = null;

async function resolveApiBase(): Promise<string> {
  if (_resolvedBase) return _resolvedBase;

  const cfg = getSafeConfiguredBase();
  if (cfg) {
    _resolvedBase = cfg;
    return _resolvedBase;
  }

  const host = window.location.hostname || 'localhost';
  for (const port of CANDIDATE_PORTS) {
    const candidate = `http://${host}:${port}`;
    try {
      const r = await fetch(`${candidate}/status`, {
        signal: AbortSignal.timeout(1500),
        headers: dashboardReadHeaders(),
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
  const cfg = getSafeConfiguredBase();
  return _resolvedBase ?? (cfg || `http://${window.location.hostname || 'localhost'}:8000`);
}

export type PnlPeriodRollup = {
  realised?: string;
  unrealised?: string;
  fees?: string;
  trades?: number;
  period_start?: string;
  period_end?: string;
};

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
  week?: PnlPeriodRollup;
  month?: PnlPeriodRollup;
  metrics?: {
    win_rate_days?: number | null;
    max_drawdown_pct?: number | null;
  };
};

export type ApiPnlHistoryResponse = {
  history?: Array<{
    date: string;
    portfolio_value?: string;
  }>;
};

export type ApiOrderRow = {
  id?: number;
  timestamp?: string | null;
  symbol?: string;
  side?: string;
  status?: string;
  filled_quantity?: string | null;
  avg_fill_price?: string | null;
  broker?: string | null;
};

export type ApiOrdersResponse = {
  orders?: ApiOrderRow[];
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

/** GET /dashboard/snapshot — persisted trading-loop decision state. */
export type DashboardSnapshot = {
  updated_at?: string;
  fingerprint?: string;
  path?: string;
  loop_iteration?: number;
  accumulator?: {
    updated_at?: string;
    bullish_top?: Array<Record<string, unknown>>;
    bearish_top?: Array<Record<string, unknown>>;
    top_by_magnitude?: Array<Record<string, unknown>>;
  };
  regime?: {
    regime_label?: string;
    market_state_score?: string;
    components?: Record<string, string>;
    timestamp?: string;
  } | null;
  opportunities?: Array<Record<string, unknown>>;
  allocation?: Record<string, unknown> | null;
  execution_plan?: {
    instructions?: Array<Record<string, unknown>>;
    estimated_turnover?: string;
    rationale?: string;
    timestamp?: string;
  } | null;
  portfolio?: Record<string, unknown>;
  global_edge?: Record<string, unknown>;
};

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
  const r = await fetch(`${base}${path}`, { headers: dashboardReadHeaders() });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return parseJsonBody<T>(r, path);
}

async function postJson<T>(path: string): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: dashboardReadHeaders(),
  });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return parseJsonBody<T>(r, path);
}

async function postJsonBody<T>(path: string, body: unknown): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...dashboardReadHeaders() },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return parseJsonBody<T>(r, path);
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...dashboardReadHeaders() },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return parseJsonBody<T>(r, path);
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
  getDashboardSnapshot: () => getJson<DashboardSnapshot>('/dashboard/snapshot'),
  getOrders: (limit = 40) => getJson<ApiOrdersResponse>(`/orders?limit=${limit}`),
};

export function toNumber(v: unknown, fallback = 0): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string') {
    const n = Number.parseFloat(v);
    if (Number.isFinite(n)) return n;
  }
  return fallback;
}
