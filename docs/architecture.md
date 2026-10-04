# Architecture — Routing Layer

**Scope:** the routing/billing/failover system: the legacy Node gateway and the
Rust `skidnir-gateway`, and the migration between them.

For non-negotiable rules, see [`invariants.md`](./invariants.md).
For why decisions were made, see [`DECISIONS.md`](./DECISIONS.md).
For what's live now, see [`ASBUILT.md`](./ASBUILT.md).

## 1. What the routing layer does

Every LLM request from Skidnir/Anton/Forge clients flows through the gateway:

```
client → Caddy (/v1/*) → gateway:8080 → auth → resolve model → reserve $ →
         dispatch to provider → settle $ → respond
```

The gateway is a **metered proxy**: it authenticates the client, picks the
upstream provider, reserves estimated cost from the client's ledger balance,
forwards the request, then settles the actual cost. It never generates tokens
itself.

Two implementations exist:
- **Legacy Node** (`services/gateway/server.js` + `model_router.js` + `token_ledger.js`)
  — currently serving production traffic.
- **Rust `skidnir-gateway`** (`services/skidnir-gateway/`, axum/tokio) — deployed
  on the VPS at `:8444` with **zero traffic**, awaiting cutover.

## 2. Why Node → Rust (migration rationale)

The full rationale is ADR R-001 in [`DECISIONS.md`](./DECISIONS.md). Summary:

1. **Memory safety at the billing boundary.** The gateway moves money. Rust's
   ownership model eliminates entire classes of bugs (use-after-free, data races
   on the ledger client) that are silent in Node.
2. **Latency budget.** Target: ≤15ms p99 added overhead. Rust measured 3–16ms
   at c1; Node is slower at every concurrency level.
3. **Key/host binding as a structural guarantee.** The D8 key-confusion bug
   (OpenRouter key sent to `api.anthropic.com`) was possible because Node's
   dynamic dispatch made it easy to pair the wrong key with the wrong host.
   Rust's type system + single choke point makes the correct pairing structural.

The migration is **parity-first**: the Rust gateway must be byte-identical to
Node on all 58 conformance cases before taking traffic. No behavior changes
during migration — those are follow-up work with their own ADRs.

## 3. Billing flow: reserve → dispatch → settle

```
1. AUTH      verify client key → tenant, balance
2. RESOLVE   model → provider, upstream URL, estimated cost
3. RESERVE   POST /internal/ledger/reserve {tenant, estimated_cents}
             → reservation_id (fail-closed: 503 if ledger unreachable)
4. DISPATCH  send to provider with provider's key
5. SETTLE    POST /internal/ledger/settle {reservation_id, actual_cents}
             → final balance
```

**Critical:** the Rust gateway never opens the ledger SQLite DB directly.
All money movement goes through the internal ledger API (`ledger.rs` is a
typed HTTP client). See invariant I-01.

**Settle failure mode (known):** if the ledger is unreachable at settle time,
the client receives the completion with `skidnir_billing: null` — wholesale
cost paid, nothing billed. This is fail-open and is being addressed (A9 M1:
alerting now, retry gated on ledger idempotency confirmation).

## 4. Provider dispatch and key/host binding (D8)

Each request resolves to one provider branch (anthropic, openai, moonshot,
meta, deepinfra, google, gemini, xai) or OpenRouter fallback.

**The D8 invariant:** a provider's key is only ever sent to that provider's
host. The legacy Node code had a key-confusion bug (`model_router.js:537`):
a Claude request with no Anthropic key would send the OpenRouter key to
`api.anthropic.com`.

**Rust enforcement:** single choke point `UpstreamPlan::dispatch_host_checked`
(`src/providers/mod.rs`) — the request is not built until the host passes the
compiled-in `UPSTREAM_ALLOWLIST` (8 hosts); the bearer key is attached only
afterwards. Key and host are resolved together in each branch; there is no
path where they can diverge (post-H1 fix).

