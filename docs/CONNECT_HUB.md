# Connect Hub

Connect Hub is the adaptive onboarding and capability layer for myTbot. It
answers one question for the core and the dashboard:

> What external systems does this operator actually have connected right now?

It covers four connector classes:

- trading platforms / brokers
- news, macro, and information feeds
- local or paid AI providers
- treasury or funding accounts

Connector categories are not equal:

- **Trading platforms** are peer venues. Adding/removing one changes venue
  coverage, not the core system.
- **Information feeds** are peer evidence sources. More feeds enrich signals;
  fewer feeds reduce coverage.
- **AI pipeline components** are layered, not peer connectors. The deterministic
  rules layer is a core baseline and cannot be disabled. Optional model layers
  sit above it: sentiment, local reasoning, and premium escalation.
- **Treasury accounts** are funding sources and require explicit allocation and
  approval policy before any automatic movement is allowed.

The system must work when a user has one broker, no treasury account, one free
news feed, and no paid AI. It must also work when another user has multiple
brokers, premium feeds, several LLMs, and a governed treasury account.

## Runtime API

- `GET /connect/hub`
- `GET /system/status` includes the same payload as `connect_hub`
- `POST /connect/add`
- `POST /connect/configure`
- `POST /connect/enable`
- `POST /connect/delete`

The payload is read-only and secret-safe. It reports whether required
environment variables are configured, but never returns secret values.

`POST /connect/configure` is the Connect Wizard save endpoint. It is guarded by
the same `X-Control-Token` / `API_CONTROL_TOKEN` protection as other mutating
control-plane routes. It accepts only environment variables declared by the
selected connector manifest and writes them to the local `.env`; secret values
are never echoed back in the response.

`POST /connect/add` is the Add Connector wizard endpoint. It creates a manifest
row for a new broker, feed, AI provider, or treasury account. For brand-new
brokers it can also scaffold `brokers/<id>/adapter.py` from the broker template.
The scaffold is not auto-registered for live trading; the adapter methods must
be implemented first.

`POST /connect/enable` toggles a connector. Disabling a broker also applies an
immediate risk-layer broker gate so new orders cannot route there while the
running process is still alive. The manifest change persists across restart.
If a broker has open exposure, disabling is treated as "block new routing" and
the operator should use a dedicated flatten/disconnect workflow before final
removal.

`POST /connect/delete` removes a connector from Connect Hub. It deliberately
does not erase `.env` secrets; credentials are left for manual audit/removal.
For brokers, delete also applies the same immediate risk-layer broker gate.

## Connector Manifests

Connector manifests live in `config/connectors.yaml`.

Each connector declares:

- `category`
- `auth_type`
- `required_secrets`
- `capabilities`
- `roles`
- `safety`
- `docs_url`
- `notes`

Example:

```yaml
brokers:
  alpaca:
    label: Alpaca
    adapter: alpaca
    enabled: true
    auth_type: api_key
    required_secrets:
      - env: ALPACA_API_KEY
      - env: ALPACA_API_SECRET
    capabilities:
      can_trade: true
      can_read_balance: true
      supports_equities: true
```

## Adding a Broker

1. Implement the normal broker adapter in `brokers/<name>/adapter.py`.
2. Add it to `brokers/registry.py`.
3. Add a manifest row under `config/connectors.yaml::brokers`.
4. Add required env vars to `.env`.
5. Start `python run.py`.

Connect Hub will show whether it is configured, connected, healthy, and usable
for trading. Strategies, risk, allocator, and execution should still talk only
through the existing broker abstraction.

## Adding an Information Feed

Feeds should normalize external content into standard events with:

- source
- title/headline
- timestamp
- affected symbols
- credibility/materiality scores when available
- raw text or URL
- latency/cost metadata when available

Future feed adapters should implement `connectors.base.InformationFeedAdapter`.

## Adding an AI Provider

AI providers remain advisory only. They may classify, score, explain, arbitrate,
or escalate decisions, but they must not place orders or bypass risk.

Future AI connectors should implement `connectors.base.AIProviderConnector` and
map themselves to roles such as:

- `fast_classifier`
- `sentiment_classifier`
- `reasoning_model`
- `premium_arbiter`
- `emergency_model`
- `fallback_model`

## Adding Treasury

Treasury is intentionally conservative.

Current state:

- balance/read metadata can be represented
- transfer capability can be declared
- automatic movement is not implemented
- default transfer quotes are approval-only and route-disallowed

Before any treasury movement exists, the system needs:

- beneficiary whitelist
- per-route limits
- daily transfer limits
- manual approval threshold
- loss/cooldown gates
- full audit trail
- dry-run transfer quotes
- explicit operator arming

