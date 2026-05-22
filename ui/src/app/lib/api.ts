const CANDIDATE_PORTS = [8000, 8001, 8002, 8003, 8004];

/** Default rows for `GET /positions` — must cover the full book; low limits slice alphabetically and skew weights. */
export const POSITIONS_POLL_LIMIT = 200;

/** Default FastAPI origin for `npm run dev` when `.env*` did not load `VITE_API_BASE`. */
const DEV_DEFAULT_API_BASE = 'http://127.0.0.1:8000';

/** Must be absolute `http(s)://host:port` — relative values break fetches (browser loads SPA HTML → JSON parse error). */
function getSafeConfiguredBase(): string {
  const raw = (import.meta.env.VITE_API_BASE || '').trim();
  if (/^https?:\/\//i.test(raw)) return raw.replace(/\/$/, '');
  if (raw) {
    console.warn(
      `[api] VITE_API_BASE must be a full URL (e.g. http://127.0.0.1:8000). Ignoring invalid value: ${raw}`,
    );
  }
  if (import.meta.env.DEV) {
    return DEV_DEFAULT_API_BASE;
  }
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

/** `/healthz` is exempt from `DASHBOARD_READ_TOKEN` — use for discovery so probing works before the banner token is pasted. */
type HealthzPayload = { ok?: boolean; service?: string };

function isMytbotHealthz(data: unknown): data is HealthzPayload {
  if (!data || typeof data !== 'object') return false;
  const d = data as HealthzPayload;
  return d.ok === true && d.service === 'api';
}

async function verifyMytbotApiBase(base: string): Promise<boolean> {
  const root = base.replace(/\/$/, '');
  try {
    const r = await fetch(`${root}/healthz`, { signal: AbortSignal.timeout(2000) });
    if (!r.ok) return false;
    const ct = (r.headers.get('content-type') || '').toLowerCase();
    if (ct && !ct.includes('json')) return false;
    const payload = await parseJsonBody<HealthzPayload>(r, '/healthz');
    return isMytbotHealthz(payload);
  } catch {
    return false;
  }
}

async function resolveApiBase(): Promise<string> {
  if (_resolvedBase) return _resolvedBase;

  const cfg = getSafeConfiguredBase();
  if (cfg && (await verifyMytbotApiBase(cfg))) {
    _resolvedBase = cfg;
    console.log(`[api] using configured API ${cfg} (healthz ok)`);
    return _resolvedBase;
  }
  if (cfg) {
    console.warn(`[api] ${cfg} is not responding as mytbot API — probing common ports`);
  }

  const hosts: string[] = [];
  const host = window.location.hostname || 'localhost';
  hosts.push(host);
  if (host !== '127.0.0.1') hosts.push('127.0.0.1');
  if (host !== 'localhost') hosts.push('localhost');

  let firstReachable: string | null = null;
  let ibkrPreferred: string | null = null;

  for (const h of hosts) {
    for (const port of CANDIDATE_PORTS) {
      const candidate = `http://${h}:${port}`;
      try {
        const r = await fetch(`${candidate}/healthz`, { signal: AbortSignal.timeout(1500) });
        if (!r.ok) continue;
        const ct = (r.headers.get('content-type') || '').toLowerCase();
        if (ct && !ct.includes('json')) continue;
        let hz: HealthzPayload;
        try {
          hz = await parseJsonBody<HealthzPayload>(r, '/healthz');
        } catch {
          continue;
        }
        if (!isMytbotHealthz(hz)) continue;
        if (!firstReachable) firstReachable = candidate;

        try {
          const sr = await fetch(`${candidate}/system/status`, {
            signal: AbortSignal.timeout(1500),
            headers: dashboardReadHeaders(),
          });
          if (sr.ok) {
            const ct2 = (sr.headers.get('content-type') || '').toLowerCase();
            if (!ct2 || ct2.includes('json')) {
              try {
                const sys = await parseJsonBody<SystemStatusResponse>(sr, '/system/status');
                const ib = sys?.brokers?.ibkr;
                if (ib && ib.connected && ib.balance_ready !== false) {
                  ibkrPreferred = candidate;
                }
              } catch {
                /* token may be required — healthz already validated API */
              }
            }
          }
        } catch {
          /* ignore */
        }
        if (ibkrPreferred) {
          _resolvedBase = ibkrPreferred;
          console.log(`[api] connected to ${ibkrPreferred} (ibkr ready)`);
          return _resolvedBase;
        }
      } catch {
        /* try next */
      }
    }
  }

  if (firstReachable) {
    _resolvedBase = firstReachable;
    console.log(`[api] connected to ${firstReachable} (healthz)`);
    return _resolvedBase;
  }

  const fallback = cfg || `http://${host}:8000`;
  _resolvedBase = fallback;
  console.warn(`[api] no healthz match — falling back to ${fallback}`);
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
  partial_coverage?: boolean;
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
    nav_status?: {
      complete?: boolean;
      coverage_full?: boolean;
      included?: string[];
      missing?: string[];
      configured?: string[];
      excluded?: Array<{ name?: string; connected?: boolean; balance_ready?: boolean; reason?: string }>;
    };
  };
  week?: PnlPeriodRollup;
  month?: PnlPeriodRollup;
  all_time?: PnlPeriodRollup;
  metrics?: {
    win_rate_days?: number | null;
    max_drawdown_pct?: number | null;
  };
};

export type ApiPnlHistoryResponse = {
  history?: Array<{
    date: string;
    realised?: string;
    unrealised?: string;
    fees?: string;
    trades?: number;
    portfolio_value?: string;
  }>;
};

/** Order-derived daily realised P&L series — source for the cumulative-realised graph. */
export type ApiRealisedCurveResponse = {
  as_of?: string;
  start?: string;
  end?: string;
  series?: Array<{
    /** UTC day, "YYYY-MM-DD". */
    date: string;
    /** Realised P&L booked that day (closed-trade gross minus fees). */
    realised?: string;
    /** Running total across the full returned window. */
    cumulative?: string;
    trades?: number;
  }>;
};

export type ApiOrderRow = {
  id?: string | number;
  timestamp?: string | null;
  symbol?: string;
  side?: string;
  status?: string;
  quantity?: string | null;
  limit_price?: string | null;
  filled_quantity?: string | null;
  avg_fill_price?: string | null;
  fee?: string | null;
  closes_position?: boolean;
  trade_pnl?: string | null;
  trade_pnl_net?: string | null;
  realised_pnl?: string | null;
  realised_pnl_net?: string | null;
  realised_pnl_gross?: string | null;
  realised_pnl_fee?: string | null;
  // Per-order transaction fee broken out so the UI can label opening trades
  // as "open · fee X.XX" instead of conflating fee with realised P&L.
  trade_fee_net?: string | null;
  closed_quantity?: string | null;
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

export type UniverseFunnelStage = {
  stage: string;
  count: number;
  fresh?: boolean;
  drops?: Array<{ reason: string; count: number }> | null;
  // D118 — opaque per-stage payload (e.g. budget meter info on
  // ``priority_ranked``, ``broker_listings`` debug count on
  // ``unique_normalized``).
  meta?: Record<string, unknown> | null;
};

export type UniverseSymbolRow = {
  sym: string;
  /** Human name when listed in `data/universe.py` curated catalogue; otherwise omitted. */
  name?: string | null;
  /** Short line for UI: name · class · sector, or ticker · class when unknown. */
  description?: string | null;
  klass: string;
  stage: string;
  conviction: number;
  trend?: string;
  sector?: string;
  factors?: Record<string, number>;
  spread?: number;
  spark?: number[];
  bookCorr?: number;
  tierReason?: string | null;
  pairWatch?: boolean;
  override?: { kind?: string; reason?: string } | null;
  // D118 — per-symbol score-age telemetry attached by the snapshot
  // service. ``last_scored_at`` is ISO-8601 UTC; ``last_score`` is the
  // most recent yfinance liquidity score; ``score_count`` is the
  // lifetime number of successful scoring events for this symbol.
  last_scored_at?: string | null;
  last_score?: number | null;
  score_count?: number | null;
  // D118 — priority pre-filter breakdown (component subscores +
  // weighted total). Present only when this symbol was picked by
  // the priority pre-filter in the most recent cycle.
  priority_breakdown?: {
    priority_score: number;
    components: Record<string, number>;
  } | null;
};

// D118 — self-tuning priority pre-filter telemetry. Carries the live
// learned weights, recent weight history (sparkline), budget controller
// state with binding-constraint label, and aggregated score-age
// summary. Returned by the snapshot service after at least one cycle.
export type UniversePriorityRule = {
  enabled: boolean;
  weights: Record<string, number>;
  weights_history: Array<{
    ts: string;
    cycle: number;
    weights: Record<string, number>;
  }>;
  weights_cycle_count: number;
  weights_last_update_at?: string | null;
  budget: {
    target_budget: number;
    binding_constraint: string;
    cycle_count: number;
    last_observation?: Record<string, unknown> | null;
    last_update_at?: string | null;
  };
  score_age_summary: {
    total_tracked: number;
    never_scored: number;
    median_age_sec: number;
  };
};

export type UniverseTransition = {
  ts: string;
  symbol: string;
  from_tier: string;
  to_tier: string;
  reason: string;
  score_delta?: number | null;
};

export type UniverseAssetClassCoverage = {
  total: number;
  by_asset_class: Array<{
    klass: string;
    count: number;
    share: number;
  }>;
};

export type IntelligenceUniverseResponse = {
  enabled: boolean;
  fallback?: string | null;
  generated_at: string;
  funnel: UniverseFunnelStage[];
  symbols: UniverseSymbolRow[];
  clusters: Array<{
    id: number;
    members: string[];
    representative: string;
    avg_abs_correlation: number;
    member_count: number;
  }>;
  promotions: unknown[];
  stream: Array<{
    sym: string;
    klass?: string;
    why?: string;
    conviction?: number;
    trend?: string;
    promotedAt?: number;
    spark?: number[];
    bookCorr?: number;
    topFactors?: Array<[string, string]>;
    relatedNews?: Array<{ source?: string; text?: string }>;
  }>;
  config_mirror: Record<string, unknown>;
  build: Record<string, unknown>;
  broker_totals?: Record<string, number>;
  coverage?: {
    broker_listing_count?: number;
    unique_normalized_count?: number;
    scored_candidate_count?: number;
    watched_count?: number;
    registry_active_count?: number;
    caps?: Record<string, number>;
    by_broker?: Record<string, {
      raw?: number;
      normalized?: number;
      source?: string;
      note?: string | null;
      // D116 instrument registry — total registry rows known for the broker,
      // and the subset already resolved to an actionable availability status.
      registry_known_count?: number;
      registry_covered_count?: number;
    }>;
  };
  // D117 — adaptive universe-tier sizing. Present only when the pipeline
  // has resolved at least one adaptive cycle and the feature is enabled.
  adaptive?: {
    enabled: boolean;
    updated_at?: string;
    resolved?: {
      candidates?: number;
      watching?: number;
      core?: number;
      scan?: number;
      base?: { candidates?: number; watching?: number; core?: number; scan?: number };
      multiplier?: number;
      regime_multiplier?: number;
      signal_pressure_multiplier?: number;
      cluster_floor_applied?: boolean;
      reasons?: string[];
    };
    context?: {
      regime_label?: string;
      breadth_score?: number | null;
      signal_pressure?: number | null;
      active_cluster_count?: number | null;
      note?: string | null;
    };
    consecutive_misses_count?: number;
    last_grace_extended?: string[];
  } | null;
  // D118 — self-tuning priority pre-filter, tier-transition stream,
  // and asset-class coverage rollup. ``priority_rule`` is ``null``
  // before the first pipeline cycle has completed.
  priority_rule?: UniversePriorityRule | null;
  transitions?: UniverseTransition[];
  asset_class_coverage?: UniverseAssetClassCoverage | null;
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
  /** Connect Hub inventory for brokers, feeds, AI providers, and treasury accounts. */
  connect_hub?: ConnectHubResponse;
};

export type ConnectHubSecret = {
  env: string;
  label: string;
  required: boolean;
  configured: boolean;
};

export type ConnectHubConnector = {
  id: string;
  label: string;
  category: 'brokers' | 'information_feeds' | 'ai_providers' | 'treasury_accounts' | string;
  enabled: boolean;
  configured: boolean;
  connected: boolean;
  healthy: boolean;
  state: string;
  adapter?: string | null;
  auth_type: string;
  required_secrets: ConnectHubSecret[];
  capabilities: Record<string, boolean>;
  roles: string[];
  safety: Record<string, unknown>;
  docs_url?: string | null;
  notes?: string | null;
  status?: Record<string, unknown>;
  next_actions?: Array<{ kind: string; label: string }>;
  /** D127 P2 — certification tier: 'certified' may execute, 'experimental' informs only. */
  certification?: string;
};

/** D127 P3 — GET /connect/ai/pipeline — the four fixed AI stages. */
export type AiPipelineStage = {
  id: string;
  label: string;
  role: string;
  order: number;
  core: boolean;
  summary: string;
  enabled: boolean;
  can_disable: boolean;
  disable_blocked_reason: string;
  can_delete: boolean;
  model: Record<string, unknown>;
};

export type AiPipelineView = {
  stages: AiPipelineStage[];
  stage_count: number;
  enabled_count: number;
};

/** D127 P1 — POST /connect/test result. */
export type ConnectTestResponse = {
  ok: boolean;
  connector: { category: string; id: string };
  status: string;
  probe: {
    ok: boolean;
    partial: boolean;
    reason: string;
    detected_capabilities: Record<string, boolean>;
    latency_ms?: number | null;
  };
  state_persisted: boolean;
  connect_hub: ConnectHubResponse;
};

export type ConnectHubResponse = {
  generated_at: string;
  categories: Record<string, ConnectHubConnector[]>;
  summary: Record<
    string,
    {
      total: number;
      enabled: number;
      configured: number;
      connected: number;
      healthy: number;
      ids: string[];
      connected_ids: string[];
    }
  >;
  capability_flags: {
    can_trade: boolean;
    has_information_feed: boolean;
    has_ai_provider: boolean;
    has_treasury_account: boolean;
    can_auto_transfer: boolean;
  };
};

export type ConnectConfigureRequest = {
  category: string;
  connector_id: string;
  secrets: Record<string, string>;
  enable?: boolean;
};

export type ConnectConfigureResponse = {
  ok: boolean;
  connector: { category: string; id: string; enabled: boolean };
  written_env: string[];
  ai_config_updated?: boolean;
  requires_restart: boolean;
  next_step: string;
  connect_hub: ConnectHubResponse;
};

export type ConnectAddRequest = {
  category: string;
  connector_id: string;
  label: string;
  auth_type?: string;
  required_env?: string[];
  capabilities?: Record<string, boolean>;
  roles?: string[];
  docs_url?: string | null;
  notes?: string | null;
  scaffold_adapter?: boolean;
};

export type ConnectAddResponse = {
  ok: boolean;
  created: {
    category: string;
    id: string;
    label: string;
    scaffolded_adapter_path?: string | null;
  };
  requires_adapter_implementation: boolean;
  next_step: string;
  connect_hub: ConnectHubResponse;
};

export type ConnectControlRequest = {
  category: string;
  connector_id: string;
  enabled?: boolean;
  exposure_action?: 'block_new_only';
};

export type ConnectControlResponse = {
  ok: boolean;
  connector?: { category: string; id: string; enabled: boolean };
  deleted?: { category: string; id: string; label: string };
  ai_config_updated?: boolean;
  runtime_applied?: string[];
  requires_restart: boolean;
  next_step: string;
  connect_hub: ConnectHubResponse;
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
  getRealisedCurve: (days = 400) =>
    getJson<ApiRealisedCurveResponse>(`/pnl/realised-curve?days=${days}`),
  getPositions: (limit = POSITIONS_POLL_LIMIT) =>
    getJson<ApiPositionsResponse>(`/positions?limit=${limit}`),
  getSignals: (limit = 20) => getJson<ApiSignalsResponse>(`/signals?limit=${limit}`),
  getNews: (limit = 30, impactfulOnly = false) =>
    getJson<ApiNewsResponse>(`/news?limit=${limit}&impactful_only=${impactfulOnly ? 'true' : 'false'}`),
  getStatus: () => getJson<ApiStatusResponse>('/status'),
  getSystemStatus: () => getJson<SystemStatusResponse>('/system/status'),
  getConnectHub: () => getJson<ConnectHubResponse>('/connect/hub'),
  configureConnector: (body: ConnectConfigureRequest) =>
    postJsonBody<ConnectConfigureResponse>('/connect/configure', body),
  addConnector: (body: ConnectAddRequest) =>
    postJsonBody<ConnectAddResponse>('/connect/add', body),
  setConnectorEnabled: (body: ConnectControlRequest) =>
    postJsonBody<ConnectControlResponse>('/connect/enable', body),
  deleteConnector: (body: ConnectControlRequest) =>
    postJsonBody<ConnectControlResponse>('/connect/delete', body),
  testConnector: (body: { category: string; connector_id: string }) =>
    postJsonBody<ConnectTestResponse>('/connect/test', body),
  getAiPipeline: () => getJson<AiPipelineView>('/connect/ai/pipeline'),
  systemStart: () => postJson<SystemStatusResponse>('/system/start'),
  systemStop: () => postJson<SystemStatusResponse>('/system/stop'),
  // Risk-engine kill switch. Activating it blocks both new opens AND
  // reduce-only closes (stop-loss, profit-harvest, allocator flatten) so
  // the book stays exactly as-is until reset. Wired to the UI's
  // "armed/paused" toggle so a single click freezes activity.
  riskKill: () => postJsonBody<{ kill_switch: boolean }>('/kill', {}),
  riskResetKill: () => postJsonBody<{ kill_switch: boolean }>('/kill/reset', {}),
  setCapitalAllocation: (pct: number) =>
    putJson<{ capital_pct: number }>('/system/capital-allocation', { pct }),
  getSystemMode: () => getJson<SystemModeResponse>('/system/mode'),
  setSystemMode: (mode: TradingMode) =>
    postJsonBody<SystemModeResponse>('/system/mode', { mode }),
  getDiscoverySummary: () => getJson<DiscoverySummaryResponse>('/discovery/summary'),
  getDiscoveryAnomalies: (limit = 8) => getJson<DiscoveryAnomaliesResponse>(`/discovery/anomalies?limit=${limit}`),
  getIntelligenceRegime: () => getJson<IntelligenceRegimeResponse>('/intelligence/regime'),
  getIntelligenceUniverse: () => getJson<IntelligenceUniverseResponse>('/intelligence/universe'),
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
