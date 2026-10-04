# Agent orientation — Routers

Open this when the task area is unclear.
Facts stay in the owner files in [`README.md`](./README.md). This page only routes.
Open the one row that matches the change. Leave the other owners closed.

## Read first

| Task | Read |
|------|------|
| Why Node → Rust; billing flow; failover; key/host binding; streaming | [`architecture.md`](./architecture.md) |
| Rules that must never break (billing, keys, failover, test surface) | [`invariants.md`](./invariants.md) |
| Why a past decision was made (Rust rewrite, fake-SSE, proxy cutover…) | [`DECISIONS.md`](./DECISIONS.md) |
| What's live right now; what deviated from plan | [`ASBUILT.md`](./ASBUILT.md) |
| What's next; cutover steps | [`../PLAN.md`](../PLAN.md) |
| Hard rules for working on the routers | [`../AGENTS.md`](../AGENTS.md) |
| Billing settle behavior; reserve/settle/release | [`architecture.md`](./architecture.md) (Billing flow) |
| Adding a provider or changing routing | [`architecture.md`](./architecture.md) (Provider dispatch) + [`invariants.md`](./invariants.md) (I-02, I-03) |
| Changing failover eligibility | [`invariants.md`](./invariants.md) (I-03) — then [`DECISIONS.md`](./DECISIONS.md) R-004 |
| Touching `finish_reason` / SSE output | [`ASBUILT.md`](./ASBUILT.md) (Deviations — M3/M4 parity breaks) |
| Conformance suite; adding a corpus case | `~/workspace/router-rearch/tests/conformance/` (code, not docs) |
| A9 fix status; what's implemented vs pending | [`ASBUILT.md`](./ASBUILT.md) (A9 status) |
| Cutover runbook (production steps) | `~/workspace/router-rearch/audit/router_rearch/CUTOVER_RUNBOOK.md` |
| Audit evidence (Gate 2, Phase 2, A9) | `~/workspace/router-rearch/audit/router_rearch/` |

## Ask first

Hard stops in [`../AGENTS.md`](../AGENTS.md) are proceed-or-stop rules.
Ask before any of these, including when the change looks locally justified:

- Changing what `stream:true` returns (fake-SSE parity is a recorded decision — see DECISIONS.md R-003).
- Changing failover eligibility beyond the D7 set (see invariants.md I-03).
- Letting the gateway open the ledger DB directly (see invariants.md I-01).
- Sending a provider key to a host not on the allowlist (see invariants.md I-02).
- Enabling the `/__test/` surface outside `GATEWAY_ENV=test` (see invariants.md I-04).
- Any production cutover step (see PLAN.md — each step needs Adam's explicit go-ahead).
