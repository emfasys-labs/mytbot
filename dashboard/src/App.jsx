import React, { useEffect, useMemo, useState } from "react";
import { getJson, postJson, wsUrl } from "./api";

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
  const h = 120;
  const points = vals
    .map((v, i) => {
      const x = (i / Math.max(1, vals.length - 1)) * w;
      const y = max === min ? h / 2 : h - ((v - min) / (max - min)) * h;
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
      <polyline fill="none" stroke="#6aa6ff" strokeWidth="2" points={points} />
    </svg>
  );
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [signals, setSignals] = useState([]);
  const [orders, setOrders] = useState([]);
  const [positions, setPositions] = useState([]);
  const [pnl, setPnl] = useState(null);
  const [pnlHistory, setPnlHistory] = useState([]);
  const [selectedSignal, setSelectedSignal] = useState(null);
  const [params, setParams] = useState({});
  const [strategyName, setStrategyName] = useState("momentum_breakout");
  const [strategyEnabled, setStrategyEnabled] = useState(true);
  const [paramName, setParamName] = useState("max_single_position_pct");
  const [paramValue, setParamValue] = useState("0.2");
  const [paramReason, setParamReason] = useState("dashboard update");
  const [msg, setMsg] = useState("");
  const [token, setToken] = useState(localStorage.getItem("controlToken") || "");

  const cards = useMemo(
    () => [
      { title: "Mode", value: status?.mode ?? "-" },
      { title: "Paper", value: String(status?.paper_mode ?? "-") },
      { title: "Kill Switch", value: String(status?.kill_switch ?? "-") },
      { title: "Brokers", value: (status?.connected_brokers || []).join(", ") || "-" },
    ],
    [status]
  );

  async function refresh() {
    try {
      const [s, sig, ord, pos, p, rp, ph] = await Promise.all([
        getJson("/status"),
        getJson("/signals?limit=50"),
        getJson("/orders?limit=50"),
        getJson("/positions?limit=50"),
        getJson("/pnl"),
        getJson("/risk/parameters"),
        getJson("/pnl/history?limit=90"),
      ]);
      setStatus(s);
      setSignals(sig.signals || []);
      setOrders(ord.orders || []);
      setPositions(pos.positions || []);
      setPnl(p);
      setParams(rp.parameters || {});
      setPnlHistory(ph.history || []);
      setSelectedSignal((sig.signals || [])[0] || null);
    } catch (e) {
      setMsg(`Refresh error: ${e.message}`);
    }
  }

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let socket;
    try {
      socket = new WebSocket(wsUrl());
      socket.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "status") setStatus(m.payload);
      };
    } catch {
      // fallback polling already active
    }
    return () => {
      if (socket) socket.close();
    };
  }, []);

  async function doKill() {
    const r = await postJson("/kill", {});
    setMsg(`Kill command accepted (${r.command_id ?? "local"})`);
    refresh();
  }
  async function doResetKill() {
    const r = await postJson("/kill/reset", {});
    setMsg(`Reset command accepted (${r.command_id ?? "local"})`);
    refresh();
  }
  async function doToggle() {
    const r = await postJson(`/strategy/${strategyName}/toggle`, { enabled: strategyEnabled });
    setMsg(`Strategy toggle enqueued (${r.command_id})`);
    refresh();
  }
  async function doParamUpdate() {
    const r = await postJson(`/risk/parameters/${paramName}`, {
      value: paramValue,
      reason: paramReason,
    });
    setMsg(`Parameter update enqueued (${r.command_id})`);
    refresh();
  }

  return (
    <div className="page">
      <h1>mytbot M7 Dashboard</h1>
      <p className="muted">{msg}</p>
      <div className="token">
        <label>Control Token:</label>
        <input
          value={token}
          onChange={(e) => {
            const v = e.target.value;
            setToken(v);
            localStorage.setItem("controlToken", v);
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
        <button onClick={doKill}>Activate Kill</button>
        <button onClick={doResetKill}>Reset Kill</button>
        <div className="row">
          <input value={strategyName} onChange={(e) => setStrategyName(e.target.value)} />
          <select value={String(strategyEnabled)} onChange={(e) => setStrategyEnabled(e.target.value === "true")}>
            <option value="true">enabled</option>
            <option value="false">disabled</option>
          </select>
          <button onClick={doToggle}>Toggle Strategy</button>
        </div>
      </div>

      <div className="panel controls">
        <h3>Risk Threshold Editor</h3>
        <div className="row">
          <input value={paramName} onChange={(e) => setParamName(e.target.value)} />
          <input value={paramValue} onChange={(e) => setParamValue(e.target.value)} />
          <input value={paramReason} onChange={(e) => setParamReason(e.target.value)} />
          <button onClick={doParamUpdate}>Apply</button>
        </div>
        <pre>{JSON.stringify(params, null, 2)}</pre>
      </div>

      <JsonTable
        title="Signals"
        rows={signals}
        columns={["timestamp", "symbol", "side", "strategy", "confidence", "news_score"]}
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
        <h3>Performance Chart (Portfolio Value)</h3>
        <PnlMiniChart history={pnlHistory} />
      </div>
    </div>
  );
}
