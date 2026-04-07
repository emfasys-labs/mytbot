# Incident Response Runbook

Scope: mytbot runtime incidents (broker disconnects, order failures, data/API outages).

## 1) Triage
- Confirm incident start time and impacted component (`run_m5`, `run_pipeline`, API, broker).
- Classify severity:
  - SEV1: Live trading risk (unexpected live orders, kill switch ignored).
  - SEV2: Trading halted or order placement/fill tracking broken.
  - SEV3: Dashboard/API degradation with no trading impact.
- Capture current context: `APP_ENV`, active broker(s), last successful order/fill.

## 2) Immediate Containment
- If risk to capital exists, trigger kill switch via API/control plane.
- Stop autonomous loop if needed: halt `run_m5`.
- Keep evidence: do not delete logs or DB rows.

## 3) Evidence Collection
- Save relevant logs around incident window from runner + API.
- Capture database snapshots for `signals`, `orders`, `positions`, `daily_pnl`.
- Record broker-side evidence (IBKR/TWS, exchange order history) for matching IDs.

## 4) Recovery
- Validate required env vars and service dependencies.
- Restart dependency stack in order: Postgres/Redis -> API -> runners.
- Run a paper smoke cycle before re-enabling live mode.

## 5) Postmortem
- Document root cause, user impact, and exact remediation.
- Add regression test for discovered failure mode.
- Update `docs/DECISIONS.md` and this runbook if process changed.
