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
