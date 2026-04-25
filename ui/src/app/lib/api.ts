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
const CONTROL_TOKEN_LS_KEY = 'apiControlToken';

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

function readControlToken(): string | undefined {
  if (typeof localStorage !== 'undefined') {
    const ls = localStorage.getItem(CONTROL_TOKEN_LS_KEY);
    if (ls?.trim()) return ls.trim();
  }
  const env = import.meta.env.VITE_API_CONTROL_TOKEN;
  if (typeof env === 'string' && env.trim()) return env.trim();
  return undefined;
}

function mutationHeaders(): Record<string, string> {
  const headers = dashboardReadHeaders();
  const tok = readControlToken();
  if (tok) headers['X-Control-Token'] = tok;
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

export function setApiControlToken(token: string | null): void {
  try {
    if (token?.trim()) {
      localStorage.setItem(CONTROL_TOKEN_LS_KEY, token.trim());
    } else {
      localStorage.removeItem(CONTROL_TOKEN_LS_KEY);
    }
  } catch {
    /* ignore */
  }
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
  let firstReachable: string | null = null;
  for (const port of CANDIDATE_PORTS) {
    const candidate = `http://${host}:${port}`;
    try {
      const r = await fetch(`${candidate}/system/status`, {
        signal: AbortSignal.timeout(1500),
        headers: dashboardReadHeaders(),
      });
      if (!r.ok) continue;
      if (!firstReachable) firstReachable = candidate;
      const payload = await parseJsonBody<SystemStatusResponse>(r, '/system/status');
      const ib = payload?.brokers?.ibkr;
      const ibOk = !!(ib && ib.connected && ib.balance_ready !== false);
      if (ibOk) {
        _resolvedBase = candidate;
        console.log(`[api] connected to ${candidate} (ibkr ready)`);
        return _resolvedBase;
      }
    } catch {
      /* port not reachable, try next */
    }
  }
  if (firstReachable) {
    _resolvedBase = firstReachable;
    console.log(`[api] connected to ${firstReachable}`);
    return _resolvedBase;
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
    /** Signed position size (negative for short). Backend always populates
     *  this from PositionLog.quantity — the legacy "derive from pnl/delta"
     *  heuristic is a fallback for responses missing the field. */
    quantity?: string;
    unrealised_pnl?: string;
    avg_entry_price?: string;
    current_price?: string;
    broker?: string;
    asset_class?: string;
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
  runtime?: Record<string, unknown>;
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

/** GET /diagnostics/strategy-candidates — StrategyCandidateLog rollups (D033). */
export type StrategyMixRow = {
  name: string;
  evaluated: number;
  by_status: Record<string, number>;
  counts: {
    no_setup: number;
    generated: number;
    filtered_regime: number;
    filtered_signal_engine: number;
    filtered_meta: number;
    lost_to_strategy: number;
    selected_for_allocation: number;
    risk_rejected: number;
    executed: number;
    skipped: number;
    execution_incomplete?: number;
  };
  last_evaluated_at: string | null;
  last_generated_at: string | null;
  top_skip_reason: { reason: string; count: number } | null;
  /** Aggregated from ``no_setup`` / ``skipped`` row metadata (near-miss). */
  top_failed_conditions?: Array<{ key: string; count: number; label: string }>;
  top_risk_rejection_reasons?: Array<{ reason: string; count: number }>;
  top_execution_incomplete?: Array<{ reason: string; count: number }>;
  /** One-line: prefer risk, then execution, then no-setup. */
  blocker_hint?: string | null;
  lifecycle: string;
};

export type StrategyCandidateMixResponse = {
  since_hours: number;
  strategies: StrategyMixRow[];
  error?: string;
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
    ai_news_score?: number | null;
    accumulator_score?: number | null;
    news_impact_source?: 'headline' | 'ai_news' | 'accumulator' | 'signal' | 'none' | string | null;
    quality_score: number | null;
    volume_z: number | null;
    verdict: string;
    risk_reason: string;
    checks_failed: string[];
    news_attribution?: Array<{
      headline?: string | null;
      source?: string | null;
      score?: number | null;
      event_type?: string | null;
      scored_at?: string | null;
      match_mode?: string | null;
    }>;
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
  /** True when the loop wrote a minimal tick (no full allocator publish). */
  heartbeat_only?: boolean;
  dashboard_feed?: {
    reason?: string;
    message?: string;
    batch_candidate_count?: number;
    universe_symbol_count?: number;
    symbols_with_features?: number;
    symbols_feature_empty?: number;
  };
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
  demand?: {
    score?: number | string;
    trend?: string;
    confidence?: number | string;
    market_volatility?: number | string;
    cross_asset_coverage?: number | string;
    alert?: Record<string, unknown> | null;
    alert_history?: Array<Record<string, unknown>>;
  };
};

export type SystemStatusResponse = {
  state: SystemState;
  state_changed_at?: string;
  /** Last orchestrator start() exception message (survives errors.clear on retry). */
  last_start_error?: string | null;
  paper_mode?: boolean;
  active_brokers?: string[];
  brokers?: Record<
    string,
    { configured: boolean; connected: boolean; balance_ready?: boolean; error?: string | null }
  >;
  /** Portfolio coverage summary: is NAV reflecting every configured wallet?
   *  When ``full`` is false, ``excluded`` carries the failing brokers with
   *  their last known error so the UI can render partial-coverage states
   *  honestly instead of silently truncating NAV. */
  coverage?: {
    full: boolean;
    configured: string[];
    included: string[];
    excluded: Array<{
      name: string;
      connected: boolean;
      balance_ready: boolean;
      reason: string;
    }>;
  };
  infrastructure?: Record<string, { healthy: boolean; error?: string | null }>;
  trading?: {
    running: boolean;
    iterations?: number;
    last_iteration_at?: string | null;
    loop_interval_sec?: number;
    last_error?: string | null;
    /** ISO time of last `dashboard.snapshot` write (full or heartbeat); mirrors snapshot.updated_at when in sync. */
    snapshot_published_at?: string | null;
    ai?: {
      news_feed_stale?: boolean;
      latest_news_age_hours?: number | null;
      news_sources_in_scoring_window?: string[];
      news_source_stats?: Record<
        string,
        {
          fresh_rows_in_window?: number;
          latest_age_hours?: number | null;
          stale?: boolean;
        }
      >;
    };
  };
  errors?: string[];
  pipeline_running?: boolean;
  capital_pct?: number;
  /** Every strategy currently registered in the trading loop — signal & arbitrage.
   *  Used by the dashboard to show a complete roster (including idle strategies
   *  that have produced no recent opportunities). */
  loaded_strategies?: Array<{
    name: string;
    enabled: boolean;
    kind: 'signal' | 'arbitrage' | string;
  }>;
  /** News & macro API keys: ingest recency (``data/ingest_telemetry``). */
  news_data_providers?: Array<{
    id: string;
    label: string;
    configured: boolean;
    state: 'live' | 'stale' | 'never' | 'off' | 'error';
    last_ingest_at: string | null;
    age_label: string;
    ok?: boolean;
    error?: string | null;
  }>;
};

export type RoutingBrokerRow = {
  symbol: string;
  broker: string;
  learned_score: number;
  fused_score: number;
  fee_prior: number;
  ci95_half: number;
  n: number;
  p50_slippage_bps: number;
  p90_slippage_bps: number;
  fill_rate: number;
  exec_attempts: number;
  exec_fills: number;
};

export type RoutingQualityResponse = {
  updated_at?: string | null;
  quality_map?: Record<string, Record<string, number>>;
  quality_stats?: Record<
    string,
    Record<
      string,
      {
        n: number;
        std: number;
        ci95_half: number;
        turnover_ema?: number;
        liquidity_ema?: number;
        fee_prior?: number;
        fused_score?: number;
        p50_slippage_bps?: number;
        p90_slippage_bps?: number;
        fill_rate?: number;
        exec_attempts?: number;
        exec_fills?: number;
      }
    >
  >;
  history?: Record<string, Array<{ ts: string; broker: string; score: number }>>;
  broker_comparison?: RoutingBrokerRow[];
  exec_metrics?: Record<string, Record<string, { slips: number[]; attempts: number; fills: number }>>;
  runtime_summary?: Record<string, unknown>;
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
    headers: mutationHeaders(),
  });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return parseJsonBody<T>(r, path);
}

async function postJsonBody<T>(path: string, body: unknown): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...mutationHeaders() },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return parseJsonBody<T>(r, path);
}

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const base = await resolveApiBase();
  const r = await fetch(`${base}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...mutationHeaders() },
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
  getNews: (limit = 30, impactfulOnly = false) =>
    getJson<ApiNewsResponse>(`/news?limit=${limit}&impactful_only=${impactfulOnly ? 'true' : 'false'}`),
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
  getRoutingQuality: () => getJson<RoutingQualityResponse>('/diagnostics/routing-quality'),
  getStrategyCandidateMix: (sinceHours = 24) =>
    getJson<StrategyCandidateMixResponse>(`/diagnostics/strategy-candidates?since_hours=${sinceHours}`),
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
