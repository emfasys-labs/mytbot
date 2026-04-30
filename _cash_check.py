import json, urllib.request
from portfolio.global_edge_coordinator import cash_factor_for_asset_class as cf

d = json.loads(urllib.request.urlopen("http://localhost:8000/dashboard/snapshot").read())
held = (d.get("global_edge", {}) or {}).get("held_edges", [])
nav = float((d.get("portfolio", {}) or {}).get("nav") or 0)
total_n = 0.0
total_c = 0.0
print(f"{'symbol':14} {'broker':8}  {'notional':>12}  {'factor':>6}  {'cash':>12}")
for h in held:
    n = float(h.get("notional") or 0)
    ac = (h.get("metadata") or {}).get("asset_class") or ""
    sym = h.get("symbol") or ""
    f = float(cf(ac, symbol=sym))
    cash = n * f
    total_n += n
    total_c += cash
    print(f"  {sym:14} {h.get('broker',''):8}  ${n:>10,.0f}  {f:>6.2f}  ${cash:>10,.0f}")
print(f"\nNAV=${nav:,.0f}")
print(f"NOTIONAL total=${total_n:,.0f}  ({total_n/nav*100:.1f}%)")
print(f"CASH     total=${total_c:,.0f}  ({total_c/nav*100:.1f}%)")
print(f"iter={d.get('loop_iteration')}  updated={d.get('updated_at')}")
