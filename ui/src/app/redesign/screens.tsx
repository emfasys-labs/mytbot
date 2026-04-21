/**
 * Secondary screens: Signals, Book, Risk, Strategies, TradeLog.
 * All screens are wired to live system data via `LiveData`.
 */

import { useMemo } from 'react';
import { Card, Label, Pill, Signed, Spark } from './primitives';
import { ACCENTS, AccentName, TOKENS } from './tokens';
import type { LiveData } from './useLiveSystem';
import { mapOrdersToTradeLog, normalizeSide } from './mapping';

export function SignalsScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const cols = '80px 60px 120px 1fr 80px 80px 100px';

  const rows = useMemo(() => {
    const sigs = live.intelligence?.signals ?? [];
    return sigs.map((s) => {
      const verdict = (s.verdict ?? '').toLowerCase();
      const side = normalizeSide(s.side);
      const score = typeof s.confidence === 'number' ? Math.max(0, Math.min(1, s.confidence)) : 0;
      const urg: 'high' | 'med' | 'low' = score >= 0.7 ? 'high' : score >= 0.45 ? 'med' : 'low';
      const ts = s.timestamp ? Date.parse(s.timestamp) : 0;
      const age = ts > 0 ? minutesAgo(ts) : '—';
      return {
        sym: (s.symbol ?? '').toUpperCase(),
        side,
        score,
        strat: s.strategy ?? '—',
        urg,
        verdict: verdict === 'approved' ? 'ok' : 'blocked',
        age,
      };
    });
  }, [live.intelligence]);

  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>
        All signals · {rows.length ? `last ${rows.length}` : 'awaiting pipeline'}
      </Label>
      <Card noPad>
        <div style={{
          padding: '12px 18px', borderBottom: `1px solid ${TOKENS.line}`,
          display: 'grid', gridTemplateColumns: cols, gap: 16,
          fontFamily: TOKENS.sans, fontSize: 10, color: TOKENS.ink3,
          textTransform: 'uppercase', letterSpacing: '0.1em',
        }}>
          <span>Symbol</span><span>Side</span><span>Score</span><span>Strategy</span>
          <span>Urgency</span><span>Verdict</span><span>Time</span>
        </div>
        {rows.length === 0 ? (
          <div style={{ padding: 40, textAlign: 'center', color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No intelligence signals yet · start the system to begin streaming
          </div>
        ) : rows.map((r, i) => (
          <div key={`${r.sym}-${i}`} style={{
            padding: '12px 18px', borderBottom: `1px solid ${TOKENS.line}`,
            display: 'grid', gridTemplateColumns: cols, gap: 16, alignItems: 'center',
          }}>
            <span style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, color: TOKENS.ink0 }}>{r.sym || '—'}</span>
            <span style={{
              fontFamily: TOKENS.mono, fontSize: 11,
              color: r.side === 'short' ? TOKENS.loss : TOKENS.ink2,
            }}>{r.side}</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ flex: 1, height: 3, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                <div style={{
                  width: `${r.score * 100}%`, height: '100%',
                  background: r.side === 'short' ? TOKENS.loss : accentColor,
                }} />
              </div>
              <span style={{
                fontFamily: TOKENS.mono, fontSize: 11,
                color: r.side === 'short' ? TOKENS.loss : accentColor,
                width: 30, textAlign: 'right',
              }}>
                {r.score.toFixed(2)}
              </span>
            </div>
            <span style={{
              fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{r.strat}</span>
            <Pill size="sm" tone={r.urg === 'high' ? 'caution' : 'neutral'}>{r.urg}</Pill>
            <Pill size="sm" tone={r.verdict === 'blocked' ? 'danger' : 'profit'}>{r.verdict}</Pill>
            <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{r.age}</span>
          </div>
        ))}
      </Card>
    </div>
  );
}