Future treasury connectors should implement `connectors.base.TreasuryAdapter`.

## Dashboard

The redesign UI has a **Connect** screen. It shows:

- category summary
- connector status
- capabilities
- roles
- missing environment variables
- next action
- treasury safety state

The UI adapts to the manifest and runtime status; it should not assume that any
specific broker, feed, AI provider, or treasury account exists.

Each connector card has a **Configure** action. The wizard:

1. Shows only the credential fields declared by the manifest.
2. Accepts an optional control token when the API is locked down.
3. Saves values into `.env`.
4. Enables the manifest where applicable.
5. Enables matching `config/ai.yaml` provider rows for AI connectors where a
   provider id already exists.
6. Refreshes Connect Hub and returns the next step.

For long-lived runtime objects such as broker adapters and AI routers, a backend
restart or system stop/start may still be required after saving credentials.

The screen also has **Add connector**:

1. Choose broker/feed/AI/treasury.
2. Enter display name and connector id.
3. Enter required environment variable names.
4. Save.

For a new broker, this creates:

- a Connect Hub manifest row
- a scaffolded adapter folder under `brokers/<id>/`
- a next-step message to implement the adapter and register it

That means a new platform can be represented and tracked immediately, but live
trading still requires an adapter that translates the broker's native API into
`brokers/base.py`.

Each connector card also has:

- **Enable / Disable** — toggles the manifest and, for brokers, immediately
  blocks or unblocks new routing at the risk layer.
- **Delete** — removes the connector from Connect Hub after confirmation.
  Credentials remain in `.env`; delete is about system participation, not
  silently destroying secrets.

Core AI pipeline components, such as the Rules engine, are shown as always-on
and do not expose disable/delete controls.

---

# Connect Hub v2 — Design (D127)

D107 delivered the read-only inventory slice. v2 takes it from *inventory* to
*lifecycle*: live connection tests, detected (not just declared) capabilities,
certification tiers that gate execution, a managed AI pipeline, and a
single governed treasury slot. This section is the accepted design; it is
implemented in phases (see "Build phases" below).

## D107 baseline vs v2 scope

| Built (D107) | Added by v2 |
|---|---|
| `connectors.yaml` catalogue | Per-install `connector_state` table |
| Read-only `/connect/hub` snapshot | Lifecycle state machine + Test/Enable/Disable wired end to end |
| Adapter ABCs in `connectors/base.py` | Capability **detection** (test-derived) |
| Configure wizard writes `.env` | Certification tiers (Certified executes, Experimental only informs) |
| AI rows from `ai.yaml` | AI pipeline as managed stages: local-LLM catalogue, machine probe, FinBERT versioning |
| Treasury row, disabled | Treasury singleton + approval-workflow design |

v2 extends D107; it does not replace it.

## Core principles

1. **Curated, not arbitrary.** Users pick only from the tested catalogue in
   `connectors.yaml`. New connectors are added over time, after testing. No
   unknown broker or treasury APIs, ever.
2. **Capability-gated.** What a connector may do = (manifest declares) ∩
   (live test detected) ∩ (certification tier permits).
3. **Three certification tiers:**
   - **Certified** — may execute (place trades; later, move treasury cash).
   - **Experimental** — may *inform* only (advisory scoring, balance reads).
     Never moves money or places orders.
   - **Unsupported** — not in the catalogue; cannot be added.
4. **Graceful degradation.** The one-button core must run on the minimum:
   one broker, no treasury, no paid AI. Everything else is optional except
   the non-removable AI core.
5. **Secrets never leave `.env`.** The API reports only whether a credential
   is configured (D107 rule, retained).

## Category shapes

The four categories are **not** modelled identically:

- **Trading platforms** — a collection: add/remove N. Broker-agnostic via the
  frozen `brokers/base.py` interface.
- **News & information** — a collection: add/remove N. All feeds are
  Experimental tier by definition (they inform, never execute).
- **AI Pipeline** — **4 fixed stages**, not an add-list: Rules, FinBERT,
  Local LLM, Premium LLM. Configured/enabled/disabled/versioned, never
  added or removed.
- **Treasury** — a **singleton**: 0 or 1. Treasury is the capital
  source-of-truth; multiple accounts create liquidity / settlement /
  double-funding ambiguity.

## Connector lifecycle state machine

```
not_configured ─Configure─▶ needs_credentials ─creds saved─▶ testing
testing ─pass─▶ connected          testing ─partial─▶ connected_limited
testing ─fail─▶ error              connected ─Disable─▶ disabled
disabled ─Enable─▶ testing         connected ─live mode, paper-only─▶ unsupported_in_live
```

