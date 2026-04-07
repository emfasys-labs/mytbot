# Rollback Runbook

Use when a deployment or config change causes regressions.

## 1) Decide Rollback Scope
- Code rollback (git commit/version).
- Config rollback (`.env`, `config/*.yaml`).
- Database rollback (migration or data correction).

## 2) Safe Order
1. Pause runners (`run_m5`, `run_m3`, `run_pipeline` loops).
2. Confirm no in-flight critical actions (open order reconciliation done).
3. Apply rollback artifact.
4. Validate startup checks and service health.
5. Resume in paper mode first.

## 3) Code Rollback
- Checkout/restore known-good commit.
- Reinstall dependencies if lock/requirements changed.
- Run `python scripts/release_gate.py --quick`.

## 4) Config Rollback
- Restore known-good `.env` and YAML configs from secure backup.
- Verify broker API keys and paper/live toggles.
- Re-run startup validation by launching each service once.

## 5) Database Rollback
- Prefer forward-fix migrations.
- Only downgrade Alembic when tested and approved.
- Always snapshot DB before schema rollback.

## 6) Exit Criteria
- Health endpoints healthy.
- One paper trade cycle completes end-to-end.
- No new critical alerts for at least one loop interval.
