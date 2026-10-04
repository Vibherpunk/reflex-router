# Agent notes — Routers

Documentation map (one owner per fact): [`docs/README.md`](docs/README.md).
When the task area is unclear, [`docs/ORIENT.md`](docs/ORIENT.md) routes.
Open the one row that matches the change. This file is the hard-rule contract.
Design lives in the owner docs; do not grow a second design book here.

## Hard rules

1. **One fact, one owner.** `docs/README.md` is the only index. Update the
   owner file; never paste a parallel spec.
2. **Invariants are non-negotiable.** Read [`docs/invariants.md`](docs/invariants.md)
   before touching billing, keys, failover, or the test surface. A change that
   breaks an invariant is rejected no matter what else it improves.
3. **No code without a plan.** Significant work needs `PLAN.md` updated first.
4. **No "done" without as-built.** Update [`docs/ASBUILT.md`](docs/ASBUILT.md)
   (including the `## Deviations` table) and add a `changelog.d/` fragment.
5. **Conformance before traffic.** No gateway code change takes production
   traffic without the 58-case conformance suite passing (I-06).
6. **No production cutover steps without Adam's explicit go-ahead.** The
   runbook steps are his hands. (See `PLAN.md`.)

## Change discipline

- One logical change per commit. Message: `scope: what and why`.
- `//` comments only for invariants, protocol rules, SAFETY, or library quirks.
- `changelog.d/`: one `*.md` fragment per change. Do not edit a changelog directly.

## Key locations

- Node gateway: `~/workspace/router-rearch/services/gateway/`
- Rust gateway: `~/workspace/router-rearch/services/skidnir-gateway/`
- Audit evidence: `~/workspace/router-rearch/audit/router_rearch/`
- Conformance: `~/workspace/router-rearch/tests/conformance/`
- Cutover runbook: `~/workspace/router-rearch/audit/router_rearch/CUTOVER_RUNBOOK.md`