| State | Meaning |
|---|---|
| `not_configured` | In catalogue, never set up |
| `needs_credentials` | Configured but a required `.env` var is missing |
| `testing` | Live connection test running |
| `connected` | Authenticated, all declared capabilities verified |
| `connected_limited` | Authenticated but a capability failed (e.g. balance OK, trade perm missing) |
| `disabled` | Operator-disabled; retained, not deleted |
| `error` | Test failed (bad key, venue down) — carries a reason string |
| `unsupported_in_live` | Paper-only connector while the system is in live mode |

## Capability detection & certification

**Declared** capabilities live in the `connectors.yaml` manifest — the static,
version-controlled ceiling. **Detected** capabilities come from a live
`Test connection` per category:

- *Brokers* — auth → read balance → read account permissions → confirm
  paper/live. Detects real `can_trade` / `can_read_balance` / `can_withdraw`.
- *Feeds* — auth → fetch a sample → confirm it parses into `InformationEvent`.
- *AI providers* — run a fixed structured-output prompt → require schema-valid
  JSON → measure latency.
- *Treasury* — read balance → confirm transfer endpoints are disabled.

**Effective capability = Declared ∩ Detected ∩ Tier.** A connector that
declares `can_trade` but is Experimental tier, or whose live test failed, does
not get `can_trade`. Tier is set in the manifest by the myTbot team after
testing; the user never sets it.

## AI Pipeline (the managed stages)

The pipeline is the existing local-first escalation chain
(`ai/router.py`, `ai/escalation.py`): **Rules → FinBERT → Local LLM →
Premium LLM**. Connect Hub manages the stages; it does not re-architect the
chain.

| Stage | Delete | Disable | Versioning |
|---|---|---|---|
| Rules Engine | never | never (core) | ships with app |
| FinBERT | never | only if another sentiment provider is active | curated, version-pinned checkpoint |
| Local LLM | slot persists | yes | model catalogue |
| Premium LLM | slot persists | yes | provider + model id |

"Disable yes, delete no" falls out naturally: these are pipeline *stages*,
not connectors — a stage cannot be deleted.

### FinBERT versioning

FinBERT is registered in `config/model_registry.yaml` like any trained model
(`finbert_sentiment` entry: version, checksum, validation report). An
operator-initiated **Update** action downloads the new checkpoint from a
curated URL, verifies the checksum, runs a sentiment smoke test against a
fixed labelled set (must clear an accuracy floor), atomically swaps, and keeps
the prior version for one-click rollback. Never auto-updates silently.

### Local LLM — curated catalogue + machine probe

On first run the app probes the machine (CPU cores, RAM, GPU, VRAM, disk,
Ollama presence) and recommends the best-fit model from the **supported
catalogue** (`qwen2.5:7b`, `llama3.1:8b`, `mistral:7b`, …). A model becomes
`Active` only after a **compatibility certification**:

1. Download via Ollama.
2. JSON-mode test — the real news-scoring prompt must return schema-valid
   structured output.
3. Latency test — must score a news item under threshold or it is flagged
   `too slow for live use`.
4. Schema-conformance test over a fixed set.

New model releases enter the catalogue only after the myTbot team runs them
through the cert suite — that is how new versions are handled. On a machine
that fails the probe, the Local LLM is skipped and the pipeline runs on
Rules + FinBERT (+ Premium if configured); the system stays fully functional.
A custom (non-catalogue) Ollama model may be allowed later as **Experimental**
— advisory only, must pass the cert suite, and cannot be the sole reasoning
provider in live mode.

### Premium LLM

Provider-based, from a supported list: Anthropic Claude, OpenAI, Google
Gemini, Azure OpenAI, or a custom OpenAI-compatible endpoint. Configure =
pick provider → pick model id → enter API key → compatibility test
(structured-output + latency). A custom endpoint is acceptable here because
the premium LLM only advises — it never executes. Escalation timing is
governed by `ai/escalation.py`.

## Treasury

- **Singleton** — Connect Hub enforces 0-or-1 treasury connector.
- **v1: read-only** — display balance as capital source-of-truth context.
  No movement; the manifest pins `transfer_execution_default: disabled`.
- **v2: approval-gated transfers** — designed now so v1 does not box us in:
  auto-top-up off by default, manual approval per transfer, max transfer/day,
  minimum treasury reserve (never swept), beneficiary whitelist (pre-approved
  broker destinations only), every request audited. Treasury execution is
  Certified-tier-only and *still* requires manual approval — no tier ever
  gets silent cash movement.

## Data model

