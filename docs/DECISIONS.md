# Architecture Decision Records — Routing Layer

Format: date, context, decision, consequences, status.
Status values: `PROPOSED` / `ACCEPTED` / `SUPERSEDED` / `RECONSTRUCTED`
(`RECONSTRUCTED` = inferred from code/history, not from a recorded decision).

---

## R-001 — Rewrite the gateway in Rust

- **Date:** 2026-10 (Phase 2) · **Status:** ACCEPTED
- **Context:** The legacy Node gateway moves money (reserve/settle billing)
  and handles provider keys. Two structural risks: (1) the D8 key-confusion
  bug was possible because dynamic dispatch made wrong key/host pairing easy;
  (2) latency budget (≤15ms p99 overhead) was hard to hit in Node at
  concurrency.
- **Decision:** Rewrite as `skidnir-gateway` in Rust (axum/tokio). Parity-first:
  byte-identical to Node on all 58 conformance cases before taking traffic.
- **Consequences:** Memory safety at the billing boundary; 3–16ms p99 at c1
  (vs Node slower at every level); key/host binding enforced structurally
  via a single choke point. Cost: two implementations to maintain during
  migration; Rust expertise required for changes.
- **Note:** RECONSTRUCTED from the Phase 2 gate docs and code. No single
  written ADR existed before this file.

## R-002 — Gateway never opens the ledger DB directly

- **Date:** 2026-10 (Gate 1) · **Status:** ACCEPTED
- **Context:** The Node `token_ledger.js` opens the SQLite ledger directly
  (2,424 lines, in-process). This couples the gateway to ledger internals
  and makes the billing boundary untestable in isolation.
- **Decision:** The Rust gateway is a ledger *client*. All money movement
  goes through `POST /internal/ledger/*` (`ledger.rs` is a typed HTTP
  client). The only SQLite use is `pins.rs` for model pins (separate concern).
- **Consequences:** Ledger invariants enforced in one place; gateway
  fail-closed on ledger unavailability (503). Cost: network hop per
  reserve/settle; ledger API must be highly available.
- **Note:** RECONSTRUCTED from Gate 1/Gate 2 docs. See invariant I-01.

## R-003 — Fake-SSE parity instead of real streaming

- **Date:** 2026-10 (Gate 2) · **Status:** ACCEPTED (with Adam's decision pending on follow-up)
- **Context:** The Phase 2 spec said "real streaming." The Node gateway never
  streams upstream — it buffers the JSON response and emits 2 fake SSE chunks
  + `[DONE]` for `stream:true` clients.
- **Decision:** Match legacy exactly (fake-SSE). Code wins over spec.
  Real upstream streaming is follow-up work requiring its own architect
  design (changes billing/settle timing) and Adam's explicit decision.
- **Consequences:** Zero behavioral delta during migration; conformance is
  byte-identical. Cost: clients don't get true streaming; the spec/spec
  discrepancy must be resolved (accept parity and amend spec, or build real
  streaming later).
- **Note:** RECONSTRUCTED from Gate 2 §3 ("code-wins-over-prompt discrepancies").
  A9 M3/M4 deliberately break parity on `finish_reason` truthfulness —
  recorded in the cutover runbook.

## R-004 — Failover only to the same model via OpenRouter (D7)

- **Date:** 2026-10 (design phase) · **Status:** ACCEPTED
- **Context:** When a provider fails, the gateway must not silently
  substitute a different (cheaper/worse) model — the client asked for
  a specific model and is billed for it.
- **Decision:** Failover eligible only on 429/500/502/503/504/529 +
  connect/timeout, only before the first byte, and only to the same
  canonical model via OpenRouter. Transport errors (TLS/DNS) fail fast,
  never fail over.
- **Consequences:** No silent model substitution; TLS errors surface
  instead of being masked. Cost: slightly lower availability on
  transport errors (correct tradeoff).
- **Note:** RECONSTRUCTED from the D7 design and A9 verification. See
  invariant I-03.

## R-005 — Gradual cutover via proxy flag (not big-bang)

- **Date:** 2026-10 (Phase 3) · **Status:** ACCEPTED
- **Context:** Switching all LLM traffic from Node to Rust in one deploy
  risks an extended outage if the Rust gateway has an undiscovered defect.
- **Decision:** Five-step cutover using `GATEWAY_UPSTREAM_URL` on the Node
  gateway: (A) deploy alongside, zero traffic → (B) shadow → (C) proxy
  flip → (D) direct after 7 clean days → (E) decommission after 30 days.
  Every step has a <60s rollback.
- **Consequences:** Blast radius contained at each step; rollback always
  available. Cost: two gateways running during migration; proxy adds
  one hop during step C.
- **Note:** RECONSTRUCTED from `CUTOVER_RUNBOOK.md`. Step C was attempted
  prematurely and rolled back (see ASBUILT.md deviations).

## R-006 — `sk-or-` keys route to OpenRouter on every branch (D8 signal)

- **Date:** 2026-10 (A9 fixes) · **Status:** ACCEPTED
- **Context:** An `sk-or-`-prefixed key in a named provider's slot (e.g.
  GOOGLE) is not valid at that provider — sending it there 401s. The
  google and xai branches ignored the signal.
- **Decision:** Every provider branch honors the `sk-or-` routing signal:
  such keys go to OpenRouter with the secondary model, tagged
  `<provider>-via-openrouter` in telemetry.
- **Consequences:** Misconfigured keys succeed via OpenRouter instead of
  401ing. No behavior change for correctly-configured native keys.
- **Note:** From A9 cold review (M5/M6). Implemented on `fix/a9-gateway-fixes`.
