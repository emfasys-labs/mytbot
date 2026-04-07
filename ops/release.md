# Release Runbook

## Pre-release Checklist
- Update docs status if milestone state changed.
- Run dev bootstrap once on clean env:
  - `python scripts/setup_dev.py`
- Run release gate:
  - `python scripts/release_gate.py`
- Verify no accidental secrets in staged files.

## Release Steps
1. Ensure branch is up to date with target base.
2. Confirm `pytest` passes and docs consistency check passes.
3. Tag/version according to project convention.
4. Deploy services in this order:
   - Data dependencies (Postgres/Redis)
   - API
   - Runners
5. Observe first cycle in paper mode (or smallest live sleeve if approved).

## Post-release Verification
- `GET /healthz` and `GET /readyz` respond healthy.
- Dashboard updates with fresh signal/order timestamps.
- Broker connectivity confirmed for enabled brokers.
- Telegram alerts show expected startup/trade flow only.

## Rollback Trigger
- Any SEV1 behavior or repeated SEV2 failures in first observation window.
- Follow `ops/rollback.md`.
