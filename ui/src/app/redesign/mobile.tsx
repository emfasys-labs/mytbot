/**
 * Mobile companion — thumb-reachable layout, wired to live system data.
 */

import { useState } from 'react';
import { EquityCurve, LiveFeed } from './dashboard';
import { prettySymbol } from './mapping';
import { Card, I, Label, NavNumber, Signed, Wordmark } from './primitives';
import { MasterButton } from './shell';
import { ACCENTS, AccentName, CURRENCY_SYMBOL, SystemState, TOKENS } from './tokens';
import type { LiveData } from './useLiveSystem';

type MobileTab = 'home' | 'book' | 'feed';

export function MobileApp({
  state, accent, armed, onArm, onPower, live,
}: {
  state: SystemState;
  accent: AccentName;
  armed: boolean;
  onArm: (v: boolean) => void;
  onPower: () => void;
  live: LiveData;
}) {
  const accentColor = ACCENTS[accent].main;
  const [tab, setTab] = useState<MobileTab>('home');
  const dayChange = live.nav - live.navOpen;

  return (
    <div style={{
      width: 390, height: 780, margin: '20px auto',
      background: TOKENS.bg0, border: `1px solid ${TOKENS.lineStrong}`,
      borderRadius: 36, overflow: 'hidden', position: 'relative',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        padding: '14px 22px 8px', display: 'flex', justifyContent: 'space-between',
        fontFamily: TOKENS.mono, fontSize: 11, color: TOKENS.ink2,
      }}>
        <span>{new Date().toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}</span>
        <span>
          {live.wsConnected && state === 'running'
            ? '● live'
            : live.wsConnected
              ? '● ws'
              : state === 'running'
                ? '○ poll'
                : '○ idle'}
        </span>
      </div>
      <div style={{
        padding: '8px 22px 16px', display: 'flex', alignItems: 'center',
        justifyContent: 'space-between', borderBottom: `1px solid ${TOKENS.line}`,
      }}>
        <Wordmark state={state} accent={accentColor} size={17} />
        <span style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>
          #{live.loopIteration || 0} · {live.path || '—'}
        </span>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 22px' }}>
        {tab === 'home' && (
          <>
            <Label>NAV</Label>
            <NavNumber value={live.nav} accent={accentColor} size={46} />
            <div style={{ display: 'flex', gap: 12, marginTop: 8 }}>
              <span style={{
                fontFamily: TOKENS.mono, fontSize: 12,
                color: dayChange >= 0 ? TOKENS.profit : TOKENS.loss,
              }}>
                {dayChange >= 0 ? '+' : '−'}{CURRENCY_SYMBOL}{Math.abs(dayChange).toFixed(0)} today
              </span>
            </div>

            <div style={{ marginTop: 18 }}>
              <EquityCurve values={live.equity.length ? live.equity : [live.nav, live.nav]} accent={accentColor} width={340} height={60} />
            </div>

            <div style={{ marginTop: 22, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
              <Card style={{ padding: 14 }}>
                <Label>Exposure</Label>
                <div style={{
                  fontFamily: TOKENS.sans, fontSize: 22, fontWeight: 300,
                  color: TOKENS.ink0, marginTop: 4, letterSpacing: '-0.02em',
                }}>
                  {(live.exposure.gross * 100).toFixed(0)}
                  <span style={{ fontSize: 13, color: TOKENS.ink3 }}>%</span>
                </div>
              </Card>
              <Card style={{ padding: 14 }}>
                <Label>Positions</Label>
                <div style={{ fontFamily: TOKENS.sans, fontSize: 22, fontWeight: 300, color: TOKENS.ink0, marginTop: 4 }}>
                  {live.positions.length}
                </div>
              </Card>
            </div>

            <Label style={{ marginTop: 22, marginBottom: 10 }}>Top conviction</Label>
            {live.conviction.length === 0 ? (
              <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
                awaiting pipeline publish
              </div>
            ) : live.conviction.slice(0, 4).map((c) => (
              <div key={c.sym} style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '10px 0', borderBottom: `1px solid ${TOKENS.line}`,
              }}>
                <span title={c.sym} style={{ fontFamily: TOKENS.sans, fontSize: 13, fontWeight: 500, color: TOKENS.ink0, width: 50, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {prettySymbol(c.sym)}
                </span>
                <div style={{ flex: 1, height: 4, background: 'rgba(255,255,255,0.04)', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{
                    width: `${c.score * 100}%`, height: '100%',
                    background: c.side === 'short' ? TOKENS.loss : accentColor,
                  }} />
                </div>
                <span style={{
                  fontFamily: TOKENS.mono, fontSize: 11,
                  color: c.side === 'short' ? TOKENS.loss : accentColor,
                  width: 36, textAlign: 'right',
                }}>
                  {c.score.toFixed(2)}
                </span>
              </div>
            ))}
          </>
        )}
        {tab === 'book' && (
          <>
            <Label style={{ marginBottom: 10 }}>Book</Label>
            {live.positions.length === 0 ? (
              <div style={{ color: TOKENS.ink3, fontFamily: TOKENS.mono, fontSize: 11 }}>
                No open positions
              </div>
            ) : live.positions.map((p) => (
              <div key={p.sym} style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '12px 0', borderBottom: `1px solid ${TOKENS.line}`,
              }}>
                <div style={{ flex: 1 }}>
                  <div title={p.sym} style={{ fontFamily: TOKENS.sans, fontSize: 14, fontWeight: 500, color: TOKENS.ink0 }}>{prettySymbol(p.sym)}</div>
                  <div style={{ fontFamily: TOKENS.mono, fontSize: 10, color: TOKENS.ink3 }}>{p.qty} · {p.last || p.avg}</div>
                </div>
                <Signed value={p.pnl} size={12} />
              </div>
            ))}
          </>
        )}
        {tab === 'feed' && (
          <>
            <Label style={{ marginBottom: 10 }}>Live feed</Label>
            <LiveFeed events={live.events} accent={accentColor} />
          </>
        )}
      </div>

      <div style={{
        padding: '12px 16px 24px', borderTop: `1px solid ${TOKENS.line}`, background: TOKENS.bg0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          {(
            [
              ['home', I.dash],
              ['book', I.wallet],
              ['feed', I.signal],
            ] as const
          ).map(([t, icon]) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              style={{
                flex: 1, padding: '10px 0',
                background: tab === t ? 'rgba(255,255,255,0.05)' : 'transparent',
                border: 'none', borderRadius: 8,
                color: tab === t ? TOKENS.ink0 : TOKENS.ink3,
                display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                cursor: 'pointer', fontFamily: TOKENS.sans, fontSize: 10,
                textTransform: 'uppercase', letterSpacing: '0.06em',
              }}
            >
              {icon()}
              {t}
            </button>
          ))}
          <MasterButton state={state} accent={accentColor} onArm={onArm} onPower={onPower} armed={armed} />
        </div>
      </div>
    </div>
  );
}
