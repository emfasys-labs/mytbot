# Capital control — wiring into the redesign shell

Two files to touch in `ui/src/app/redesign/`:

## 1. Add `capital.tsx`
Copy `capital.tsx` from this folder into `ui/src/app/redesign/capital.tsx`. It
imports only from `./primitives`, `./tokens`, `./data`, and `./useLiveSystem` —
no new deps.

## 2. Wire `CAPITAL_KEYFRAMES` into the global style block
In `App.tsx`, append `CAPITAL_KEYFRAMES` to the existing `<style>` string (same
block that hosts `ds-fade-out-slow`, `ds-slide-in`, etc.):

```tsx
import { CAPITAL_KEYFRAMES } from './capital';
// ...
<style>{`
  ${EXISTING_KEYFRAMES}
  ${CAPITAL_KEYFRAMES}
`}</style>
```

## 3. Mount `<CapitalPanel />` in the dashboard
In `dashboard.tsx`, the current right-rail has `Conviction river` + `Live feed`.
Swap the conviction card for a tabbed column, or add a new row below the hero:

```tsx
import { CapitalPanel } from './capital';
// inside DashboardScreen, below the hero Card:
<CapitalPanel live={live} accent={accentColor} />
```

Recommended layout: give capital its own full-width row between the hero NAV
card and the conviction/live-feed row. The slider wants vertical space (~380px)
and benefits from sitting directly under NAV so the deployed line feels tied to
the account figure above it.

## 4. Mount `<KillSwitchButton />` in the top bar
In `shell.tsx`, the top bar currently has the mode selector and start/stop.
Add the kill control to the right cluster:

```tsx
import { KillSwitchButton } from './capital';
// in the top-bar right cluster:
<KillSwitchButton live={live} />
```

It is deliberately read-only until the backend ships `POST /system/kill` — when
`live.killSwitch` is true the button shows the engaged state and pulses; when
running it routes tap → `live.stop()` for a graceful halt.

---

## Backend endpoints touched

| Action                  | Endpoint                         | Status     |
| ----------------------- | -------------------------------- | ---------- |
| Commit ceiling (drag up)| `PUT /system/capital-allocation` | ✅ shipped |
| Lower ceiling (trim)    | `PUT /system/capital-allocation` | ✅ shipped |
| Force-close per symbol  | `POST /positions/{id}/close`     | ❌ missing |
| Flatten book            | `POST /positions/flatten`        | ❌ missing |
| Kill switch             | `POST /system/kill`              | ❌ missing |
| Graceful halt           | `POST /system/stop`              | ✅ shipped |

The component is truthful about the gaps — trim shows "engine unwinds on its
own signals, not a force-close", flatten shows a "backend pending" banner. When
the two missing endpoints ship, only `confirmTrim`, `confirmFlatten`, and
`KillSwitchButton.handle` need rewiring; the UX stays identical.

---

## One `useLiveSystem` tweak I recommend

Current `setCapitalPct` optimistically updates local state and silently
swallows errors. Suggested shape:

```ts
const setCapitalPct = useCallback(async (p: number) => {
  const c = Math.max(0, Math.min(1, p));
  const prev = capitalPct;
  setCapitalPctState(c);
  try { localStorage.setItem('mytbot_capital_pct', String(c)); } catch {}
  try {
    const r = await api.setCapitalAllocation(c);
    if (typeof r.capital_pct === 'number') setCapitalPctState(r.capital_pct);
  } catch (err) {
    setCapitalPctState(prev);               // revert
    try { localStorage.setItem('mytbot_capital_pct', String(prev)); } catch {}
    throw err;                               // let caller show toast
  }
}, [capitalPct]);
```

`CapitalPanel` already treats `setCapitalPct` as throwing; the `catch` in
`commitCeiling` swallows cleanly. Adding error surfacing at the toast layer is
a separate follow-up.