**`sk-or-` routing signal:** a key with the `sk-or-` prefix in any provider
slot is not valid at that provider — it's routed to OpenRouter instead.
Every branch must honor this signal (A9 M5/M6 fixed the google and xai
branches that didn't).

## 5. Failover (D7)

Failover sends the failed request to the **same canonical model** via
OpenRouter. Eligibility:

- **When:** 429, 500, 502, 503, 504, 529, connect timeout — and only
  **before the first byte** of the upstream response.
- **Where:** only to the same canonical model (`canonical_model(primary) ==
  canonical_model(secondary)`).
- **Never:** from OpenRouter (already there), after streaming starts,
  or to a different model.

Transport errors (TLS failures, DNS) are **not** failover-eligible — they
fail fast (A9 M2). Silently routing around a TLS error would mask
interception.

## 6. Streaming approach: fake-SSE parity

The gateway does **not** do real upstream streaming. Both Node and Rust:

1. Send `stream:false` upstream (buffer the full JSON response).
2. If the client asked for `stream:true`, emit exactly 2 SSE chunks
   (full delta + finish/usage/billing) followed by `[DONE]`.

This is **deliberate parity**, not a bug (ADR R-003). The original Phase 2
spec said "real streaming"; the code won. Changing to real streaming is
follow-up work requiring its own architect design (it changes billing/settle
timing) and Adam's decision.

**Known deviation in progress (A9 M3/M4):** the fake-SSE path hardcoded
`finish_reason: "stop"` and leaked raw Anthropic `stop_reason` values.
The fixes pass through truthful values, which deliberately breaks
byte-parity with legacy on those fields. Recorded in the cutover runbook.

## 7. Models endpoint

`GET /v1/models` returns the curated model list (reverse unshift order,
matching Node) merged with the live OpenRouter catalog. Pricing comes from
`models.json` (wholesale, live-synced by ModelSentinel).

## 8. Cutover strategy

Gradual, reversible, via the `GATEWAY_UPSTREAM_URL` proxy flag on the Node
gateway (ADR R-005). Full runbook:
`~/workspace/router-rearch/audit/router_rearch/CUTOVER_RUNBOOK.md`.

| Step | What | Rollback |
|------|------|----------|
| A | Deploy Rust alongside Node (`:8444`), zero traffic | Stop the Rust process |
| B | Shadow traffic (optional): mirror + compare | Disable the tee |
| C | Flip proxy flag: Node proxies `/v1/*` to Rust | Unset flag, restart Node (<60s) |
| D | After 7 clean days: clients point direct at Rust | Point LB/DNS back |
| E | After 30 clean days: remove Node `/v1` handlers | Git revert |

**Current state:** Step A done (Rust live on `:8444`, zero traffic).
Step C attempted prematurely and rolled back (see ASBUILT.md deviations).

## 9. Module layout

**Node** (`services/gateway/`):
- `server.js` (1,438 lines) — HTTP server, auth, route handling, proxy flag
- `model_router.js` (1,142 lines) — provider dispatch, key resolution
- `token_ledger.js` (2,424 lines) — SQLite ledger (reserve/settle/release)

**Rust** (`services/skidnir-gateway/src/`):
- `main.rs` — boot, config, graceful shutdown
- `handlers/completions.rs` — POST /v1/chat/completions
- `handlers/models.rs` — GET /v1/models
- `handlers/health.rs` — /health, /ready, /metrics
- `providers/` — per-provider dispatch, failover (D7), key/host binding (D8)
- `ledger.rs` — internal ledger API client (never opens the DB)
- `keys.rs` — secret-enclave fetch, 10-min refresh, env fallback
- `pins.rs` — D5 model pins (write-through SQLite — the only rusqlite use)
- `failover.rs` — failover eligibility logic
- `sse.rs` — SSE parser (retained; docs corrected — not live)
