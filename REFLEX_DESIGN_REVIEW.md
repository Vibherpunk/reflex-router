# Independent Review: REFLEX_BLOCKER_FIX_DESIGN.md (v2.3.0)

**Reviewer role:** independent (did not write the design).
**Method:** every load-bearing claim checked against the code in `~/workspace/reflex-router`; regexes and classification chains executed, not eyeballed.
**Verdict: GO-WITH-CHANGES** — the approach is sound, but the design has one fundamental flaw (catalog epoch/snapshot), one behavior-neutrality bug (memory-escalation profiles), and materially under-counted blast radius in two places. All fixable in the design; details below.

---

## Claim-by-claim verdicts

### 1. solver.py imports TIER3_PATTERNS from classifier — PASS

`solver.py:14`:
```python
from classifier import detect_tool_errors, classify_domain, TIER3_PATTERNS, TIER2_PATTERNS, TIER1_PATTERNS, PROTECTED_DOMAINS
```
Used at `solver.py:142,152,178`. No local duplicate definition exists (grep confirms the import is the only source). The design's "one-line fix propagates" claim holds — `solver.py` needs zero changes for Blocker 1.

### 2. Blocker 1 resulting classifications — PASS (with one attribution correction)

Keyword grounding in `scoring_spec.yaml`:
- `"reconcile"` → accounting keywords (`scoring_spec.yaml:316`) ✓
- `"audit"` → compliance keywords (`scoring_spec.yaml:332`) ✓
- `"general ledger"` → accounting keywords (`scoring_spec.yaml:312`) ✓ (also finance, `:280`)
- `accounting`, `compliance`, `finance` ∈ `PROTECTED_DOMAINS` (`classifier.py:28`) ✓

End-to-end verification — I ran the design's proposed new `TIER3_PATTERNS` through the real `classify_request`:

| Prompt | Tier | Domain floor | Expected |
|---|---|---|---|
| `reconcile my QBO feed` | 2 | accounting | 2 ✓ |
| `audit last month's transactions` | 2 | compliance | 2 ✓ |
| `show me the general ledger balance` | 2 | **finance** | 2 ✓ |
| `design a byzantine fault-tolerant distributed consensus protocol` | 3 | pattern[1] | 3 ✓ |
| `prove correctness of this split-brain recovery algorithm` | 3 | pattern[1] | 3 ✓ |
| `perform cryptographic audit and formal verification of the enclave` | 3 | pattern[2] | 3 ✓ |
| `plan a zero-downtime database migration for the payments cluster` | 3 | pattern[1]+[2] | 3 ✓ |
| `write an rfc for the new system architecture` | 3 | pattern[1] | 3 ✓ |
| `assess the threat model for our auth service` | 3 | pattern[2] | 3 ✓ |

**Correction:** the design says "general ledger" → domain `accounting`. Actual: `finance`, because `finance` (yaml `:271`) precedes `accounting` (yaml `:307`) in dict order and also lists `"general ledger"`. Tier outcome is identical (both protected → Tier 2), so the test assertion (`tier == 2`) is safe — but the design's "verified by reading the decision chain" overstates its precision. If any future logic branches on the specific domain name (not just protected-membership), this matters.