function minutesAgo(ts: number): string {
  const secs = Math.max(0, (Date.now() - ts) / 1000);
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

export function BookScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const totalPnl = live.positions.reduce((s, p) => s + p.pnl, 0);
  const nav = live.nav > 0 ? live.nav : 0;
  // Sum the actual position notionals instead of deriving from ``nav *
  // exposure.gross`` — the latter was unreliable when the backend shipped
  // exposure as an absolute £ figure rather than a ratio (see D026).
  const deployedCapital = live.positions.reduce((sum, p) => sum + (p.notional || 0), 0);
  const pendingCapital = live.orders
    .filter((o) => isPendingOrder(o.status))
    .reduce((sum, o) => {
      const qty = toFiniteNumber(o.quantity);
      const px = toFiniteNumber(o.limit_price ?? o.avg_fill_price);
      if (qty <= 0 || px <= 0) return sum;
      return sum + (qty * px);
    }, 0);
  const capitalAtWork = deployedCapital + pendingCapital;
  const capitalAtWorkPct = nav > 0 ? Math.max(0, Math.min(1, capitalAtWork / nav)) : 0;

  return (
    <div style={{
      padding: 20, display: 'grid', gap: 14,
      gridTemplateColumns: '1fr 320px', height: '100%', overflow: 'hidden',
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minHeight: 0 }}>
        <Card>
          <Label style={{ marginBottom: 12 }}>Open positions</Label>
          {live.positions.length === 0 ? (
            <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
              No open positions
            </div>
          ) : (
            <div style={{ display: 'grid', gap: 2 }}>
              <div style={{
                display: 'grid',
                gridTemplateColumns: '110px 110px 80px 80px 95px 1fr 80px',
                gap: 12, padding: '0 0 6px 0',
                fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3,
                textTransform: 'uppercase', letterSpacing: '0.06em',
                borderBottom: `1px solid ${TOKENS.line}`,
              }}>
                <span>Symbol</span>
                <span>Size</span>
                <span>Avg</span>
                <span>Last</span>
                <span>P&amp;L</span>
                <span>Weight</span>
                <span style={{ textAlign: 'right' }}>Trend</span>
              </div>
              {live.positions.map((p) => (
                <div key={p.sym} style={{
                  display: 'grid',
                  gridTemplateColumns: '110px 110px 80px 80px 95px 1fr 80px',
                  gap: 12, alignItems: 'center', padding: '10px 0',
                  borderBottom: `1px solid ${TOKENS.line}`,
                }}>
                  <div>
                    <div style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{p.sym}</div>
                    <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
                      qty {p.qty}{p.broker ? ` · ${p.broker}` : ''}
                    </div>
                  </div>
                  <div>
                    <div style={{ fontFamily: TOKENS.mono, fontSize: 13, color: TOKENS.ink0 }}>
                      {fmtNotional(p.notional)}
                    </div>
                    <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>notional</div>
                  </div>
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>{fmtPrice(p.avg)}</span>
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink1 }}>{fmtPrice(p.last)}</span>
                  <Signed value={p.pnl} size={12} />
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <div style={{ flex: 1, height: 3, background: 'rgba(255,255,255,0.04)', borderRadius: 2, overflow: 'hidden' }}>
                      <div style={{ width: `${Math.min(100, p.w * 100 * 4)}%`, height: '100%', background: accentColor }} />
                    </div>
                    <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, width: 36, textAlign: 'right' }}>
                      {(p.w * 100).toFixed(1)}%
                    </span>
                  </div>
                  <Spark values={[p.avg * 0.99 || 0, p.avg || 0, p.avg * 1.01 || 0, p.last || p.avg || 0]} width={72} height={24} accent={accentColor} />
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <Card>
          <Label style={{ marginBottom: 10 }}>Totals</Label>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div>
              <Label style={{ marginBottom: 2 }}>Unrealised</Label>
              <div style={{
                fontFamily: TOKENS.sans, fontSize: 24, fontWeight: 300,
                color: totalPnl >= 0 ? TOKENS.profit : TOKENS.loss,
                letterSpacing: '-0.02em',
              }}>
                {totalPnl >= 0 ? '+' : '−'}£{Math.abs(totalPnl).toFixed(2)}
              </div>
            </div>
            <div style={{ borderTop: `1px solid ${TOKENS.line}`, paddingTop: 10 }}>
              <Label style={{ marginBottom: 6 }}>Exposure</Label>
              {(
                [
                  ['gross', live.exposure.gross],
                  ['net', live.exposure.net],
                  ['cash', live.exposure.cash],
                ] as const
              ).map(([k, v]) => (
                <div key={k} style={{
                  display: 'flex', justifyContent: 'space-between',
                  padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
                }}>
                  <span style={{ color: TOKENS.ink3 }}>{k}</span>
                  <span style={{ color: TOKENS.ink1 }}>{(v * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
            <div style={{ borderTop: `1px solid ${TOKENS.line}`, paddingTop: 10 }}>
              <Label style={{ marginBottom: 6 }}>Capital at work</Label>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
              }}>
                <span style={{ color: TOKENS.ink3 }}>deployed</span>
                <span style={{ color: TOKENS.ink1 }}>£{deployedCapital.toFixed(2)}</span>
              </div>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
              }}>
                <span style={{ color: TOKENS.ink3 }}>pending orders</span>
                <span style={{ color: TOKENS.ink1 }}>£{pendingCapital.toFixed(2)}</span>
              </div>
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '3px 0', fontFamily: TOKENS.mono, fontSize: 11,
              }}>
                <span style={{ color: TOKENS.ink3 }}>total working</span>
                <span style={{ color: TOKENS.ink1 }}>
                  £{capitalAtWork.toFixed(2)} ({(capitalAtWorkPct * 100).toFixed(1)}%)
                </span>
              </div>
            </div>
          </div>
        </Card>
        <Card>
          <Label style={{ marginBottom: 10 }}>Brokers</Label>
          {live.brokers.length === 0 ? (
            <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>No brokers configured</div>
          ) : (
            live.brokers.map((b) => (
              <div key={b.name} style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '6px 0', fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2,
              }}>
                <span>{b.name}</span>
                <Pill size="sm" tone={b.state === 'live' ? 'profit' : b.state === 'warming' ? 'caution' : 'neutral'}>{b.state}</Pill>
              </div>
            ))
          )}
        </Card>
      </div>
    </div>
  );
}