| Layer | Store |
|---|---|
| Catalogue (supported list) | `config/connectors.yaml` — non-secret, version-controlled |
| Secrets | `.env` — never DB, never API-echoed |
| Per-install state | new `connector_state` table — enabled, last_test_at, last_test_result, detected_capabilities, certification_tier, ai_model_version, local_model_install_state, machine_probe |
| Live runtime status | merged at snapshot build from `BrokerManager`, ingest telemetry, `ai.yaml` |

One install = one user (downloadable personal app, no multi-tenancy), so
per-install state needs no `user_id`.

## API surface (extends `/connect/hub`)

```
GET   /connect/hub                          # snapshot (exists)
POST  /connect/{category}/{id}/configure    # write .env creds (exists, extend)
POST  /connect/{category}/{id}/test         # live capability probe → detected caps
POST  /connect/{category}/{id}/enable
POST  /connect/{category}/{id}/disable
GET   /connect/ai/local/catalogue           # supported local models + machine fitness
POST  /connect/ai/local/install             # download + cert a catalogue model
POST  /connect/ai/local/activate
POST  /connect/ai/finbert/update            # versioned FinBERT update + smoke test
GET   /connect/machine-probe                # CPU/GPU/RAM capability report
POST  /connect/treasury/configure           # singleton-guarded
```

Every mutating call returns the refreshed connector state for an atomic UI
update.

## UI / UX

The Connect screen has four sections matching the categories.

- **Trading & News** — a card grid plus an **Add connector** wizard:
  choose category → choose supported provider → capability preview → enter
  credentials → Test → detected-capabilities result → Save.
- **AI Pipeline** — *not* an add-list. Four fixed stage cards in pipeline
  order, each with enable/disable (where allowed), version info, and
  Configure. The Local LLM card opens the model catalogue sub-screen with a
  per-machine fitness column; the FinBERT card shows version + "Update
  available".
- **Treasury** — a single card; Configure is singleton-guarded; read-only
  balance once connected; approval settings panel inert until v2.

Every card carries a state chip, capability badges, a certification-tier
badge, and a contextual next-action. A first-run onboarding wizard walks a
new user through: add ≥1 broker → optionally add feeds → AI auto-configures
(Rules + FinBERT on; Local LLM auto-installs if the probe passes; Premium
optional) → Treasury optional → dashboard. The system must be launchable
with a single paper broker.

## Security & safety

- Secrets only in `.env`; the API returns `configured: true/false` only.
- `can_withdraw` is hard-wired `false` for every broker manifest.
- Certification tier gates execution; Experimental connectors physically
  cannot reach the order path or treasury movement.
- Paper-only connectors surface `unsupported_in_live` in live mode and are
  excluded from the live execution set.
- Treasury transfers (v2) always require manual approval + whitelist +
  daily cap + reserve floor.
- Every Test / Configure / Enable / Disable action is audit-logged.

## Build phases

| Phase | Scope | Status |
|---|---|---|
| P1 | `connector_state` table + lifecycle state machine + Test endpoints + capability detection | ✅ implemented |
| P2 | Certification tiers wired into execution gating; live-mode guard | ✅ implemented |
| P3 | AI Pipeline: 4 stage cards, enable/disable rules, FinBERT versioning | ✅ implemented |
| P4 | Local LLM: machine probe + catalogue + install/cert + graceful fallback | ✅ implemented |
| P5 | Premium LLM provider picker + compatibility test | ✅ implemented |
| P6 | First-run onboarding wizard | ✅ implemented |
| P7 | Treasury v2 approval workflow | ⏸ deferred to its own project |

P1–P6 are all read-only inventory / config / advisory-gating. P7 is
categorically different — it moves real cash — and is deferred to a
dedicated, carefully-scoped project after a paper soak (see DECISIONS
D127, open decision #4). Until then the treasury connector stays
read-only: a usable capital reference with no movement.

The P1–P6 **redesign UI** is implemented in `ui/src/app/redesign/
screens.tsx` (`ConnectScreen`):
- broker/feed cards carry a **Test** button + certification badge;
- the AI Pipeline renders as four ordered **stage cards** with the
  per-stage disable rules and no delete control;
- the **Local LLM catalogue** modal (machine probe + per-model fitness
  + install/activate) and the **Premium provider picker** modal
  (select provider, save credentials, test, activate);
- a first-run **onboarding panel** banner driven by
  `GET /connect/onboarding`.

Connect Hub v2 (P1–P6) is therefore complete end to end — backend and
UI. P7 (treasury cash movement) remains a separate future project.

## Open decisions

1. Local LLM on weak machines — silent skip (recommended) vs. prompt each launch.
2. Custom local/premium models — ship the Experimental escape hatch in v1 or stay catalogue-only.
3. FinBERT updates — operator-initiated only (recommended) vs. an auto-update toggle.
4. Treasury v2 timing — in this feature's scope or deferred to its own project.
