# mytbot — "living instrument" redesign shell

Ported from the Claude Design handoff bundle (`mytbot-design-system/project/prototypes/redesign/`)
and fully wired to the live trading backend.

Sidebar + command palette (⌘K) + six screens (Dashboard, Signals, Book, Risk,
Strategies, Trade log) + desktop/tablet/mobile viewports + a live tweaks panel
(accent, density, theme, viewport).

## Files

- `tokens.ts` — colour/type/motion tokens and accent palette
- `data.ts` — shared TypeScript view models (Conviction, Position, LiveEvent…)
- `mapping.ts` — adapters from live API shapes to view models
- `useLiveSystem.ts` — HTTP polling + WebSocket aggregation hook exposing `LiveData`
- `primitives.tsx` — `Glyph`, `Wordmark`, `NavNumber`, `Card`, `Label`, `Pill`,
  `Signed`, `Spark`, inline icon set `I`
- `shell.tsx` — `Sidebar`, `TopBar`, `MasterButton` (tap = start/stop, hold 0.9s =
  arm to stop), `CmdPalette` (nav + start/stop + mode), `TweaksPanel`
- `dashboard.tsx` — hero NAV with digit color-flash, conviction river, live
  feed, book strip
- `screens.tsx` — Signals / Book / Risk / Strategies / Trade log
- `mobile.tsx` — thumb-reachable companion with anchored kill switch
- `App.tsx` — root: state, routing, keybinds, overlays (armed, error)

## Mounting

`ui/src/main.tsx` mounts this redesign by default. Add `?legacy=1` to the URL
to load the previous production shell (`ui/src/app/App.tsx`) unchanged.

Global keyframes and the `ds-root` class live in `ui/src/styles/design-system.css`.

## Live wiring

Every screen reads from the same HTTP + WS surface as the legacy shell:

| Redesign surface            | API source                                       |
| --------------------------- | ------------------------------------------------ |
| NAV, today/week/month P&L   | `GET /pnl`                                       |
| Equity curve                | `GET /pnl/history`                               |
| Exposure ring / gauges      | `GET /dashboard/snapshot` (`portfolio.*`)        |
| Conviction river            | `GET /dashboard/snapshot` (`accumulator`, `opportunities`) |
| Positions / Book            | `GET /positions`, `portfolio.*`                  |
| Approved / rejected signals | `GET /intelligence/signals`                      |
| Risk gauges                 | snapshot + metrics                               |
| Strategies mix              | derived from `snapshot.opportunities`            |
| Trade log                   | `GET /orders`                                    |
| Brokers                     | `GET /system/status`                             |
| Live feed                   | WebSocket `tick` frames + order stream           |
| System state                | `GET /system/status` + `WS system.state`         |
| Loop iteration / path       | `snapshot.loop_iteration`, `snapshot.path`       |

Actions (master button, ⌘K palette) call `POST /system/start`,
`POST /system/stop`, `POST /system/mode`.

When the backend reports `state ≠ running`, snapshots/positions/orders are
cleared so the UI never reads stale-looking "live" data.

## Keybinds

- `⌘/Ctrl-K` — open command palette (Enter to run top match)
- `Esc` — close palette / disarm
