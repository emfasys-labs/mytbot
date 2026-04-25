# mytbot Trading App UI Kit

High-fidelity recreation of the mytbot production dashboard (`ui/` React app).

## Design language
- Dark-only: `#0a0a0a` background, panels at `rgba(255,255,255,0.02)`
- Semantic color system: emerald (active/safe), amber (caution), rose (danger/kill), blue (loading)
- Typography: Inter for UI copy, JetBrains Mono for all data/scores/prices
- Lucide icons only — `Power` + `Loader2` — 14px, strokeWidth 2.3
- Motion: Framer Motion, spring stiffness 400 / damping 30, stagger 0.03s

## Components in index.html
- `NewsTicker` — horizontal scrolling news bar with sentiment dots
- `LiveStrip` — NAV headline, P&L (today/week/month), system state, MasterControl
- `MasterControl` — tap/hold power button with state-aware glow + arm-to-stop slider
- `ModeSelector` — trader / hunter / sentinel risk mode tabs
- `SignalBrain` — conviction scores (bullish/bearish) + live event log
- `AllocationCenter` — capital/targets, opportunities table, hold pressure, next actions
- `PerformancePanel` — mini equity chart + period P&L
- `RiskGate` — approved/rejected signal list with reason humanization
- `OpportunityTicker` — bottom ticker with ranked opportunities
- `PositionChips` — rounded position badges with % change
- `SystemHeartbeat` — live pulse indicator

## States demoed
System can be in: `off`, `starting`, `running`, `stopping`. Click the power button to transition.
MasterControl: tap = pause/resume, hold 800ms = arm-to-stop (drag down to confirm stop).
