/**
 * Secondary screens: Signals, Book, Risk, Strategies, TradeLog.
 * Ported from mytbot-design-system/project/prototypes/redesign/screens.jsx.
 */

import { DATA } from './data';
import { Card, Label, Pill, Signed, Spark } from './primitives';
import { ACCENTS, AccentName, TOKENS } from './tokens';

export function SignalsScreen({ accent }: { accent: AccentName }) {
  const accentColor = ACCENTS[accent].main;
  const rows = [
    ...DATA.conviction,
    ...DATA.conviction.slice(0, 4).map((c) => ({ ...c, sym: `${c.sym}·` })),
  ];
  const cols = '80px 60px 80px 1fr 80px 60px 100px';

  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>All signals · last 24h</Label>
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
        {rows.map((c, i) => (
          <div key={`${c.sym}-${i}`} style={{
            padding: '12px 18px', borderBottom: `1px solid ${TOKENS.line}`,
            display: 'grid', gridTemplateColumns: cols, gap: 16, alignItems: 'center',
          }}>
            <span style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, color: TOKENS.ink0 }}>{c.sym}</span>
            <span style={{
              fontFamily: TOKENS.mono, fontSize: 11,
              color: c.side === 'short' ? TOKENS.loss : TOKENS.ink2,
            }}>{c.side}</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ flex: 1, height: 3, background: 'rgba(255,255,255,0.05)', borderRadius: 2, overflow: 'hidden' }}>
                <div style={{
                  width: `${c.score * 100}%`, height: '100%',
                  background: c.side === 'short' ? TOKENS.loss : accentColor,
                }} />
              </div>
              <span style={{
                fontFamily: TOKENS.mono, fontSize: 11,
                color: c.side === 'short' ? TOKENS.loss : accentColor,
                width: 30, textAlign: 'right',
              }}>
                {c.score.toFixed(2)}
              </span>
            </div>
            <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>{c.strat}</span>
            <Pill size="sm" tone={c.urg === 'high' ? 'caution' : 'neutral'}>{c.urg}</Pill>
            <Pill size="sm" tone={i % 4 === 2 ? 'danger' : 'profit'}>{i % 4 === 2 ? 'blocked' : 'ok'}</Pill>
            <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{(i + 1) * 3}m ago</span>
          </div>
        ))}
      </Card>
    </div>
  );
}

export function BookScreen({ accent }: { accent: AccentName }) {
  const accentColor = ACCENTS[accent].main;
  const totalPnl = DATA.positions.reduce((s, p) => s + p.pnl, 0);
  return (
    <div style={{
      padding: 20, display: 'grid', gap: 14,
      gridTemplateColumns: '1fr 320px', height: '100%', overflow: 'hidden',
    }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14, minHeight: 0 }}>
        <Card>
          <Label style={{ marginBottom: 12 }}>Open positions</Label>
          <div style={{ display: 'grid', gap: 2 }}>
            {DATA.positions.map((p) => (
              <div key={p.sym} style={{
                display: 'grid', gridTemplateColumns: '100px 70px 90px 90px 1fr 90px',
                gap: 12, alignItems: 'center', padding: '10px 0',
                borderBottom: `1px solid ${TOKENS.line}`,
              }}>
                <div>
                  <div style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{p.sym}</div>
                  <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>qty {p.qty}</div>
                </div>
                <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2 }}>avg {p.avg}</span>
                <span style={{ fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink1 }}>last {p.last}</span>
                <Signed value={p.pnl} size={12} />
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <div style={{ flex: 1, height: 3, background: 'rgba(255,255,255,0.04)', borderRadius: 2, overflow: 'hidden' }}>
                    <div style={{ width: `${p.w * 100 * 4}%`, maxWidth: '100%', height: '100%', background: accentColor }} />
                  </div>
                  <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3, width: 36, textAlign: 'right' }}>
                    {(p.w * 100).toFixed(0)}%
                  </span>
                </div>
                <Spark values={[p.avg * 0.99, p.avg, p.avg * 1.01, p.last]} width={80} height={24} accent={accentColor} />
              </div>
            ))}
          </div>
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
                  ['gross', DATA.exposure.gross],
                  ['net', DATA.exposure.net],
                  ['cash', DATA.exposure.cash],
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
          </div>
        </Card>
        <Card>
          <Label style={{ marginBottom: 10 }}>By asset class</Label>
          <AssetBreakdown accent={accentColor} />
        </Card>
      </div>
    </div>
  );
}

