import json, urllib.request
d = json.loads(urllib.request.urlopen("http://localhost:8000/dashboard/snapshot").read())
p = d.get("portfolio", {}) or {}
nav = float(p.get("nav") or 0)
ge = float(p.get("gross_exposure") or 0)
pct = (ge / nav * 100) if nav else 0
print(f"NAV={nav:,.0f}  Gross={ge:,.0f}  Deployed={pct:.2f}%  iter={d.get('loop_iteration')}")
ep = d.get("execution_plan", {}) or {}
ins = ep.get("instructions", [])
opens = [i for i in ins if i.get("action") == "open_strategy"]
trims = [i for i in ins if i.get("action") == "trim_symbol"]
print(f"plan: opens={len(opens)} trims={len(trims)} mode={ep.get('mode')}")
ge_blk = d.get("global_edge", {}) or {}
held = ge_blk.get("held_edges", [])
print(f"held_edges={len(held)}")
for h in held[:40]:
    print(f"  {h.get('symbol')} {h.get('broker')} ${float(h.get('notional') or 0):,.0f}")
print(f"updated_at={d.get('updated_at')}")
