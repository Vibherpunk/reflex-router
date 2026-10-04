# As-Built — Routing Layer

**Date:** 2026-10-02 (evening)
**Status:** Living document. Update after every production change.

What actually exists right now. For what *should* be true, see
[`invariants.md`](./invariants.md). For what's next, see [`../PLAN.md`](../PLAN.md).

## 1. Production traffic path (live)

```
client → Caddy (/v1/*) → tdh-onboarding:8080 (Node gateway) → providers
```

- **Node gateway** serves all production `/v1/chat/completions` and
  `/v1/models` traffic via the `tdh-onboarding` container (`:8080`).
- **Current degradation (2026-10-02):** `/v1/*` is returning **HTTP 502**
  (`Gateway upstream unavailable`). Root cause: `GATEWAY_UPSTREAM_URL=
  http://127.0.0.1:8444` is set in the container, but `127.0.0.1` inside
  the container is the container itself, not the host — the Rust gateway
  is unreachable at that address. The container *can* reach Rust via the
  docker-gateway IP (`http://172.19.0.1:8444` → 401, correct). Not fixed
  per Adam's order ("do not change any prod config" during audit).
- **Provider keys in prod:** none in container env. Keys come from the
  secret-enclave via the in-process RAM vault. Only the `openrouter` key
  was loading at last check — all providers route via OpenRouter fallback.

## 2. Rust gateway (deployed, zero traffic)

- **Binary:** `/opt/skidnir/bin/skidnir-gateway` (8,134,264 bytes),
  systemd `skidnir-gateway.service`, port `:8444`, `GATEWAY_ENV=production`.
- **Source identity:** sha256 `95c7c583…` — byte-identical to tree at
  `router-rearch` HEAD `4c8989b` (verified 2026-10-02).
- **Traffic:** zero established connections. No Caddy route points at `:8444`.
- **Ledger wiring:** `LEDGER_API_URL=http://127.0.0.1:8081` → the
  `skidnir-ledger-api.service`, which currently runs the **test** starter
  (`test_ledger.db`). Must switch to the production ledger before any
  real traffic (PLAN.md step).
- **Pins DB:** `/opt/skidnir/data/gateway-pins/pins.db`, healthy, WAL active.

## 3. Verification status

| Check | Status | Evidence |
|-------|--------|----------|
| Gate 2 (Rust impl) | PASS | 58/58 conformance, 0 diffs; 34/34 cargo tests; clippy clean |
| Phase 2 audit (A1–A8) | PASS (A3 = decision pending) | `audit/router_rearch/PHASE2_AUDIT_PLAN.md` |
| A9 cold review | **NOT APPROVED** | 1 HIGH (TOCTOU), 5 MEDIUM, 2 LOW — see §4 |
| A9 fixes | **IN PROGRESS** | Branch `fix/a9-gateway-fixes` (from `router-rearch`) |
| Perf (c50) | FAIL (known) | Ledger SQLite bottleneck, not gateway overhead; tracked follow-up |

## 4. A9 fix status (as of 2026-10-02 ~23:30 PDT)

| Finding | Severity | Status |
|---------|----------|--------|
| H1 TOCTOU key/host confusion | HIGH | In progress (`fix/a9-gateway-fixes`) |
| M1 settle fail-open | MEDIUM | Alerting in progress; retry gated on ledger idempotency confirmation |
| M2 dead `network_eligible` | MEDIUM | In progress |
| M3 fake-SSE hardcoded `finish_reason` | MEDIUM | In progress (deliberate parity break — see Deviations) |
| M4 Anthropic `stop_reason` leak | MEDIUM | In progress (deliberate parity break — see Deviations) |
| M5 google `sk-or-` routing | MEDIUM | In progress |
| M6 xai `sk-or-` routing | MEDIUM | In progress (critic found; architect confirmed no other branches affected) |
| L1/L2 dead code, `usage_source` | LOW | In progress |

Design: `audit/router_rearch/A9-FIX-DESIGN.md` (critic-reviewed, M6 added).
Conformance must re-run after implementation (M3/M4 diffs expected and correct).

## 5. Known production discrepancies

- **Ledger path:** docs once claimed `/data/gateway/gateway_ledger.db`;
  actual prod path is `/data/gateway_ledger.db`. (Phase 0 §3.)
- **AGY bridge port:** repo says `:8100`; actual is `:8443` on docker-gateway
  IPs (`:8100` is the Friday MCP server). Repo is stale.
- **Prod drift (resolved):** `token_ledger.js` / `server.js` differed
  prod-vs-git; diff was branding-only (`vibehard.ai`→`bothard.ai`), zero
  behavioral difference on `/v1/*`.
- **Proxy code on VPS:** `/opt/tdh-agency/services/gateway/server.js` has
  uncommitted proxy code (85+/18-) on branch `postalagent-agentfirst` —
  not in any `router-rearch` branch. Reconcile before cutover.

## Deviations

| Planned | Built | Reason |
|---------|-------|--------|
| Real upstream streaming (Phase 2 spec) | Fake-SSE, byte-identical to Node | Deliberate parity decision (R-003); code won over spec |
| D9 `tool_passthrough` | Removed (never existed in Node) | Spec described behavior Node didn't have; porting it would *create* a delta |
| Usage estimation `ceil(prompt_chars/4)` | Removed; legacy settles 0 tokens | Spec contradicted actual Node behavior |
| `finish_reason: "stop"` parity (M3) | Truthful pass-through (in progress) | Correctness over parity; recorded in cutover runbook |
| Raw `stop_reason` parity (M4) | Mapped values (in progress) | Correctness over parity; recorded in cutover runbook |
| GATEWAY_UPSTREAM_URL unset until Step C | Set (to wrong address) in `tdh-onboarding` | Premature cutover attempt; caused 502s; rolled back pending Adam's go-ahead |
| Production ledger on `:8081` | Test ledger wired | Zero-traffic deployment; must switch before any real traffic |
| All provider keys provisioned | Only `openrouter` loads | Secret-enclave fetch fails from host; must resolve before cutover |

Items marked without a source are RECONSTRUCTED from code, git history,
and audit docs — the original plans were not all written down.