Also verified: `tests/test_router.py` and `tests/test_effort_routing.py` contain no bare audit/reconcile/ledger Tier-3 assertions (only `test_effort_routing.py:83-84`'s "cryptographic audit and formal verification", which stays T3 via pattern[2]). The design's blast-radius note here is accurate. `test_p1_1`'s huge prompt ("Refactor distributed consensus protocol architecture spec…") still hits T3 via pattern[1] — its `tier == 3` assertion survives.

### 3. Consolidation: no test_router.py assertion breaks — PASS on conclusion, FAIL on one sub-claim

Audited every explanation/reason assertion in `tests/test_router.py`:

| Assertion | Source of `reason` | After consolidation | Breaks? |
|---|---|---|---|
| `"Tier 0"/"Tier 1"/"Tier 2"/"Tier 3" in reason` (lines 39–57) | `classify_request` directly — already classifier strings | unchanged | No |
| `"Escalated" in reason` (line 73) | `classify_request` directly | unchanged | No |
| `"Memory Auto-Escalation" in reason` (line 138) | `classify_request` directly | unchanged | No |
| `"Explicit model override"` (line 83) | `arbitrate` (untouched) | unchanged | No |
| `"Primary Sub"` (line 94) | `arbitrate` (untouched) | unchanged | No |
| `"KV-Cache Latch locked"` (line 113) | `arbitrate` KV-latch branch (untouched) | unchanged | No |

The design's conclusion ("no assertion breaks") is correct, but its mechanism description is muddled: it implies the changed strings are the `classify_request`-tested ones. In fact the only solver-path string change is `vector.explanation` embedded in `arbitrate`'s final explanation (`solver.py:428`: `Fallback: … [{vector.explanation}]`), changing e.g. `"Matched Tier 3 Architecture pattern: …"` → `"Matched Tier 3 pattern: …"`. I grepped all test files for assertions pinning the exact `"Matched Tier N …"` strings — none exist. `test_effort_routing.py` asserts `vec.tier_num`/`vec.effort` on the vector (values preserved: `get_tier_effort(3) == "high"` confirmed), not explanation text. Safe.

**Real bug — consolidation is NOT behavior-neutral on the memory-escalation path.** The design claims "Behavior-neutral by construction." Compare:

- Current `solver.py` memory branch: uses `scoring_spec.get_memory_escalation_defaults(esc_tier)` → tier_2: `reasoning_depth 0.60, architecture_score 0.70` (`scoring_spec.yaml:205-210`).
- Design's consolidated code: `profile = scoring_spec.get_tier_threshold_profile(tier_num)` → tier_2: `0.90 / 0.75` (`scoring_spec.yaml:191-204`).

A memory-escalated-to-Tier-2 prompt would get a *stronger* capability vector (0.90/0.75 vs 0.60/0.70), which changes the hard feasibility gate in `arbitrate` (`solver.py:285-288`: models with `reasoning_capability < vector.reasoning_depth` are excluded) — i.e., different routing outcomes. Trojan-horse, T3/T2/T1-pattern, domain-floor, length, and empty branches all map to identical profiles (verified each); only the memory branch diverges (tier_3 memory escalation happens to coincide at 0.95/0.95).

**Required change:** preserve the memory-escalation profile distinction — e.g., branch on `"Memory Auto-Escalation" in tier_explanation` and use `get_memory_escalation_defaults(tier_num)` in that case, or have `classify_request` return the firing branch. As written, the design silently alters routing for memory-escalated prompts.

Minor hygiene the design omits: after consolidation, `solver.py`'s `import re`, `detect_tool_errors`, `check_memory`, `record_incident` become dead (all uses are in the deleted branches, lines 100–179). The design only mentions dropping the pattern imports.

On the "double-count" claim: currently nothing calls both `classify_request` and `extract_vector` on the same messages in production (`classify_request` appears nowhere in `server.py`/`mcp_server.py`/`federation.py`/`reflex.py`), so there is no live double-record of incidents today. Consolidation is behavior-neutral on incident counting, not "strictly more correct." Overstatement, not a defect.

### 4. Auth blast radius: "~8 call sites across 3 files" — FAIL (undercounted ~2.5×)

Actual protected-endpoint `TestClient` call sites (verified by grep; `/health` excluded — stays open):

**`tests/test_audit_patches.py` — 4 sites** (design said 3):
- `:68` `POST /v1/chat/completions` (`test_p0_2_circuit_open_returns_503_load_shedding`)
- `:110` `POST /v1/chat/completions` (`test_p0_4_client_4xx_does_not_trip_circuit_breaker`)
- `:127` `POST /v1/route` (`test_p1_1_preview_endpoint_does_not_pollute_session_affinity`)
- `:132` `POST /v1/route` (same test, second call)

**`tests/test_effort_routing.py` — 5 sites** (design did not enumerate):
- `:141`, `:151`, `:159`, `:167` `POST /v1/route`
- `:257` `POST /v1/chat/completions`

**`tests/test_router.py` — 11 sites** (design did not enumerate):
- `:27` `GET /v1/models`, `:129` `POST /v1/feedback`, `:141` `GET /v1/stats`, `:149` `GET /v1/harnesses`, `:159`+`:172` `POST /v1/delegate`, `:163` `POST /v1/route`, `:279`,`:309`,`:340`,`:367` `POST /v1/chat/completions`

**Total: 20, not ~8.** The fix (conftest token + `headers=auth_headers`) is the same shape, but the implementer needs the real list or tests will be missed.

**Load-bearing subtlety the design misses — `test_p0_4` (line 101):** it loops `client_error_codes = [400, 401, 404, 422]` asserting `resp.status_code == code`. Auth runs before provider logic, so without the header *every* iteration returns 401 — the 400/404/422 assertions fail, and the 401 case passes for the wrong reason. Adding `auth_headers` here isn't mechanical; it's required for the test to keep testing what it claims.

`tests/test_federation.py:191` patches `federation.call_reflex_http_gateway` (doesn't hit the real gateway) — safe. `tests/benchmark_30_prompts.py` makes no HTTP calls — safe. No other test files import `TestClient`.

### 5. Decision-logging blast radius: select_candidate_routes unpack sites — FAIL (under-enumerated)

- **test_router.py: 8 four-tuple unpack sites**, not 3: lines `81, 91, 106, 111, 268, 295, 327, 356`. All break when the return becomes a 5-tuple.
- **server.py has ONE internal call site** (`:528`, in `/v1/chat/completions`) — not two. `/v1/route` (`:278`–`:287`) calls `solver.arbitrate` **directly** and never goes through `select_candidate_routes`. The design's "`/v1/route` passes `endpoint='/v1/route'`" is unimplementable as stated — `/v1/route` needs its own direct `log_decision` call after its `arbitrate` call. The design must spell this out or the `/v1/route` hook will be silently dropped.
- `tests/test_effort_routing.py:16` imports `select_candidate_routes` but never calls it (dead import) — no change needed.
- **Un-enumerated:** `preflight_and_stream` returns a 4-tuple (`server.py:372`) unpacked at `:550`. Threading `decision_id` for the failover `supersedes` chain (as the design requires) means changing that signature and unpack too.

### 6. mcp_server.py / federation.py bearer call sites — PASS, with one merge hazard

- `mcp_server.py`: `POST /v1/delegate` (`:125`), `GET /v1/harnesses` (`:160`), `POST /v1/route` (`:185`) need the header. `GET /health` (`:159`) stays open; adding the header there is harmless.
- `federation.py`: `call_reflex_http_gateway` (`:460`), urllib `Request` built at `:482-486` — needs `Authorization` added to the headers dict. (Design said "~line 462"; actual Request construction is `:482`.)
- No other in-repo gateway HTTP callers (`reflex.py` makes no HTTP calls; `install.py` only writes a config URL).
- **Hazard:** `mcp_server.py:125` already passes `headers={Content-Type, X-Reflex-Delegation-Depth, X-Reflex-Delegation-Chain, X-Reflex-Caller}`. The design's "pass `headers=_gateway_headers()`" implemented literally would **clobber** those headers. Must be a merge (`{**headers, **_gateway_headers()}` or equivalent), not a replacement.

### 7. scoring_spec.get_spec_hash — PASS

- No `get_spec_hash` exists in `scoring_spec.py` (18 functions enumerated; none hash) — must be added, as the design says. ✓
- User overlay mechanism exists: `~/.reflex/scoring_spec.yaml` deep-merged over the repo base (`scoring_spec.py:11-12, 29-52`), with mtime-based cache invalidation (`:36-38`) — the design's "hash covers repo + overlay, recomputed on mtime change" aligns with existing machinery. ✓

### 8. catalog.py epoch migration — PASS on pattern, FAIL on "additive"

- The try/except `ALTER TABLE` migration pattern exists (`catalog.py:362-374`). ✓
- `refresh_from_providers` → `upsert_models` is the refresh write path (`:464-487`); `rescore_all` also funnels through `upsert_models`. ✓
- **But it is not additive.** `upsert_models` (`:428-447`) uses positional `INSERT OR REPLACE INTO models VALUES (…21 params…)` with **no column list**. Adding an `epoch` column breaks the INSERT (22 columns vs 21 values). The implementer must also add `epoch` to the `ReflexModelDefinition` dataclass (`:52-73`) — otherwise `_load_cache`'s `ReflexModelDefinition(**d)` (`:406`) raises `TypeError` on the unexpected `epoch` kwarg. The design says "stamps the current epoch on every row it writes" without flagging either change.
- **Fundamental flaw — snapshot() cannot work as specified.** The table's `PRIMARY KEY (id, provider)` plus `INSERT OR REPLACE` keeps exactly **one row per (provider, id)**: every refresh destroys the previous epoch's rows. The design's `snapshot(epoch)` ("the row with max epoch ≤ requested per (provider, id)") will find nothing for any past epoch after a refresh — history is unrecoverable. Mode-A replay (`replay_decision` against `catalog.snapshot(record["catalog_epoch"])`) and the design's own `test_decision_replay` / `test_catalog_epoch_logged` ("two decisions across a catalog refresh carry different epochs" — implying cross-refresh replay) cannot work on this schema. **Required design change:** either a `models_history` table (append on refresh, never replace) or making `epoch` part of the primary key. As specified, Blocker 5's reproducibility goal is not achieved.

### 9. Version strings — PASS

Found: `server.py:67` (`FastAPI(version="2.2.0")`), `server.py:218` (`"version": "2.2.0"` in `/health`), `pyproject.toml:3` (`2.1.0` — drift the design correctly notes), `config.py` has no `VERSION` (to be added ✓), `mcp_server.py:41` (`version="2.1.0"` — design correctly says leave it; it's the MCP interface version). The design's bump plan covers exactly the right files. Optional sweep (not required): `README.md:1` says v2.1.0 and `tests/benchmark_30_prompts.py:3` says v2.2.0 — doc-only.

### 10. Other runtime hazards

a) **Lifespan RuntimeError × TestClient — design's claim holds.** All existing tests use bare module-level `TestClient(app)` (no context manager), so `lifespan` startup never runs under pytest. `conftest.py` sets the env var unconditionally, covering any future `with` usage. ✓

b) **"Rotation needs no restart" is overstated.** `config.get_key` checks `os.environ` live, but `HERMES_ENV` is snapshotted at import (`config.py:33`). Rotating the token via `os.environ` works without restart; editing `~/.hermes/.env` post-start does **not** take effect until the daemon restarts. The design should say exactly that.

c) **`decision_log.py` sketch bugs:** uses `_SNIPPET_RE` without `import re`, and `Optional` without importing it. (Sketch-level; the implementer will hit it immediately.)

