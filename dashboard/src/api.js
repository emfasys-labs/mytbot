const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

const LS_DASH = "dashboardReadToken";
const LS_CONTROL = "controlToken";

export function getApiBase() {
  return API_BASE;
}

function dashboardHeaders() {
  const h = {};
  const dt = localStorage.getItem(LS_DASH);
  if (dt) h["X-Dashboard-Token"] = dt;
  return h;
}

function headers(includeControl = false) {
  const h = { "Content-Type": "application/json", ...dashboardHeaders() };
  if (includeControl) {
    const token = localStorage.getItem(LS_CONTROL) || "";
    if (token) h["X-Control-Token"] = token;
  }
  return h;
}

export async function postDashboardLogin(password) {
  const r = await fetch(`${API_BASE}/auth/dashboard/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!r.ok) throw new Error(`login failed (${r.status})`);
  return r.json();
}

export async function getJson(path) {
  const r = await fetch(`${API_BASE}${path}`, { headers: dashboardHeaders() });
  if (r.status === 401) {
    const err = new Error("unauthorized");
    err.status = 401;
    throw err;
  }
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return r.json();
}

export async function postJson(path, body) {
  const r = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`${path} failed (${r.status})`);
  return r.json();
}

export function wsUrl() {
  const base = API_BASE.replace(/^http/, "ws");
  const tok = localStorage.getItem(LS_DASH);
  const q = tok ? `?token=${encodeURIComponent(tok)}` : "";
  return `${base}/ws${q}`;
}

export { LS_DASH, LS_CONTROL };
