# mytbot Design System

**myTbot** is an open-source autonomous multi-asset trading system under AGPL v3: equities, bonds, ETFs, forex, and crypto. It is published by Emfasys Labs, a division of Emfasys Ltd, and built as a single-operator control surface with transparent decision intelligence and risk governance. It is not investment advice, portfolio management, brokerage, custody, a managed trading service, a signal-selling service or a promise of performance.

**Sources used:**
- GitHub repo: https://github.com/kvcom/mytbot (private — `kvcom/mytbot`)
  - `ui/` — React (Vite + TypeScript + Tailwind) production dashboard
  - `dashboard/` — Legacy React dashboard (milestone M7)
  - `ui/src/styles/theme.css` — Tailwind CSS variable definitions
  - `ui/src/app/App.tsx` — Root layout + orchestration
  - `ui/src/app/components/` — All UI components

---

## CONTENT FUNDAMENTALS

**Voice & Tone**
mytbot speaks in terse, technical, operator-grade language. Every string earns its place on screen. There is no marketing copy, no onboarding fluff, no explanatory prose unless surfacing a live system event.

**Casing conventions**
- Section/panel labels: `UPPERCASE` with wide letter-spacing (e.g. `SIGNAL BRAIN`, `RISK GATE`, `NAV`)
- Data values: plain, no decoration — just the number or state word
- System state words: lowercase (`running`, `off`, `stopping`, `error`)
- Error/warning messages: sentence case, plain language, no exclamation points
- Time: relative (`4m`, `2h`, `3d`) or ISO for raw debug; never "3 minutes ago"

**Numerics**
- Financial values: `£12,345` or signed `+£12,345 / -£340`
- Scores: 2 decimal places, tabular-nums (`0.82 ↑`)
- Percentages: one decimal place maximum
- Tabular-nums always applied to any number that updates live

**Tone markers**
- Never emoji
- No exclamation marks
- Dots `·` as separators (not `|` or `/`)
- Arrows `↑ ↓` for direction, not icons
- The system says what happened, not how to feel about it

**Example strings from codebase:**
- `"No bullish edge in view."`
- `"Quiet — system not running."`
- `"Snapshot unavailable — fix read token (banner above) to load conviction."`
- `"24h discovery · signals 12 · anomalies 3"`
- `"Tradable £8,200 · 82%"`

---

## VISUAL FOUNDATIONS

### Color system
Dark mode only. Background near-black (`#0a0a0a`). All surfaces use translucent white on black. No colored backgrounds on panels — color is reserved for semantic meaning.

| Role | Value | Usage |
|---|---|---|
| Background | `#0a0a0a` | Page bg |
| Surface | `rgba(255,255,255,0.02)` | Cards/panels |
| Surface elevated | `rgba(255,255,255,0.04)` | Hover, nested |
| Border subtle | `rgba(255,255,255,0.05)` | Panel edges |
| Border medium | `rgba(255,255,255,0.10)` | Dividers, strip bg |
| Text primary | `#ffffff` / `rgba(255,255,255,0.90)` | Values, symbols |
| Text secondary | zinc-300 `#d4d4d8` | Supporting info |
| Text muted | zinc-500 `#71717a` | Labels, timestamps |
| Text dim | zinc-600 `#52525b` | Disabled / decorative |

**Semantic palette**
| State | Color | Token | Usage |
|---|---|---|---|
| Active / safe / profit | emerald-400 `#34d399` | `--color-active` | System running, approved signals, positive P&L |
| Active bg | `rgba(52,211,153,0.10)` | `--color-active-bg` | Pill/badge bg |
| Caution / risk rising | amber-400 `#fbbf24` | `--color-caution` | Stale snapshot, connecting broker |
| Caution bg | `rgba(251,191,36,0.10)` | `--color-caution-bg` | Amber badges |
| Danger / kill / loss | rose-400 `#fb7185` | `--color-danger` | Kill switch, rejected signals, drawdown |
| Danger bg | `rgba(251,113,133,0.10)` | `--color-danger-bg` | Red badges |
| Info / loading | blue-400 `#60a5fa` | `--color-info` | Starting state, loading |
| Info bg | `rgba(96,165,250,0.10)` | `--color-info-bg` | Blue badges |

### Typography
No custom fonts in the codebase; system sans-serif via Tailwind. Design system uses **Inter** (closest match to Tailwind default sans, clean technical feel) for UI copy, and **JetBrains Mono** for all data, scores, and numeric values.

- **Labels**: 10px, uppercase, tracking-widest (0.1em+), zinc-500 — `SIGNAL BRAIN`, `NAV`
- **Values/numbers**: 11–18px, JetBrains Mono, tabular-nums, white/90
- **Body**: 11–12px, Inter, zinc-300
- **Headlines (NAV)**: 18px, font-light, white — single large number

