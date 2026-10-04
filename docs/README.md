# Documentation map — Routers

**One fact, one owner. Everyone else links.**

This is the only index for the routing layer documentation. Do not add a second
table of contents anywhere else. When a fact already has an owner, update that
file — do not paste a parallel spec.

Scope: the routing/billing/failover system — the legacy Node gateway AND the
Rust `skidnir-gateway`, and the migration between them. For the Skidnir product
itself (not the gateway), see the Skidnir docs. For Anton's brain/remediation,
see the Anton docs.

| Audience | Start | Owns |
|----------|-------|------|
| Agent (new task) | [`ORIENT.md`](./ORIENT.md) | Task → doc router. Open the one matching row. |
| Agent (hard rules) | [`../AGENTS.md`](../AGENTS.md) | Hard rules + pointers. Not a design book. |
| Architecture | [`architecture.md`](./architecture.md) | Why the routing layer is shaped this way. Migration rationale. |
| Non-negotiables | [`invariants.md`](./invariants.md) | Rules that must never be violated. The "only copy" pattern. |
| Decisions | [`DECISIONS.md`](./DECISIONS.md) | ADRs: context, decision, consequences, status. |
| What shipped | [`ASBUILT.md`](./ASBUILT.md) | Current production state + `## Deviations` (planned vs built). |
| What's next | [`../PLAN.md`](../PLAN.md) | Current state → cutover complete. Steps with contracts. |

## `docs/` (this directory)

| Doc | Owns |
|-----|------|
| [`ORIENT.md`](./ORIENT.md) | Agent task router and read-first links. Not a fact index. |
| [`architecture.md`](./architecture.md) | Migration rationale (Node → Rust), billing flow, failover design, key/host binding, streaming approach. |
| [`invariants.md`](./invariants.md) | Billing through ledger API only; key/host binding; failover scoping; test surface inert in prod. |
| [`DECISIONS.md`](./DECISIONS.md) | ADRs R-001 through R-00x. Date, context, decision, consequences, status. |
| [`ASBUILT.md`](./ASBUILT.md) | What's live now (Node :8080/:8443, Rust :8444 zero-traffic), conformance status, A9 fix status. Deviations table. |

## Root (`~/workspace/router-docs/`)

| Doc | Owns |
|-----|------|
| [`AGENTS.md`](../AGENTS.md) | Hard rules for agents working on the routers. |
| [`PLAN.md`](../PLAN.md) | The cutover plan: A9 fixes → conformance → ledger switch → keys → proxy flip → direct. |
| [`changelog.d/`](../changelog.d/) | One `*.md` fragment per change. Folded at release. |

## Source repos (where the code lives)

| System | Location |
|--------|----------|
| Legacy Node gateway | `~/workspace/router-rearch/services/gateway/` (`server.js`, `model_router.js`, `token_ledger.js`) |
| Rust gateway | `~/workspace/router-rearch/services/skidnir-gateway/` |
| Audit evidence | `~/workspace/router-rearch/audit/router_rearch/` |
| Git history | `~/workspace/router-rearch`, branch `router-rearch` (base `f535bd9`); A9 fixes on `fix/a9-gateway-fixes` |

## Adding or changing a doc

1. Find the owner in the tables above. Edit that file.
2. If the fact has no owner, add a row here in the same commit as the new file.
3. Link; never duplicate. A second copy of a fact is a doc bug.