d) **Memory-check interference in new tests.** `classify_request`'s memory step (step 2) runs before T3 patterns and consults the on-disk incident DB. The new `test_bookkeeping_words_not_tier3` asserts exact `tier == 2`; a sufficiently similar previously-recorded incident (e.g. from `test_feedback_and_memory_escalation`, which writes to the real DB) could escalate a bookkeeping prompt off Tier 2 and make the test order-dependent/flaky. The design should mandate memory isolation in `test_blocker_fixes.py` (tmp `REFLEX_DB`/monkeypatched `check_memory`).

e) **`test_decision_log_written`'s `prompt_sha256` check** needs "last user content" defined identically in the server hook and the test (last user-role message). Minor spec ambiguity to pin down.

f) `start.sh` sources both env files as the design claims ✓; the `export $(… | xargs)` word-splitting is fine for hex tokens.

g) The Blocker-1 test rewrite's 7 kept keywords (`indemnification, statutory, blast radius, delaware, oar 414, erdc, subpoena`) all match new pattern[3] ✓ (verified), and the `f"Please process the {kw} requirements…"` template introduces no cross-pattern interference (verified by the end-to-end run in §2).

---

## Required changes before implementation (GO-WITH-CHANGES)

1. **Redesign catalog epoch/snapshot** (§8): `snapshot(epoch)` is unimplementable on `INSERT OR REPLACE` + `PRIMARY KEY (id, provider)`. Add a history table or epoch-scoped PK, and spell out the `upsert_models` INSERT + dataclass changes (they are not additive).
2. **Fix consolidation's memory-escalation profile** (§3): preserve `get_memory_escalation_defaults` for the memory branch; the single-profile lookup silently changes routing for memory-escalated Tier-2 prompts.
3. **Correct the auth blast-radius enumeration** (§4): 20 TestClient sites across the 3 files (exact file:line list above), and call out `test_p0_4`'s status-code loop as semantically sensitive to the header.
4. **Correct the tuple-change enumeration** (§5): 8 unpack sites in `test_router.py`; `/v1/route` bypasses `select_candidate_routes` (needs its own `log_decision` call — specify it); `preflight_and_stream`'s 4-tuple return/unpack (`server.py:372,550`) must change for the `supersedes` chain.
5. **Merge, don't replace, gateway headers** in `mcp_server.py:125` (§6).
6. **Qualify the rotation claim** (§10b) and **fix the `decision_log.py` sketch imports** (§10c).
7. **Isolate memory in the new test module** (§10d); pin the `prompt_sha256` definition (§10e); drop dead imports in consolidated `solver.py` (§3).
8. Fix the `finance` vs `accounting` domain attribution (§2) if any downstream logic keys on domain name.

