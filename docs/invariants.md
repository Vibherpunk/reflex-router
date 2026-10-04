# Invariants — Routing Layer

Non-negotiable rules. These must never be violated by any change to the
routing layer. A change that breaks an invariant is rejected regardless of
what else it improves.

For the reasoning behind each, see [`architecture.md`](./architecture.md)
and [`DECISIONS.md`](./DECISIONS.md).

## I-01 — Billing goes through the ledger API, never direct DB

The gateway must never open `gateway_ledger.db`. Every money movement
(reserve, settle, release) goes through `POST /internal/ledger/*`.

- Rust: `ledger.rs` is a typed HTTP client. `rusqlite` is used in exactly
  one module (`pins.rs`) for the model-pins DB — a separate concern.
- Rationale: the ledger is a separate service with its own invariants.
  Direct DB access bypasses them.

## I-02 — Key/host binding (D8)

A provider's key is only ever sent to that provider's host. The key and
host are resolved together; there is no path where they diverge.

- Rust: single choke point `UpstreamPlan::dispatch_host_checked` with the
  compiled-in `UPSTREAM_ALLOWLIST`. Key attached only after host passes.
- The `sk-or-` prefix signal must be honored on every provider branch:
  such a key routes to OpenRouter, never to the named provider's host.

## I-03 — Failover scoping (D7)

Failover is eligible **only** when ALL of these hold:

- Status is 429, 500, 502, 503, 504, 529, or connect/timeout — AND
- No bytes received yet (before first byte) — AND
- Target is the **same canonical model** via OpenRouter — AND
- Source is not already OpenRouter.

Transport errors (TLS, DNS) are **never** failover-eligible — they fail
fast. Silently routing around a TLS error masks interception.

## I-04 — Test surface inert in production

`POST /__test/key_overrides` and `TEST_PROVIDER_BASE_URL_*` overrides
must be unreachable and unusable in production.

- Rust: gated on exact `GATEWAY_ENV == "test"`. Deployed with
  `GATEWAY_ENV=production` — the route is never registered.
- Node: honored only when `NODE_ENV === 'test'`.

## I-05 — Fail-closed on ledger unavailability (reserve path)

If the ledger API is unreachable at reserve time, the gateway returns
503 `ledger_unavailable`. It must never serve a completion without a
reservation.

(Note: the settle path is currently fail-open — see architecture.md §3.
I-05 covers reserve only. Settle hardening is tracked in PLAN.md.)

## I-06 — Conformance parity before traffic

No gateway code change takes production traffic without the conformance
suite passing. The 58-case corpus must pass with zero unexpected diffs.
Expected diffs (e.g. M3/M4 truthful `finish_reason`) must be recorded in
the cutover runbook before they are accepted.
