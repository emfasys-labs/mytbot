/**
 * Operator-facing strategy names — single source for Strategy Mix cards and
 * anywhere else we show a strategy id from the backend.
 *
 * Convention: sentence-style titles (first word capitalized; "arb" stays
 * lowercase; hyphenated compounds preserved). Unknown ids are derived from
 * snake_case via {@link humanizeStrategyId}.
 */

/** Canonical display title per backend strategy / coordinator id. */
export const STRATEGY_DISPLAY_NAMES: Record<string, string> = {
  momentum_breakout: 'Momentum breakout',
  mean_reversion: 'Mean reversion',
  volume_flow: 'Volume flow',
  event_driven_news: 'Event-driven (news)',
  pairs_trading: 'Pairs trading',
  volatility_regime: 'Volatility regime',
  regime_rotation: 'Regime rotation',
  funding_rate_arbitrage: 'Funding rate arb',
  cross_exchange_arbitrage: 'Cross-exchange arb',
  factor_sleeve: 'Factor sleeve',
  stat_arb_pairs: 'Stat-arb pairs',
  options_long_call: 'Options long call',
  options_long_put: 'Options long put',
  options_protective_put: 'Protective put',
  options_covered_call: 'Covered call',
  global_edge_rotation: 'Global edge rotation',
  global_edge_trim: 'Global edge trim',
  global_edge_flatten: 'Global edge flatten',
  capital_recycle: 'Capital recycle',
  adaptive_shed: 'Adaptive shed',
  stop_loss_monitor: 'Stop-loss monitor',
  profit_harvest_monitor: 'Profit harvest monitor',
};

/** Words that stay lowercase when not in the first position. */
const LOWERCASE_TAIL_WORDS = new Set(['arb']);

/**
 * Turn ``momentum_breakout`` / ``global edge rotation`` into a display title.
 */
export function humanizeStrategyId(raw: string): string {
  const normalized = raw.trim().replace(/_/g, ' ').replace(/\s+/g, ' ').toLowerCase();
  if (!normalized) return '—';

  return normalized
    .split(' ')
    .map((word, index) => {
      if (word.includes('-')) {
        const parts = word.split('-');
        return parts
          .map((part, partIndex) => {
            const isFirst = index === 0 && partIndex === 0;
            if (!isFirst && LOWERCASE_TAIL_WORDS.has(part)) return part;
            return part.charAt(0).toUpperCase() + part.slice(1);
          })
          .join('-');
      }
      if (index > 0 && LOWERCASE_TAIL_WORDS.has(word)) return word;
      if (index === 0) return word.charAt(0).toUpperCase() + word.slice(1);
      return word;
    })
    .join(' ');
}

export function formatStrategyDisplayName(strategyId: string): string {
  const key = strategyId.trim().toLowerCase();
  if (!key) return '—';
  return STRATEGY_DISPLAY_NAMES[key] ?? humanizeStrategyId(strategyId);
}