function toFiniteNumber(v: unknown): number {
  const n = typeof v === 'number' ? v : Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
}

function isPendingOrder(status: string | null | undefined): boolean {
  const s = String(status ?? '').toLowerCase();
  return s === 'pending' || s === 'open' || s === 'submitted' || s === 'partially_filled';
}

function fmtPrice(v: number): string {
  if (!Number.isFinite(v) || v === 0) return '—';
  return v >= 100 ? v.toFixed(2) : v.toFixed(4);
}

/** Compact account-currency formatter for position notionals. Uses ``k`` /
 *  ``M`` suffixes above £1k / £1M so the Book row stays one line on narrow
 *  cards while still exposing exact pence for small positions. */
function fmtNotional(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return '—';
  if (v >= 1_000_000) return `£${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 10_000) return `£${(v / 1_000).toFixed(1)}k`;
  if (v >= 1_000) return `£${(v / 1_000).toFixed(2)}k`;
  return `£${v.toFixed(2)}`;
}

export function RiskScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  const { approved, rejected, executionRejections } = live;

  const gauges = useMemo(() => {
    const portfolio = (live.snapshot?.portfolio ?? {}) as Record<string, unknown>;
    const nav = typeof portfolio.nav === 'string' || typeof portfolio.nav === 'number'
      ? Number(portfolio.nav) || 0 : 0;
    const gross = numFromPortfolio(portfolio.gross_exposure, nav);
    const net = Math.abs(numFromPortfolio(portfolio.net_exposure, nav));
    const maxPosCount = live.positions.length;
    const positionHeat = Math.min(1, maxPosCount / 20);
    const drawdown = numFromPortfolio(live.pnl?.metrics?.max_drawdown_pct);
    const orderRate = Math.min(1, (live.orders?.length ?? 0) / 50);
    const gauges: Array<{ name: string; v: number; cap: number; tone: 'profit' | 'caution' | 'danger' }> = [
      { name: 'Max drawdown',    v: Math.min(1, drawdown / 50), cap: 1, tone: drawdown > 20 ? 'danger' : drawdown > 10 ? 'caution' : 'profit' },
      { name: 'Position heat',   v: positionHeat, cap: 1, tone: positionHeat > 0.85 ? 'danger' : positionHeat > 0.6 ? 'caution' : 'profit' },
      { name: 'Gross exposure',  v: gross, cap: 1, tone: gross > 0.9 ? 'danger' : gross > 0.7 ? 'caution' : 'profit' },
      { name: 'Net exposure',    v: net, cap: 1, tone: net > 0.85 ? 'danger' : net > 0.6 ? 'caution' : 'profit' },
      { name: 'Order rate',      v: orderRate, cap: 1, tone: orderRate > 0.85 ? 'caution' : 'profit' },
    ];
    return gauges;
  }, [live.snapshot, live.positions, live.orders, live.pnl]);

  return (
    <div style={{
      padding: 20, display: 'grid', gap: 14,
      gridTemplateColumns: '1fr 1fr', height: '100%', overflow: 'auto',
    }}>
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label accent={accentColor}>Approved · {approved.length}</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>last batch</span>
        </div>
        {approved.length === 0 ? (
          <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No approved signals yet
          </div>
        ) : approved.map((a, i) => (
          <div key={`${a.sym}-${i}`} style={{ padding: '10px 0', borderBottom: `1px solid ${TOKENS.line}` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{a.sym || '—'}</span>
                <Pill size="sm" tone="neutral">{a.side}</Pill>
              </div>
              <Pill size="sm" tone="profit">approved</Pill>
            </div>
            <div style={{
              display: 'flex', gap: 14, marginTop: 6,
              fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink3,
            }}>
              <span>conf {a.conf.toFixed(1)}%</span>
              <span>quality {a.q.toFixed(2)}</span>
            </div>
          </div>
        ))}
      </Card>
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label style={{ color: TOKENS.loss }}>Rejected · {rejected.length}</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>last batch</span>
        </div>
        {rejected.length === 0 ? (
          <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11, lineHeight: 1.5 }}>
            No rejections
            <div style={{ marginTop: 4, color: TOKENS.ink4, fontSize: 10 }}>
              risk engine approved every recent signal
            </div>
          </div>
        ) : rejected.map((r, i) => (
          <div key={`${r.sym}-${i}`} style={{ padding: '10px 0', borderBottom: `1px solid ${TOKENS.line}` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{r.sym || '—'}</span>
                <Pill size="sm" tone="loss">{r.side}</Pill>
              </div>
              <Pill size="sm" tone="danger">blocked</Pill>
            </div>
            <div style={{ marginTop: 6, color: TOKENS.ink2, fontSize: 12, lineHeight: 1.4 }}>{r.explain}</div>
            <div style={{ marginTop: 4, fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.loss }}>{r.reason}</div>
          </div>
        ))}
      </Card>
      <Card style={{ gridColumn: '1 / -1' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label style={{ color: TOKENS.caution }}>
            Execution rejections · {executionRejections.length}
          </Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
            broker-side · post risk-gate
          </span>
        </div>
        {executionRejections.length === 0 ? (
          <div style={{ padding: 12, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No execution rejections — every approved order made it to the broker
          </div>
        ) : (
          <div style={{ display: 'grid', gap: 8 }}>
            {executionRejections.map((x) => (
              <div
                key={`${x.broker}-${x.sym}-${x.t}`}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '140px 80px 90px 1fr 140px',
                  alignItems: 'center',
                  gap: 12,
                  padding: '8px 10px',
                  background: 'rgba(255,255,255,0.02)',
                  border: `1px solid ${TOKENS.line}`,
                  borderRadius: 6,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, color: TOKENS.ink0 }}>
                    {x.sym}
                  </span>
                  <Pill size="sm" tone={x.side === 'long' ? 'profit' : 'loss'}>{x.side}</Pill>
                </div>
                <Pill size="sm" tone={x.status === 'rejected' ? 'danger' : 'caution'}>
                  {x.status}
                </Pill>
                <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>
                  {x.broker || '—'}
                </span>
                <span
                  style={{
                    fontFamily: TOKENS.mono,
                    fontSize: 11,
                    color: x.reason ? TOKENS.caution : TOKENS.ink3,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={x.reason ?? ''}
                >
                  {x.reason ?? '(no reason recorded — see backend log)'}
                </span>
                <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, textAlign: 'right' }}>
                  {formatRelativeTime(x.t)}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card style={{ gridColumn: '1 / -1' }}>
        <Label style={{ marginBottom: 12 }}>Risk gauges</Label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 18 }}>
          {gauges.map((g) => {
            const color = g.tone === 'danger' ? TOKENS.danger : g.tone === 'caution' ? TOKENS.caution : accentColor;
            const textColor = g.tone === 'danger' ? TOKENS.danger : g.tone === 'caution' ? TOKENS.caution : TOKENS.profit;
            return (
              <div key={g.name}>
                <div style={{
                  display: 'flex', justifyContent: 'space-between',
                  fontFamily: TOKENS.mono, fontSize: 11, marginBottom: 4,
                }}>
                  <span style={{ color: TOKENS.ink2 }}>{g.name}</span>
                  <span style={{ color: textColor }}>{(g.v * 100).toFixed(0)}%</span>
                </div>
                <div style={{ height: 4, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{
                    width: `${g.v * 100}%`, height: '100%',
                    background: color,
                    transition: `width 600ms ${TOKENS.ease}`,
                  }} />
                </div>
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

function numFromPortfolio(raw: unknown, nav = 0): number {
  if (raw == null || raw === '') return 0;
  const n = typeof raw === 'number' ? raw : parseFloat(String(raw));
  if (!Number.isFinite(n)) return 0;
  const a = Math.abs(n);
  if (a <= 1) return Math.max(0, Math.min(1, a));
  if (a <= 100) return Math.max(0, Math.min(1, a / 100));
  if (nav > 0) return Math.max(0, Math.min(1, a / nav));
  return 0;
}

function formatRelativeTime(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '—';
  const delta = Date.now() - ms;
  if (delta < 0) return 'just now';
  const s = Math.floor(delta / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export function StrategiesScreen({ accent, live }: { accent: AccentName; live: LiveData }) {
  const accentColor = ACCENTS[accent].main;
  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>Strategy mix</Label>
      {live.strategies.length === 0 ? (
        <Card>
          <div style={{ padding: 20, color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No strategy weights yet · waiting for first allocator publish
          </div>
        </Card>
      ) : (
        <div style={{ display: 'grid', gap: 14, gridTemplateColumns: 'repeat(2, 1fr)' }}>
          {live.strategies.map((s) => {
            const isIdle = !!s.idle || (s.weight === 0 && s.trades === 0);
            const isArb = s.kind === 'arbitrage';
            return (
              <Card key={s.name} style={isIdle ? { opacity: 0.72 } : undefined}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 8 }}>
                  <span style={{
                    fontFamily: TOKENS.sans, fontSize: 15, fontWeight: 500,
                    color: TOKENS.ink0, letterSpacing: '-0.02em',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{s.name}</span>
                  <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                    {isArb && <Pill tone="neutral">arbitrage</Pill>}
                    {s.enabled === false && <Pill tone="loss">disabled</Pill>}
                    {isIdle
                      ? <Pill tone="neutral">idle</Pill>
                      : <Pill tone="neutral">weight {(s.weight * 100).toFixed(0)}%</Pill>
                    }
                  </div>
                </div>
                {isIdle ? (
                  <div style={{
                    height: 56, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11,
                    borderRadius: 6, background: 'rgba(255,255,255,0.02)',
                  }}>
                    {isArb
                      ? 'awaiting arbitrage opportunity'
                      : 'registered · no opportunities in current regime'}
                  </div>
                ) : (
                  <Spark
                    values={Array.from({ length: 12 }, (_, i) => 1 + Math.sin(i * 0.7 + s.weight * 10) * 0.2 + i * 0.05 * s.weight)}
                    width={260} height={56} accent={accentColor}
                  />
                )}
                <div style={{
                  display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 12,
                  marginTop: 12, paddingTop: 12, borderTop: `1px solid ${TOKENS.line}`,
                }}>
                  <StratStat label="Avg conf" value={s.sharpe ? s.sharpe.toFixed(2) : '—'} />
                  <StratStat label="Opp score" value={s.winRate ? `${(s.winRate * 100).toFixed(0)}%` : '—'} />
                  <StratStat label="Opps"   value={String(s.trades)} />
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

function StratStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <Label style={{ marginBottom: 2 }}>{label}</Label>
      <span style={{ fontFamily: TOKENS.sans, fontSize: 16, fontWeight: 400, color: TOKENS.ink0 }}>{value}</span>
    </div>
  );
}

export function TradeLogScreen({ live }: { live: LiveData }) {
  const rows = useMemo(() => mapOrdersToTradeLog(live.orders), [live.orders]);
  const totalToday = rows.length;

  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>Trade log · {totalToday} events</Label>
      <Card noPad>
        {rows.length === 0 ? (
          <div style={{ padding: 40, textAlign: 'center', color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
            No orders yet
          </div>
        ) : rows.map((r, i) => (
          <div key={`${r.t}-${i}`} style={{
            padding: '12px 18px',
            borderBottom: i < rows.length - 1 ? `1px solid ${TOKENS.line}` : 'none',
            display: 'flex', alignItems: 'center', gap: 16,
            fontFamily: TOKENS.mono, fontSize: 11,
          }}>
            <span style={{ color: TOKENS.ink3, width: 130 }}>{r.t}</span>
            <Pill size="sm" tone={r.ok === true ? 'profit' : r.ok === false ? 'danger' : 'neutral'}>{r.kind}</Pill>
            {r.sym && <span style={{ color: TOKENS.ink0, fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, width: 60 }}>{r.sym}</span>}
            {r.side && <span style={{ color: TOKENS.ink2, width: 44 }}>{r.side}</span>}
            {r.qty !== undefined && Number.isFinite(r.qty) && (
              <span style={{ color: TOKENS.ink1 }}>
                {r.qty} {r.price ? `@ ${r.price}` : ''}
              </span>
            )}
            {r.reason && <span style={{ color: TOKENS.loss }}>{r.reason}</span>}
            <span style={{ flex: 1 }} />
            {r.venue && <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 9 }}>{r.venue}</span>}
          </div>
        ))}
      </Card>
    </div>
  );
}
