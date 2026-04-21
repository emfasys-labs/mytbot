# mytbot — "living instrument" redesign shell

Ported from the Claude Design handoff bundle (`mytbot-design-system/project/prototypes/redesign/`).
This is a full app reimagination landed on during the design chat: sidebar +
command palette (⌘K) + six screens (Dashboard, Signals, Book, Risk, Strategies,
Trade log) + desktop/tablet/mobile viewports + a live tweaks panel (accent,
density, system state, theme, viewport).

## Files

- `tokens.ts` — colour/type/motion tokens and accent palette
- `data.ts` — fake demo data (symbol conviction, positions, events, etc.)
- `primitives.tsx` — `Glyph`, `Wordmark`, `NavNumber`, `Card`, `Label`, `Pill`,
  `Signed`, `Spark`, inline icon set `I`
- `shell.tsx` — `Sidebar`, `TopBar`, `MasterButton` (tap = toggle, hold 0.9s =
  arm to stop), `CmdPalette`, `TweaksPanel`
- `dashboard.tsx` — hero NAV with digit color-flash, conviction river, live
  feed, book strip
- `screens.tsx` — Signals / Book / Risk / Strategies / Trade log tables
- `mobile.tsx` — thumb-reachable companion with anchored kill switch
- `App.tsx` — root: state, routing, keybinds, overlays (armed, error)

## Mounting

`ui/src/main.tsx` mounts this redesign by default. Add `?legacy=1` to the URL
to load the previous production shell (`ui/src/app/App.tsx`) unchanged.

Global keyframes and the `ds-root` class live in `ui/src/styles/design-system.css`.

## Data

The prototype runs on `data.ts` demo values — real-API wiring (REST +
WebSocket) is already implemented in the legacy shell. Hooking each redesign
panel (NAV, conviction, live feed, book strip, risk gate) to
`api.getDashboardSnapshot`/`getPnl`/WS `tick` events is the logical next step
once the layout is approved.

## Keybinds

- `⌘/Ctrl-K` — open command palette
- `Esc` — close palette / disarm
