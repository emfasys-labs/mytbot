const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

function headers(includeToken = false) {
  const h = { "Content-Type": "application/json" };
  if (includeToken) {
    const token = localStorage.getItem("controlToken") || "";
    if (token) h["X-Control-Token"] = token;
  }
  return h;
}

export async function getJson(path) {
  const r = await fetch(`${API_BASE}${path}`);
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
  return `${base}/ws`;
}