Nothing here is fatal to the approach. With these corrections the design is implementable as specified.

---

## Implementation addendum (2026-09-29, implementer)

Corrections 1–3 and 5–8 were applied as specified. Correction 4's *structural*
finding (one `select_candidate_routes` call site; `/v1/route` bypasses it and
needs its own hook) was applied, but its *mechanical* plan was written against
a stale `server.py` shape and was **adapted during implementation**:

- Real `select_candidate_routes(payload, session_id, access_method)` is sync
  and returns `(subscription_route, metered_route, tier_num, explanation)` — a
  4-tuple kept unchanged. The 5-tuple/`decision_id`-threading plan was dropped
  (it would have required touching 8 test unpack sites and both
  `preflight_and_stream` return sites for no behavioral gain).
- `solver.arbitrate` returns `(primary, metered, explanation)`; there is no
  `route_category` on route dicts (`preflight_and_stream` computes it locally).
- Logging moved to the endpoint handlers (`/v1/route`, streaming and
  non-streaming `/v1/chat/completions`) via a `_log_route_decision` helper.
  Speculative-failover supersedes chaining: the superseded plan is logged with
  `route_category="planned_superseded"`, then the executed decision with
  `supersedes=<plan decision_id>`. This records the *executed* route, which the
  reviewed design did not.
- `REFLEX_BLOCKER_FIX_DESIGN.md` Blocker 5 (server hooks) was re-corrected to
  match the shipped code; it remains the source of truth.

Additional implementation-time adaptations (documented, behavior-neutral):

- `decision_log.py` replay works from the record's own fields
  (`catalog_epoch` → `catalog.snapshot(epoch)`); no separate snapshot helpers
  were needed. **Correction to this addendum (2026-09-29, post-review):** an
  earlier draft of this addendum claimed `snapshot_epoch`/`snapshot_timestamp`
  helpers were added to `decision_log.py` — they were not and do not exist.
- `reflex_client.py` 429 handling honors `Retry-After` once then retries exactly
  once (the reviewed sketch slept without issuing the retry).
- Mac-only harness tests (`agy`/`opencode`/`goose` installed+authenticated)
  skip with a stated reason on hosts without them via a `requires_harnesses`
  marker in `tests/conftest.py`; the agy/opencode assertions in
  `test_harnesses_endpoint` are conditional on binary presence. A session
  autouse fixture seeds a 5-model synthetic catalog when the real one is empty
  (removes only its own rows at teardown), so the suite is green from a cold
  `~/.reflex/catalog.db`.