function AssetBreakdown({ accent }: { accent: string }) {
  const classes = [
    { name: 'equities', w: 0.62, color: accent },
    { name: 'crypto',   w: 0.18, color: TOKENS.caution },
    { name: 'fx',       w: 0.12, color: TOKENS.info },
    { name: 'cash',     w: 0.08, color: TOKENS.ink3 },
  ];
  return (
    <>
      <div style={{ display: 'flex', height: 6, borderRadius: 3, overflow: 'hidden', marginBottom: 10 }}>
        {classes.map((c) => (
          <div key={c.name} style={{ width: `${c.w * 100}%`, background: c.color }} />
        ))}
      </div>
      {classes.map((c) => (
        <div key={c.name} style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0',
          fontFamily: TOKENS.mono, fontSize: 11,
        }}>
          <span style={{ width: 8, height: 8, borderRadius: 2, background: c.color }} />
          <span style={{ color: TOKENS.ink2, flex: 1 }}>{c.name}</span>
          <span style={{ color: TOKENS.ink1 }}>{(c.w * 100).toFixed(0)}%</span>
        </div>
      ))}
    </>
  );
}

export function RiskScreen({ accent }: { accent: AccentName }) {
  const accentColor = ACCENTS[accent].main;
  const gauges: Array<{ name: string; v: number; cap: number; tone: 'profit' | 'caution' | 'danger' }> = [
    { name: 'Max drawdown',   v: 0.32, cap: 1, tone: 'profit' },
    { name: 'Position heat',  v: 0.68, cap: 1, tone: 'caution' },
    { name: 'Asset class cap',v: 0.95, cap: 1, tone: 'danger' },
    { name: 'Order rate',     v: 0.21, cap: 1, tone: 'profit' },
  ];
  return (
    <div style={{
      padding: 20, display: 'grid', gap: 14,
      gridTemplateColumns: '1fr 1fr', height: '100%', overflow: 'auto',
    }}>
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label accent={accentColor}>Approved · 3</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>last hour</span>
        </div>
        {DATA.approved.map((a, i) => (
          <div key={`${a.sym}-${i}`} style={{ padding: '10px 0', borderBottom: `1px solid ${TOKENS.line}` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{a.sym}</span>
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
              <span>gate: all 7 checks</span>
            </div>
          </div>
        ))}
      </Card>
      <Card>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
          <Label style={{ color: TOKENS.loss }}>Rejected · 2</Label>
          <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>last hour</span>
        </div>
        {DATA.rejected.map((r, i) => (
          <div key={`${r.sym}-${i}`} style={{ padding: '10px 0', borderBottom: `1px solid ${TOKENS.line}` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{r.sym}</span>
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
        <Label style={{ marginBottom: 12 }}>Risk gauges</Label>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 18 }}>
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

export function StrategiesScreen({ accent }: { accent: AccentName }) {
  const accentColor = ACCENTS[accent].main;
  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>Strategy performance</Label>
      <div style={{ display: 'grid', gap: 14, gridTemplateColumns: 'repeat(2, 1fr)' }}>
        {DATA.strategies.map((s) => (
          <Card key={s.name}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <span style={{
                fontFamily: TOKENS.sans, fontSize: 15, fontWeight: 500,
                color: TOKENS.ink0, letterSpacing: '-0.02em',
              }}>{s.name}</span>
              <Pill tone="neutral">weight {(s.weight * 100).toFixed(0)}%</Pill>
            </div>
            <Spark
              values={Array.from({ length: 12 }, (_, i) => 1 + Math.sin(i * 0.7) * 0.2 + i * 0.05)}
              width={260} height={56} accent={accentColor}
            />
            <div style={{
              display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 12,
              marginTop: 12, paddingTop: 12, borderTop: `1px solid ${TOKENS.line}`,
            }}>
              <StratStat label="Sharpe"   value={s.sharpe.toFixed(2)} />
              <StratStat label="Win rate" value={`${(s.winRate * 100).toFixed(0)}%`} />
              <StratStat label="Trades"   value={String(s.trades)} />
            </div>
          </Card>
        ))}
      </div>
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

export function TradeLogScreen() {
  interface Row {
    t: string;
    kind: 'fill' | 'signal' | 'reject' | 'tick';
    sym?: string;
    side?: 'long' | 'short';
    qty?: number;
    price?: number;
    venue?: string;
    score?: number;
    strat?: string;
    reason?: string;
    loop?: number;
    path?: string;
    ok: boolean | null;
  }
  const rows: Row[] = [
    { t: '14:32:08', kind: 'fill',   sym: 'NVDA', side: 'long',  qty: 12,   price: 882.40, venue: 'ibkr',   ok: true },
    { t: '14:31:44', kind: 'signal', sym: 'MSFT', side: 'long',  score: 0.63, strat: 'momentum_breakout', ok: true },
    { t: '14:30:12', kind: 'reject', sym: 'META', side: 'short', reason: 'asset_class_limit', ok: false },
    { t: '14:28:02', kind: 'fill',   sym: 'AAPL', side: 'long',  qty: 20,   price: 182.44, venue: 'ibkr',   ok: true },
    { t: '14:26:45', kind: 'signal', sym: 'SPY',  side: 'long',  score: 0.42, strat: 'regime_filter', ok: true },
    { t: '14:24:11', kind: 'tick',   loop: 47,    path: 'D015',  ok: null },
    { t: '14:21:58', kind: 'fill',   sym: 'BTC',  side: 'long',  qty: 0.05, price: 43211,  venue: 'kraken', ok: true },
    { t: '14:19:22', kind: 'reject', sym: 'TSLA', side: 'short', reason: 'max_position', ok: false },
  ];
  return (
    <div style={{ padding: 20, height: '100%', overflow: 'auto' }}>
      <Label style={{ marginBottom: 14 }}>Trade log · 247 events today</Label>
      <Card noPad>
        {rows.map((r, i) => (
          <div key={`${r.t}-${i}`} style={{
            padding: '12px 18px',
            borderBottom: i < rows.length - 1 ? `1px solid ${TOKENS.line}` : 'none',
            display: 'flex', alignItems: 'center', gap: 16,
            fontFamily: TOKENS.mono, fontSize: 11,
          }}>
            <span style={{ color: TOKENS.ink3, width: 70 }}>{r.t}</span>
            <Pill size="sm" tone={r.ok === true ? 'profit' : r.ok === false ? 'danger' : 'neutral'}>{r.kind}</Pill>
            {r.sym && <span style={{ color: TOKENS.ink0, fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, width: 60 }}>{r.sym}</span>}
            {r.side && <span style={{ color: TOKENS.ink2, width: 44 }}>{r.side}</span>}
            {r.qty !== undefined && <span style={{ color: TOKENS.ink1 }}>{r.qty} @ {r.price}</span>}
            {r.score !== undefined && <span style={{ color: TOKENS.ink1 }}>score {r.score}</span>}
            {r.reason && <span style={{ color: TOKENS.loss }}>{r.reason}</span>}
            {r.loop !== undefined && <span style={{ color: TOKENS.ink2 }}>loop #{r.loop} · path {r.path}</span>}
            <span style={{ flex: 1 }} />
            {r.venue && <span style={{ color: TOKENS.ink3, textTransform: 'uppercase', fontSize: 9 }}>{r.venue}</span>}
            {r.strat && <span style={{ color: TOKENS.ink3 }}>{r.strat}</span>}
          </div>
        ))}
      </Card>
    </div>
  );
}
