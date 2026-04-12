import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getJson,
  postJson,
  postDashboardLogin,
  wsUrl,
  LS_DASH,
  LS_CONTROL,
} from "./api";

function Card({ title, value }) {
  return (
    <div className="card">
      <div className="label">{title}</div>
      <div className="value">{value}</div>
    </div>
  );
}

function JsonTable({ title, rows, columns }) {
  return (
    <div className="panel">
      <h3>{title}</h3>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
          </thead>
          <tbody>
            {(rows || []).slice(0, 25).map((r, idx) => (
              <tr key={idx}>
                {columns.map((c) => (
                  <td key={c}>{String(r?.[c] ?? "")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PnlMiniChart({ history }) {
  if (!history || history.length < 2) return <div className="muted">Not enough history for chart</div>;
  const vals = history.map((x) => Number(x.portfolio_value || 0));
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const w = 600;
  const h = 140;
  const pad = 8;
  const innerW = w - pad * 2;
  const innerH = h - pad * 2;
  let peak = vals[0];
  const dd = vals.map((v) => {
    peak = Math.max(peak, v);
    return peak > 0 ? ((v - peak) / peak) * 100 : 0;
  });
  const ddMin = Math.min(...dd, 0);
  const ddMax = Math.max(...dd, 0);
  const linePoints = vals
    .map((v, i) => {
      const x = pad + (i / Math.max(1, vals.length - 1)) * innerW;
      const y = pad + (max === min ? innerH / 2 : innerH - ((v - min) / (max - min)) * innerH);
      return `${x},${y}`;
    })
    .join(" ");
  const ddPoints = dd
    .map((d, i) => {
      const x = pad + (i / Math.max(1, dd.length - 1)) * innerW;
      const y =
        pad +
        (ddMax === ddMin ? innerH / 2 : innerH - ((d - ddMin) / (ddMax - ddMin + 1e-9)) * innerH);
      return `${x},${y}`;
    })
    .join(" ");
  const zeroY =
    pad +
    (ddMax === ddMin ? innerH / 2 : innerH - ((0 - ddMin) / (ddMax - ddMin + 1e-9)) * innerH);
  const areaPoints = `${pad},${zeroY} ${ddPoints} ${pad + innerW},${zeroY}`;

  return (
    <div>
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
        <polygon fill="rgba(255,100,100,0.15)" stroke="none" points={areaPoints} />
        <polyline fill="none" stroke="#ff6666" strokeWidth="1.5" points={ddPoints} opacity={0.9} />
        <line x1={pad} x2={w - pad} y1={zeroY} y2={zeroY} stroke="#555" strokeDasharray="4 4" />
        <polyline fill="none" stroke="#6aa6ff" strokeWidth="2" points={linePoints} />
      </svg>
      <p className="muted small">
        Blue: portfolio value. Red: drawdown % from running peak (shaded below 0%).
      </p>
    </div>
  );
}

async function pollCommandUntilDone(commandId, onUpdate, maxSec = 120) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxSec * 1000) {
    try {
      const row = await getJson(`/control/commands/${commandId}`);
      onUpdate(row);
      if (row.status === "done" || row.status === "failed") return row;
    } catch {
      /* ignore */
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  return null;
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [signals, setSignals] = useState([]);
  const [orders, setOrders] = useState([]);
  const [positions, setPositions] = useState([]);
  const [pnl, setPnl] = useState(null);
  const [pnlHistory, setPnlHistory] = useState([]);
  const [anomalies, setAnomalies] = useState([]);
  const [theses, setTheses] = useState([]);
  const [selectedSignal, setSelectedSignal] = useState(null);
  const [params, setParams] = useState({});
  const [strategyName, setStrategyName] = useState("momentum_breakout");
  const [strategyEnabled, setStrategyEnabled] = useState(true);
  const [paramName, setParamName] = useState("max_single_position_pct");
  const [paramValue, setParamValue] = useState("0.2");
  const [paramReason, setParamReason] = useState("dashboard update");
  const [msg, setMsg] = useState("");
  const [token, setToken] = useState(localStorage.getItem(LS_CONTROL) || "");
  const [dashToken, setDashToken] = useState(localStorage.getItem(LS_DASH) || "");
  const [loginPwd, setLoginPwd] = useState("");
  const [needsLogin, setNeedsLogin] = useState(false);
  const [commandStatus, setCommandStatus] = useState(null);
  const [recentEvents, setRecentEvents] = useState([]);
  const wsRef = useRef(null);
  const reconnectAttempt = useRef(0);
  const shouldReconnectWs = useRef(true);
  const reconnectTimer = useRef(null);
  const [wsStale, setWsStale] = useState(false);
  const MAX_WS_RECONNECT = 18;

  const cards = useMemo(
    () => [
      { title: "Mode", value: status?.mode ?? "-" },
      { title: "Paper", value: String(status?.paper_mode ?? "-") },
      { title: "Kill Switch", value: String(status?.kill_switch ?? "-") },
      { title: "Brokers", value: (status?.connected_brokers || []).join(", ") || "-" },
    ],
    [status]
  );

  const refresh = useCallback(async () => {
    try {
      const [s, sig, ord, pos, p, rp, ph, an, th] = await Promise.all([
        getJson("/status"),
        getJson("/signals?limit=50"),
        getJson("/orders?limit=50"),
        getJson("/positions?limit=50"),
        getJson("/pnl"),
        getJson("/risk/parameters"),
        getJson("/pnl/history?limit=90"),
        getJson("/discovery/anomalies?limit=50"),
        getJson("/discovery/theses?limit=50"),
      ]);
      setStatus(s);
      setSignals(sig.signals || []);
      setOrders(ord.orders || []);
      setPositions(pos.positions || []);
      setPnl(p);
      setParams(rp.parameters || {});
      setPnlHistory(ph.history || []);
      setAnomalies(an.anomalies || []);
      setTheses(th.theses || []);
      setSelectedSignal((sig.signals || [])[0] || null);
      setNeedsLogin(false);
    } catch (e) {
      if (e.status === 401 || String(e.message).includes("401")) {
        setNeedsLogin(true);
        setMsg("Dashboard read token required — login or paste token.");
      } else {
        setMsg(`Refresh error: ${e.message}`);
      }
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const connectWs = useCallback(() => {
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    if (wsRef.current) {
      try {
        wsRef.current.close();
      } catch {
        /* ignore */
      }
    }
    try {
      const socket = new WebSocket(wsUrl());
      wsRef.current = socket;
      socket.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "tick" && m.payload) {
          if (m.payload.status) setStatus(m.payload.status);
          if (Array.isArray(m.payload.events) && m.payload.events.length) {
            setRecentEvents((prev) => [...(m.payload.events || []), ...prev].slice(0, 30));
          }
        } else if (m.type === "status") {
          setStatus(m.payload);
        }
      };
      socket.onopen = () => {
        reconnectAttempt.current = 0;
        setWsStale(false);
      };
      socket.onclose = () => {
        if (!shouldReconnectWs.current) return;
        if (reconnectAttempt.current >= MAX_WS_RECONNECT) {
          setWsStale(true);
          return;
        }
        const base = Math.min(30000, 1000 * 2 ** reconnectAttempt.current);
        const jitter = Math.min(1500, base * 0.15) * Math.random();
        const delay = base + jitter;
        reconnectAttempt.current += 1;
        reconnectTimer.current = setTimeout(() => connectWs(), delay);
      };
    } catch {
      /* polling fallback */
    }
  }, []);

  useEffect(() => {
    shouldReconnectWs.current = true;
    localStorage.setItem(LS_DASH, dashToken);
    connectWs();
    return () => {
      shouldReconnectWs.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          /* ignore */
        }
      }
    };
  }, [dashToken, connectWs]);

  async function doLogin() {
    try {
      const r = await postDashboardLogin(loginPwd);
      if (r.token) {
        setDashToken(r.token);
        localStorage.setItem(LS_DASH, r.token);
        setNeedsLogin(false);
        setMsg("Logged in.");
        refresh();
        connectWs();
      }
    } catch (e) {
      setMsg(`Login failed: ${e.message}`);
    }
  }

  async function runWithCommandWatch(promiseFn) {
    setCommandStatus(null);
    const r = await promiseFn();
    const cid = r.command_id;
    if (cid != null) {
      setMsg(`${r.message || "command"} — id ${cid} (waiting…)`);
      void pollCommandUntilDone(cid, setCommandStatus);
    } else {
      setMsg(r.message || JSON.stringify(r));
    }
    await refresh();
  }

  async function doKill() {
    await runWithCommandWatch(() => postJson("/kill", {}));
  }
  async function doResetKill() {
    await runWithCommandWatch(() => postJson("/kill/reset", {}));
  }
  async function doToggle() {
    await runWithCommandWatch(() => postJson(`/strategy/${strategyName}/toggle`, { enabled: strategyEnabled }));
  }
  async function doParamUpdate() {
    await runWithCommandWatch(() =>
      postJson(`/risk/parameters/${paramName}`, {
        value: paramValue,
        reason: paramReason,
      })
    );
  }

  if (needsLogin) {
    return (
      <div className="page">
        <h1>mytbot Dashboard</h1>
        <div className="panel">
          <h3>Authenticate</h3>
          <p className="muted">
            Set <code>DASHBOARD_PASSWORD</code> and <code>DASHBOARD_READ_TOKEN</code> on the API, or paste the read token
            below.
          </p>
          <div className="row">
            <input
              type="password"
              placeholder="Dashboard password (if configured)"
              value={loginPwd}
              onChange={(e) => setLoginPwd(e.target.value)}
            />
            <button type="button" onClick={doLogin}>
              Login
            </button>
          </div>
          <div className="row">
            <input
              placeholder="Or paste DASHBOARD_READ_TOKEN"
              value={dashToken}
              onChange={(e) => {
                setDashToken(e.target.value);
                localStorage.setItem(LS_DASH, e.target.value);
              }}
            />
            <button
              type="button"
              onClick={() => {
                refresh();
                connectWs();
              }}
            >
              Apply token
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <h1>mytbot M7 Dashboard</h1>
      <p className="muted">{msg}</p>
      {wsStale && (
        <div className="panel">
          <p className="muted">
            Live WebSocket stopped after {MAX_WS_RECONNECT} reconnect attempts (API may have restarted). HTTP refresh still runs every 5s.
            <button
              type="button"
              style={{ marginLeft: 8 }}
              onClick={() => {
                reconnectAttempt.current = 0;
                setWsStale(false);
                connectWs();
              }}
            >
              Retry WebSocket
            </button>
          </p>
        </div>
      )}
      {commandStatus && (
        <p className="muted">
          Command {commandStatus.id}: <strong>{commandStatus.status}</strong>
          {commandStatus.error ? ` — ${commandStatus.error}` : ""}
        </p>
      )}
      {recentEvents.length > 0 && (
        <div className="panel">
          <h3>Recent events (WebSocket)</h3>
          <ul className="event-list">
            {recentEvents.slice(0, 12).map((ev, i) => (
              <li key={i}>
                <code>{ev.type}</code> {ev.ts ? `— ${ev.ts}` : ""}{" "}
                {ev.payload ? JSON.stringify(ev.payload).slice(0, 120) : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="token">
        <label>Dashboard read token:</label>
        <input
          value={dashToken}
          onChange={(e) => {
            const v = e.target.value;
            setDashToken(v);
            localStorage.setItem(LS_DASH, v);
          }}
          placeholder="X-Dashboard-Token (if API requires)"
        />
      </div>
      <div className="token">
        <label>Control Token:</label>
        <input
          value={token}
          onChange={(e) => {
            const v = e.target.value;
            setToken(v);
            localStorage.setItem(LS_CONTROL, v);
          }}
          placeholder="optional if API_CONTROL_TOKEN unset"
        />
      </div>

      <div className="cards">
        {cards.map((c) => (
          <Card key={c.title} title={c.title} value={c.value} />
        ))}
      </div>

      <div className="panel controls">
        <h3>Control Actions</h3>
        <button type="button" onClick={doKill}>
          Activate Kill
        </button>
        <button type="button" onClick={doResetKill}>
          Reset Kill
        </button>
        <div className="row">
          <input value={strategyName} onChange={(e) => setStrategyName(e.target.value)} />
          <select value={String(strategyEnabled)} onChange={(e) => setStrategyEnabled(e.target.value === "true")}>
            <option value="true">enabled</option>
            <option value="false">disabled</option>
          </select>
          <button type="button" onClick={doToggle}>
            Toggle Strategy
          </button>
        </div>
      </div>

      <div className="panel controls">
        <h3>Risk Threshold Editor</h3>
        <div className="row">
          <input value={paramName} onChange={(e) => setParamName(e.target.value)} />
          <input value={paramValue} onChange={(e) => setParamValue(e.target.value)} />
          <input value={paramReason} onChange={(e) => setParamReason(e.target.value)} />
          <button type="button" onClick={doParamUpdate}>
            Apply
          </button>
        </div>
        <pre>{JSON.stringify(params, null, 2)}</pre>
      </div>

      <JsonTable
        title="Signals"
        rows={signals}
        columns={["timestamp", "symbol", "side", "strategy", "confidence", "news_score"]}
      />
      <JsonTable
        title="Discovery Anomalies"
        rows={anomalies}
        columns={["timestamp", "symbol", "direction", "price_move_pct", "price_z_score", "anomaly_score", "thesis_generated"]}
      />
      <JsonTable
        title="Discovery Theses"
        rows={theses}
        columns={["timestamp", "trigger_symbol", "trigger_direction", "overall_confidence", "time_horizon_hours", "model_used"]}
      />
      <div className="panel">
        <h3>Trade Detail View</h3>
        <pre>{JSON.stringify(selectedSignal, null, 2)}</pre>
      </div>
      <JsonTable
        title="Orders"
        rows={orders}
        columns={["timestamp", "symbol", "side", "order_type", "status", "filled_quantity", "avg_fill_price"]}
      />
      <JsonTable title="Positions" rows={positions} columns={["timestamp", "symbol", "quantity", "current_price", "asset_class"]} />
      <div className="panel">
        <h3>PnL</h3>
        <pre>{JSON.stringify(pnl, null, 2)}</pre>
      </div>
      <div className="panel">
        <h3>Performance Chart (Portfolio Value + Drawdown)</h3>
        <PnlMiniChart history={pnlHistory} />
      </div>
    </div>
  );
}