### Spacing & radii
- Panel border radius: `12px` (`rounded-xl`)
- Button/control border radius: `16px` (`rounded-2xl`)
- Chip/badge border radius: `9999px` (`rounded-full`)
- Base padding: `10px` on panels, `8px` for dense rows
- Gaps: `8px` tight, `12px` standard, `16px` loose

### Backgrounds
- No gradients except semantic: amber replacement view uses `from-amber-950/40 to-black/40`
- Panels: `bg-white/[0.02]` — nearly invisible tint, boundary defined by border only
- Glow effects: spread blur with RGBA matching state color — e.g. `rgba(74,222,128,0.22)` for active
- No images, no textures, no patterns — the data IS the background

### Animation
- Library: Framer Motion (`motion/react`)
- Entry: `opacity: 0 → 1`, `x: -4 → 0`, delay staggered at `0.03s` per item
- Spring: stiffness 400, damping 30 (snappy, not bouncy)
- Glow pulse: `opacity` oscillates `0.22 → 0.48 → 0.22`, 2.4s ease-in-out, infinite when live
- Busy spinner: `animate-spin` on Lucide `Loader2` icon
- No page transitions — the control surface is always on one screen

### Hover / press states
- Hover: `opacity` increase on interactive elements, no color shift
- Press/active state: color matched to system state (emerald/amber/rose)
- Long press to arm dangerous actions (800ms hold)
- Buttons: no box shadows, rely on bg opacity shift

### Borders & shadows
- All borders: white at low opacity (`/5`, `/10`)
- No `box-shadow` — glow is done with absolutely positioned blur divs
- No outer drop shadows on cards

### Layout
- Single-screen app: `min-h-screen`, `overflow-hidden`
- Vertical structure: `NewsTicker` → `LiveStrip` → `[SignalBrain | AllocationCenter | RiskGate]` → `OpportunityTicker`
- 3-column main at `lg` breakpoint: left sidebar (SignalBrain), center (AllocationCenter), right sidebar (RiskGate)
- Fixed chrome: tickers always at top/bottom, LiveStrip always at top
- CapitalSlider: absolutely positioned right edge at `xl`

### Transparency & blur
- Backdrop blur used on floating/tooltip elements: `backdrop-blur-xl`
- Tooltips: `bg-black/55` with `border-white/10`
- No frosted-glass panels; only tooltips and transient overlays

### Iconography
- Lucide icons only — stroke-weight 2 / 2.3, 14px for controls
- `Power` icon for MasterControl
- `Loader2` with `animate-spin` for loading state
- SVG inline for micro-icons (chevron down in armed state)

---

## ICONOGRAPHY

mytbot uses **Lucide React** (https://lucide.dev) exclusively. No icon font, no PNG icons, no emoji. Icon size is consistently `14px` at `strokeWidth={2.3}` for control elements. Icons are never used decoratively — each icon is functional and represents a specific action or state.

Icons in use:
- `Power` — MasterControl on/off button
- `Loader2` — loading/transitioning state (with `animate-spin`)
- Chevron SVG (inline, custom) — armed/stop slider indicator

CDN: `https://unpkg.com/lucide@latest` or via npm `lucide-react`.

---

## FILES INDEX

```
README.md                     ← This file
colors_and_type.css           ← CSS design tokens (colors, type, spacing)
SKILL.md                      ← Agent skill definition

assets/                       ← Visual assets (currently none — no logo/imagery in codebase)

preview/                      ← Design system cards for Design System tab
  colors-base.html
  colors-semantic.html
  colors-surfaces.html
  type-scale.html
  type-mono.html
  spacing-radii.html
  components-badges.html
  components-buttons.html
  components-panel.html
  components-livenumbers.html
  components-mastercontrol.html
  components-ticker.html

ui_kits/
  trading_app/
    README.md                 ← UI kit docs
    index.html                ← Interactive trading dashboard (main view)
    LiveStrip.jsx             ← NAV strip + MasterControl
    SignalBrain.jsx           ← Signal conviction panel
    AllocationCenter.jsx      ← Opportunities + hold pressure
    RiskGate.jsx              ← Risk approval/rejection panel
    NewsTicker.jsx            ← Scrolling news bar
    OpportunityTicker.jsx     ← Bottom opportunity ticker
```

---

## CAVEATS

- No logo or brand mark exists in the codebase — no SVG/PNG assets were found. The system has no visual identity beyond the UI itself.
- Fonts are substituted: **Inter** (for Inter/system-sans) and **JetBrains Mono** (for monospaced data). Originals are system fonts via Tailwind — no font files to copy.
- The legacy `dashboard/` uses different (simpler) styles than the production `ui/` — this design system reflects the production `ui/` codebase.
