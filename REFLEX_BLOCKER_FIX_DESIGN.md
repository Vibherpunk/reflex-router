# Reflex Router — 5-Blocker Fix Design (v2.3.0)

**Status:** DESIGN ONLY — not implemented.
**Date:** 2026-09-29
**Revision 2026-09-29 ~13:55 PDT:** incorporated all 8 required corrections from
`REFLEX_DESIGN_REVIEW.md` (§1–§8: catalog history-table redesign, memory-escalation
profile fix, 20-site auth enumeration, tuple-change enumeration, header merge,
rotation qualification, test memory isolation, finance/domain attribution) plus the
launchd token-provisioning correction (plist `EnvironmentVariables`, not
`~/.hermes/.env`, for the live daemon). This doc is the single source of truth.
**Scope:** `~/workspace/reflex-router` (Python FastAPI gateway, live on Mac at `127.0.0.1:8787`)
**Posture:** Advisory-only wiring (Reflex suggests, caller decides). No production deploy without Adam's YubiKey-signed gate; Mac daemon restart is Adam's hands.

Design principle throughout: smallest diff that permanently closes the blocker, single source of truth for every decision, every behavior pinned by a regression test.

---

## Blocker 1 — Tier-3 pattern over-rotation (bare "audit" / "reconcile" / "ledger")

### Problem
`classifier.py::TIER3_PATTERNS[3]` contains bare-word alternations `reconcile|ledger|audit`. Routine bookkeeping ("reconcile my QBO feed") matches Tier 3 → most expensive frontier models. Behaviorally proven 2026-09-29.

### Design

**File: `classifier.py`** — one-line change plus rationale comment:

```python
TIER3_PATTERNS = [
    r"(?i)(?:^|\s)(--deep|#hard|#architect|#spec)(?:\s|$)",
    r"(?i)\b(system architecture|architecture|rfc|specification|database migration|distributed|consensus|byzantine|split-brain)\b",
    r"(?i)\b(zero[- ]downtime|formal verification|cryptographic audit|security vulnerability|threat model)\b",
    # NOTE (2026-09-29, blocker fix): bare bookkeeping words "audit", "reconcile",
    # "ledger" were REMOVED from Tier 3. They now fall through to the protected-domain
    # hard floor (accounting/finance/compliance -> Tier 2). Genuinely high-consequence
    # terms stay: indemnification, statutory, subpoena, delaware, oar 414, erdc,
    # blast radius. "cryptographic audit" stays Tier 3 via pattern [2] (multi-word).
    r"(?i)\b(indemnification|statutory|blast radius|delaware|oar[- ]?414|erdc|subpoena)\b"
]
```

No change needed in `solver.py` — it imports `TIER3_PATTERNS` from `classifier`, so the fix propagates automatically (this import structure is load-bearing; do not duplicate the list).

**Resulting classification for the problem prompts** (verified by reading the decision chain):
- `"reconcile my QBO feed"` → no T3 match → no T2 match → **step 4b bookkeeping pattern** → **Tier 2**. (Implementation note: a `TIER2_BOOKKEEPING_PATTERNS` step was added between the T2 patterns and the domain floor, so bare bookkeeping words are guaranteed Tier 2 even when no domain keyword matches. The domain floor would also have caught these three prompts; 4b is belt-and-suspenders.)
- `"audit last month's transactions"` → step 4b → **Tier 2**.
- `"show me the general ledger balance"` → step 4b → **Tier 2**.
- `"design a byzantine fault-tolerant distributed consensus protocol"` → pattern[1] → **Tier 3** (unchanged).
- `"perform cryptographic audit and formal verification of the enclave"` → pattern[2] → **Tier 3** (unchanged; this is the existing `test_effort_routing.py` case).

**File: `tests/test_audit_patches.py`** — rewrite `test_p0_1_tier3_high_consequence_keywords`:

```python
def test_p0_1_tier3_high_consequence_keywords():
    """High-consequence legal/security/architecture terms must stay Tier 3."""
    tier3_terms = [
        "indemnification", "statutory", "blast radius",
        "delaware", "oar 414", "erdc", "subpoena",
    ]
    hard_prompts = [
        "design a byzantine fault-tolerant distributed consensus protocol",
        "prove correctness of this split-brain recovery algorithm",
        "perform cryptographic audit and formal verification of the enclave",
        "plan a zero-downtime database migration for the payments cluster",
        "write an rfc for the new system architecture",
        "assess the threat model for our auth service",
    ]
    for kw in tier3_terms:
        prompt = f"Please process the {kw} requirements for this task."
        tier, reason = classify_request([{"role": "user", "content": prompt}])
        assert tier == 3, f"Keyword '{kw}' must stay Tier 3: got Tier {tier} ({reason})"
    for prompt in hard_prompts:
        tier, reason = classify_request([{"role": "user", "content": prompt}])
        assert tier == 3, f"Hard prompt must stay Tier 3: got Tier {tier} ({reason})"

def test_p0_1_bookkeeping_words_not_tier3():
    """Bare bookkeeping words must NOT reach Tier 3 (cost defect, 2026-09-29)."""
    cases = [
        "reconcile my QBO feed",
        "audit last month's credit card transactions",
        "show me the general ledger balance",
    ]
    for prompt in cases:
        tier, reason = classify_request([{"role": "user", "content": prompt}])
        assert tier != 3, f"Bookkeeping prompt routed to Tier 3: '{prompt}' ({reason})"
        assert tier == 2, f"Bookkeeping prompt should land on domain floor Tier 2: '{prompt}' got Tier {tier}"
```

### Blast radius
- **Positive:** prompts with bare audit/reconcile/ledger now route to cheaper models. This is the intended cost fix.
- **Accepted trade-off:** a genuinely high-stakes prompt like "audit this smart contract for vulnerabilities" drops T3→T2. Mitigated: `security vulnerability` still matches T3 pattern[2]; compliance/security domain floor guarantees Tier ≥ 2. Documented, not gold-plated.
- **Tests affected:** `test_audit_patches.py::test_p0_1_tier3_high_consequence_keywords` (rewritten per above). `test_router.py` — grep shows no bare audit/reconcile/ledger Tier-3 assertions; safe. `test_effort_routing.py:83` uses "cryptographic audit and formal verification" → still T3 via pattern[2]; safe.
- **`solver.py`:** zero changes (imports the list).

### Rollback
Single-line revert of the regex in `classifier.py` (`git revert`). No state, no migration.

---

## Blocker 2 — No gateway auth

### Problem
`server.py` has zero auth. Anyone reaching `127.0.0.1:8787` can spend Adam's provider keys (OpenRouter metered fallback is real money).

### Design

**New file: `auth.py`**

```python
"""Bearer-token gateway authentication. Fail-closed by design."""
import hmac
from fastapi import Request, HTTPException
from config import get_key

TOKEN_ENV_VAR = "REFLEX_GATEWAY_TOKEN"

def get_expected_token() -> str:
    """Lazy read: os.environ -> $HOME/.env -> $HOME/.hermes/.env (via config.get_key)."""
    return get_key(TOKEN_ENV_VAR)

def token_configured() -> bool:
    return bool(get_expected_token())

async def verify_gateway_token(request: Request) -> None:
    """FastAPI dependency. 401 on bad/missing token, 503 if server misconfigured."""
    expected = get_expected_token()
    if not expected:
        # Fail closed: a misconfigured gateway never serves.
        raise HTTPException(status_code=503, detail="Gateway auth not configured")
    auth = request.headers.get("authorization", "")
    scheme, _, presented = auth.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(status_code=401, detail="Missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    # Constant-time comparison: no length/oracle leakage.
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
```

Token is read lazily per request (via `get_key`, which checks `os.environ` first — cheap)
so tests can inject it. Rotation via `os.environ` needs no restart; rotation via
`~/.hermes/.env` edit needs a restart (import-time snapshot — see Provisioning note).

**File: `server.py`**

```python
from fastapi import Depends
from auth import verify_gateway_token, token_configured
from config import CONFIG, VERSION, get_key
```

- Add `dependencies=[Depends(verify_gateway_token)]` to every protected route decorator:
  `GET /v1/models`, `POST /v1/catalog/refresh`, `POST /v1/feedback`, `GET /v1/stats`,
  `GET /v1/harnesses`, `POST /v1/route`, `POST /v1/delegate`, `POST /v1/chat/completions`.
- **Stay open:** `GET /health`, `GET /` (health checks, no keys material). The health payload exposes provider names + breaker states only — no secrets. Documented as intentional.
- **Fail-closed startup** in `lifespan`, before opening the connection pool:

```python
if not token_configured():
    raise RuntimeError(
        "REFLEX_GATEWAY_TOKEN is not set (checked $REFLEX_GATEWAY_TOKEN, "
        "$HOME/.env, $HOME/.hermes/.env). Refusing to start an unauthenticated "
        "gateway. Provision with: openssl rand -hex 32 >> appended to ~/.hermes/.env"
    )
```

**File: `start.sh`** — no code change needed (already sources both env files). Add a comment documenting the token requirement.

**File: `mcp_server.py`** — it calls the gateway over HTTP (`ROUTER_BASE_URL`). Add:

```python
_REFLEX_TOKEN = os.environ.get("REFLEX_GATEWAY_TOKEN", "")
def _gateway_headers() -> dict:
    return {"Authorization": f"Bearer {_REFLEX_TOKEN}"} if _REFLEX_TOKEN else {}
```

and merge the bearer header into existing headers on all httpx calls to `ROUTER_BASE_URL`
— **merge, never replace**: `mcp_server.py:125` already passes
`headers={Content-Type, X-Reflex-Delegation-Depth, X-Reflex-Delegation-Chain, X-Reflex-Caller}`;
implement as `{**existing_headers, **_gateway_headers()}` or the delegation headers
will be clobbered (review §6).

**File: `federation.py`** — `_fallback_gateway()` (~line 462) uses urllib against `/v1/chat/completions`. Add `Authorization: Bearer` header from `os.environ.get("REFLEX_GATEWAY_TOKEN", "")`.

**Provisioning — CORRECTED 2026-09-29 (review + deploy recon):** the live Mac daemon runs
under launchd (`com.reflex.router` plist), which does **not** source `~/.env` or
`~/.hermes/.env` — it only sees its plist `EnvironmentVariables` plus the process
environment. So there are two provisioning paths:

- **Live daemon (launchd):** `REFLEX_GATEWAY_TOKEN` must be present in the plist's
  `EnvironmentVariables` dict (deployed via plist edit), **before** the restart.
  The daemon refuses to start without it (fail-closed `lifespan` check) — token
  into plist first, then restart with:
  `launchctl kickstart -k gui/$(id -u)/com.reflex.router`
- **Manual runs (`start.sh`):** `printf 'REFLEX_GATEWAY_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env`
  (start.sh sources both env files).

**Rotation semantics (qualified per review §10b):** `config.get_key` checks `os.environ`
live on every call, but `HERMES_ENV` is an import-time snapshot. Rotating the token via
`os.environ` takes effect **without** a restart; editing `~/.hermes/.env` post-start
needs a daemon restart; editing the plist always needs a restart.

### Blast radius
- **Existing tests:** `test_audit_patches.py`, `test_router.py`, `test_effort_routing.py` use bare `TestClient` against now-protected endpoints → all break. Fixed by new `tests/conftest.py` (sets `REFLEX_GATEWAY_TOKEN=test-token` in `os.environ` before `server` import) plus passing `headers=auth_headers` at the protected call sites (3 files; ~8 call sites — enumerated in Blocker 4).
- **Existing local clients:** anything calling the gateway (future VibeHard wiring, Adam's scripts, MCP server, federation fallback) must send `Authorization: Bearer`. This is a flag day for local callers — documented in `CLIENT_CONTRACT.md`.
- **`/health` stays open by design** — acceptable: no key material in the payload.
- **Daemon restart risk:** if the token isn't provisioned before restart, the daemon exits loudly (RuntimeError in lifespan, non-zero exit). This is the intended fail-closed behavior, but the token MUST be provisioned first — sequencing note for whoever restarts the Mac daemon.

### Rollback
Revert `server.py` decorators + `lifespan` check; delete `auth.py`; restart daemon. Old unauthenticated local clients work again immediately.

---

## Blocker 3 — Mac-as-SPOF: fail-closed client contract

### Problem
If the Mac daemon dies, VPS builds consulting Reflex must not hang or fail. Advisory-only posture: Reflex suggests, the caller decides.

### Design

**New file: `CLIENT_CONTRACT.md`** (repo root — this is the contract, written for human and agent callers):

- Transport: `HTTP POST` to `$REFLEX_URL` (default `http://127.0.0.1:8787`), header `Authorization: Bearer $REFLEX_GATEWAY_TOKEN`.
- **Timeouts:** connect ≤ 2s; `/v1/route` read ≤ 10s; `/v1/chat/completions` read = caller-defined (streaming).
- **Retry:** exactly ONE retry on transport error, timeout, or HTTP 503, with 250ms fixed backoff. Never retry 4xx. Honor `Retry-After` once on 429.
- **Fallback (fail-closed to local default):** after retry exhaustion, the caller MUST NOT raise into its own build path. It returns a local default suggestion and continues:

```json
{
  "available": false,
  "tier": 0,
  "effort": "low",
  "model": "<caller's own default model>",
  "provider": "local-default",
  "reason": "reflex_unavailable: <cause: refused|timeout|503|auth>"
}
```

- **Logging:** emit one structured event per fallback: `reflex_unavailable` with `cause`, `latency_ms`, `retries`. NEVER log prompt text or the bearer token.
- **Health caching (optional):** callers MAY cache `GET /health` for 30s to avoid hammering a dead daemon; not required.

**New file: `reflex_client.py`** — tiny reference client (~120 lines, `httpx` only, already a dependency):

```python
@dataclass
class RouteSuggestion:
    available: bool
    tier: int
    effort: str
    model: str
    provider: str
    reason: str

def suggest_route(messages, model="auto", base_url=None, token=None,
                  timeout=10.0, default_model="local-default") -> RouteSuggestion:
    """Advisory-only Reflex client. NEVER raises on Reflex outage.

    Fail-closed contract: one retry on transport/503, then local default.
    """
```

Implementation: POST `/v1/route` with bearer header, `httpx.Timeout(connect=2.0, read=timeout)`; catch `httpx.TransportError`/`TimeoutException`/503 → one retry after 0.25s → return `RouteSuggestion(available=False, ...)`. Log `reflex_unavailable` via stdlib `logging` (no prompt text). Map `/v1/route` response fields to the dataclass on success.

### Blast radius
None — two new files, zero changes to existing code paths. The contract constrains *future* callers only.

### Rollback
Delete the two files.

---

## Blocker 4 — Regression suite

### Design

**New file: `tests/conftest.py`**

```python
import os
os.environ.setdefault("REFLEX_GATEWAY_TOKEN", "test-token-reflex-local")
import pytest

@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test-token-reflex-local"}
```

(`os.environ` is set before `server` import because conftest loads first. The lazy per-request token read in `auth.py` makes this robust regardless of import order.)

**New file: `tests/test_blocker_fixes.py`** — pins all five blockers:

| Test | Pins |
|---|---|
| `test_hard_prompts_stay_tier3` | 6/6 hard prompts → Tier 3 (same list as Blocker 1 design) |
| `test_bookkeeping_words_not_tier3` | "reconcile my QBO feed", "audit …", "general ledger …" → Tier 2, never 3 |
| `test_health_open_without_auth` | `GET /health` → 200 with no `Authorization` header |
| `test_protected_endpoints_require_auth` | `/v1/route`, `/v1/models`, `/v1/stats`, `/v1/chat/completions` → 401 without header |
| `test_wrong_token_rejected` | wrong token → 401 (wrong-length and wrong-value both 401 — no oracle) |
| `test_token_guard_fail_closed` | `verify_gateway_token` with empty env → 503 (monkeypatched) |
| `test_client_fail_closed_on_dead_port` | `reflex_client.suggest_route` vs `127.0.0.1:9` → `available=False`, no exception |
| `test_client_single_retry_then_fallback` | mock transport: first attempt timeout, assert exactly 2 attempts then fallback |
| `test_decision_log_written` | POST `/v1/route` (authed) → JSONL line exists with all schema fields; `prompt_sha256 == sha256(last_user_content)`; raw prompt text NOT in the line |
| `test_decision_replay` | rebuild from logged `catalog_epoch` snapshot → same model/provider |
| `test_catalog_epoch_logged` | two decisions across a catalog refresh carry different epochs |

**Updates to existing tests — CORRECTED enumeration (review §4): 20 TestClient call sites
across 3 files** (the design's "~8" was undercounted). Every site below gets
`headers=auth_headers` (the `conftest.py` fixture); `/health` stays open and needs none.

- `tests/test_audit_patches.py` — 4 sites:
  `:68` `POST /v1/chat/completions` (`test_p0_2`), `:110` `POST /v1/chat/completions`
  (`test_p0_4`), `:127` + `:132` `POST /v1/route` (`test_p1_1`, two calls).
  **Semantic subtlety — `test_p0_4` (line 101):** it loops `client_error_codes =
  [400, 401, 404, 422]` asserting `resp.status_code == code`. Auth runs before provider
  logic, so without the header *every* iteration returns 401 — the 400/404/422 assertions
  fail and the 401 case passes for the wrong reason. The header here is not mechanical;
  it is required for the test to keep testing what it claims.
- `tests/test_effort_routing.py` — 5 sites: `:141`, `:151`, `:159`, `:167`
  `POST /v1/route`; `:257` `POST /v1/chat/completions`.
- `tests/test_router.py` — 11 sites: `:27` `GET /v1/models`, `:129` `POST /v1/feedback`,
  `:141` `GET /v1/stats`, `:149` `GET /v1/harnesses`, `:159`+`:172` `POST /v1/delegate`,
  `:163` `POST /v1/route`, `:279`, `:309`, `:340`, `:367` `POST /v1/chat/completions`.

`tests/test_federation.py:191` patches `federation.call_reflex_http_gateway` (no real
gateway) — safe. `tests/benchmark_30_prompts.py` makes no HTTP calls — safe. No other
test files import `TestClient`.

**Memory isolation (review §10d):** `classify_request`'s memory step consults the on-disk
incident DB before T3 patterns. The new `test_blocker_fixes.py` must isolate memory —
monkeypatch `classifier.check_memory` to return `None` (or point `REFLEX_DB` at a tmp
path) — or the exact-`tier == 2` assertions become order-dependent/flaky (e.g. after
`test_feedback_and_memory_escalation` writes real incidents).

**`prompt_sha256` definition (review §10e):** sha256 of the **last user-role message's
content string** (`str(msg.get("content",""))`). The server hook and the test must use
this identical definition.

### Blast radius
- New files are additive. The `conftest.py` env default changes nothing for tests that don't touch auth (token is only read on protected requests and at daemon startup via `lifespan`, which `TestClient` exercises only under a context manager — existing tests don't use one).
- Risk: a test that *does* enter the lifespan context without the env var would hit the fail-closed RuntimeError. `conftest.py` sets the var unconditionally, so this can't happen under pytest.

### Rollback
Delete new test files; revert the `test_audit_patches.py` rewrite and header additions.

---

## Blocker 5 — Non-reproducible routing (decision logging)

### Problem
Hourly catalog refresh changes routing decisions with no record. Post-hoc, nobody can answer "why did prompt X route to model Y at time T?"

### Design

**File: `catalog.py`** — version the catalog. **REDESIGNED 2026-09-29 (review §8):** the
original sketch (an `epoch` column on `models`) is unimplementable — `models` uses
`PRIMARY KEY (id, provider)` with `INSERT OR REPLACE`, so every refresh destroys prior
epochs and `snapshot()` could never recover history. Corrected design: an append-only
history table.

```python
class ModelCatalog:
    def __init__(self, ...):
        ...
        self.catalog_epoch: int = 0        # bumped on every successful refresh
        self.last_refresh_ts: float = 0.0  # wall-clock of last successful refresh

    def refresh_from_providers(self, force=False):
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            ...  # existing discovery -> upsert_models (unchanged)
            self.catalog_epoch += 1
            self.last_refresh_ts = time.time()
            self._persist_meta()            # catalog_meta(key,value): epoch, last_refresh_ts
            self._append_history_snapshot() # copy current models rows -> models_history
        finally:
            self._refresh_lock.release()

    def version_info(self) -> Dict[str, Any]:
        return {"catalog_epoch": self.catalog_epoch,
                "catalog_refreshed_at": self.last_refresh_ts}

    def snapshot(self, epoch: int) -> List[ReflexModelDefinition]:
        """Rows as they were at `epoch`: the history rows with the greatest epoch <= requested."""
        ...
```

- `_init_db` gains (following the existing try/except `CREATE TABLE` pattern):
  `models_history` with the **same columns as `models`** plus `epoch INTEGER NOT NULL`,
  `PRIMARY KEY (epoch, provider, id)`; and `catalog_meta(key TEXT PRIMARY KEY, value TEXT)`.
- `_append_history_snapshot()`: `INSERT INTO models_history SELECT <cols>, :epoch FROM models`
  (column list explicit — never positional-`*`).
- On `__init__`, restore `catalog_epoch`/`last_refresh_ts` from `catalog_meta` (default 0).
- `snapshot(epoch)`: single SQL query —
  `SELECT * FROM models_history WHERE epoch = (SELECT MAX(epoch) FROM models_history WHERE epoch <= :epoch)`,
  mapped through `ReflexModelDefinition(**d)` exactly like `_load_cache`.
- **Deliberately NOT changed:** the live `models` table keeps its schema and
  `INSERT OR REPLACE` behavior; `upsert_models` is untouched. The history table is the
  only new write path, and it is append-only.

**New file: `decision_log.py`**

```python
"""Structured, append-only routing decision log (JSONL). Privacy: prompt SHA-256 only, never raw text."""
import hashlib, json, logging, os, re, time, uuid
from pathlib import Path
from typing import Any, Dict, Optional

DECISION_LOG_PATH = Path(os.getenv("REFLEX_DECISION_LOG", "~/.reflex/decisions.jsonl")).expanduser()
_MAX_BYTES = 64 * 1024 * 1024
_KEEP_ROTATIONS = 3

# Fields that may embed prompt fragments (memory auto-escalation includes a 35-char sample).
_SNIPPET_RE = re.compile(r"learned from prior failure in '.*?'")

def sanitize_reason(reason: str) -> str:
    return _SNIPPET_RE.sub("learned from prior failure in '[redacted]'", reason or "")

def _rotate_if_needed() -> None: ...

def log_decision(*, endpoint: str, session_id: str, prompt_text: str,
                 tier: int, effort: str, model: str, provider: str,
                 billing_type: str, reason: str, route_category: str,
                 catalog_epoch: int, catalog_refreshed_at: float,
                 spec_hash: str, token_count: int, latency_ms: float,
                 kv_latch_applied: bool = False,
                 memory_incident_id: Optional[int] = None,
                 supersedes: Optional[str] = None) -> str:
    """Appends one JSONL record. Returns decision_id. Never raises (logging must not break routing)."""
    ...
```

Record schema (every field required unless noted):

```json
{
  "decision_id": "uuid4",
  "ts": "2026-09-29T13:40:00.123Z",
  "endpoint": "/v1/route | /v1/chat/completions",
  "session_id": "…",
  "prompt_sha256": "hex(sha256(last_user_content))",
  "tier": 2, "effort": "medium",
  "model": "…", "provider": "…", "billing_type": "subscription|metered",
  "reason": "<sanitized>",
  "route_category": "selected | failover_after_subscription_rate_limit | kv_latch | emergency_fallback",
  "catalog_epoch": 14, "catalog_refreshed_at": 1729…,
  "scoring_spec_sha256": "hex",
  "token_count": 1234, "latency_ms": 3.2,
  "kv_latch_applied": false, "memory_incident_id": null,
  "supersedes": null
}
```

- `scoring_spec_sha256`: hash of the effective `scoring_spec.yaml` (repo + user overlay); cached, recomputed on mtime change. Add `scoring_spec.get_spec_hash()` helper.
- Rotation: on open, if file > 64MB, rotate `.1`→`.2`→`.3`, current→`.1`.
- `log_decision` catches all exceptions internally (logs to stderr) — a logging failure must never 500 a routing request.

**Replay** (`decision_log.py`):

```python
def replay_decision(record: dict, catalog: ModelCatalog, solver: ArbitrationSolver,
                    prompt_text: Optional[str] = None) -> dict:
    """Two-mode reproducibility check. Returns {'match': bool, 'detail': …}."""
```

- Mode A — **audit** (always possible, no prompt needed): rebuild a `CapabilityRequestVector` from the logged fields (tier → threshold profile from `scoring_spec`, logged effort/token_count), run `solver.arbitrate` against `catalog.snapshot(record["catalog_epoch"])`, assert same `(model, provider)`. This pins the solver half deterministically.
- Mode B — **full replay** (caller supplies the original prompt): verify `sha256(prompt) == record["prompt_sha256"]`, then run the full `extract_vector` + `arbitrate` pipeline against the epoch snapshot and assert identical `(tier, effort, model, provider)`.

**Honest non-determinism disclosure** (documented in the module docstring): Mode B can still diverge if, at decision time, (a) a memory incident fired (logged via `memory_incident_id` — replay with the same DB reproduces it), (b) session-affinity KV latch applied (logged via `kv_latch_applied` + `session_id`), or (c) circuit breakers were open (breaker states are point-in-time and intentionally not snapshotted). The catalog — the actual complaint — is fully pinned.

**File: `server.py`** — hook points. **RE-CORRECTED 2026-09-29 (implementation):**
the design above and review §5 were written against a stale `server.py` shape.
The real code:

- `select_candidate_routes(payload, session_id, access_method="http_gateway")`
  is **sync** and returns a **4-tuple**
  `(subscription_route, metered_route, tier_num, explanation)`. Its signature
  and arity are **unchanged** — 8 unpack sites in `test_router.py` plus the
  `/v1/chat/completions` call site depend on the 4-tuple. No `decision_id`
  threading, no endpoint param.
- `solver.arbitrate(...)` returns a **3-tuple** `(primary, metered, explanation)`;
  route dicts carry `model`/`provider`/`billing_type`/`effort`. There is no
  `route_category` on route dicts — `preflight_and_stream` computes it locally
  (`"subscription_zero_marginal_cost"`, `"metered_circuit_open_or_unconfigured"`,
  `"failover_after_subscription_rate_limit"`).
- `preflight_and_stream(sub_route, metered_route, payload, session_id, request)`
  returns `(generator, model_used, provider_used, route_category)` — the
  *executed* route after speculative failover.

Logging therefore happens at the endpoint handlers, via a small
`_log_route_decision(...)` wrapper (adds `catalog.version_info()`,
`get_spec_hash()`, `kv_latch_applied = explanation.startswith("KV-Cache Latch")`,
`memory_incident_id` parsed from `"Incident #N"` in the reason, and measured
`latency_ms`):

- `/v1/route` (which calls `solver.arbitrate` **directly**, bypassing
  `select_candidate_routes` — review §5's structural point stands): after a
  successful arbitrate, `log_decision(endpoint="/v1/route",
  route_category="preview", ...)`.
- `/v1/chat/completions` **streaming**: after `preflight_and_stream` returns,
  log the executed `(model_used, provider_used, category)`. When speculative
  failover fired (`(model_used, provider_used) != (sub_route...)`), first log
  the superseded plan (`route_category="planned_superseded"`), then the
  executed decision with `supersedes=<plan decision_id>`.
- `/v1/chat/completions` **non-streaming**: before returning the upstream
  `Response`, log the executed `target` with the same supersedes chaining when
  `target != sub_route` (covers both breaker-open-at-select and 429/5xx
  mid-request failover).

This records the *executed* route rather than just the planned one — strictly
more auditable than the original sketch. `token_count` comes from
`estimate_tokens(payload)`; prompt text for the hash via `_last_user_content`
(last user-role message, mirroring the classifier).

### Blast radius
- **Write path:** one small JSONL append per routing decision (buffered, O(1)). Negligible latency; wrapped in try/except so it can't break routing. Disk: ~500 bytes/decision.
- **`catalog.py`:** new columns/attributes are additive; `_init_db` migration follows the existing try/except pattern. `refresh_from_providers` gains two assignments under the existing lock.
- **`server.py`:** `select_candidate_routes` signature and 4-tuple return are
  **unchanged** (RE-CORRECTED 2026-09-29 — the 5-tuple plan in the earlier
  correction was written against a stale file shape; the real function is
  `(subscription_route, metered_route, tier_num, explanation)`). New code is
  additive only: `_last_user_content` + `_log_route_decision` helpers and five
  `log_decision` call sites at the endpoint handlers. `preflight_and_stream`'s
  return shape is unchanged. All 8 `test_router.py` unpack sites keep working
  untouched. `tests/test_effort_routing.py:16` imports `select_candidate_routes`
  but never calls it (dead import) — no change needed.
- **Privacy:** raw prompts never touch disk. The one existing leak vector — memory auto-escalation `reason` strings embedding a 35-char prompt sample — is redacted by `sanitize_reason` at the logging boundary (the in-memory/API reason is unchanged).

### Rollback
Remove the `log_decision` call sites (or set `REFLEX_DECISION_LOG=/dev/null`); drop the `epoch` column usage (column can stay — harmless). Decisions made while logging was on remain valid JSONL.

---

## Consolidation: `solver.py` vs `classifier.py` duplicate tier logic — DECISION: consolidate

The verifier's drift hazard is real: `solver.extract_vector` re-implements `classifier.classify_request` step-for-step (trojan-horse → memory → T3 → T2 → domain floor → T1 → length → T0). They have already diverged in explanation strings, and any future pattern fix must be applied twice.

### Design
- `classifier.classify_request(messages) -> (tier, explanation)` becomes the **single tier authority**. No logic changes to it in this consolidation.
- `solver.extract_vector` is reduced to:

```python
def extract_vector(self, messages, requested_model="auto"):
    ...
    tier_num, tier_explanation = classify_request(messages)   # single call, was 7 duplicated steps
    # CORRECTED (review §3): the memory-escalation branch uses WEAKER capability
    # defaults than the plain tier profile. Preserve that distinction or routing
    # changes for memory-escalated prompts (0.60/0.70 vs 0.90/0.75 at Tier 2 alters
    # the hard feasibility gate in arbitrate).
    if "Memory Auto-Escalation" in tier_explanation:
        profile = scoring_spec.get_memory_escalation_defaults(tier_num)
    else:
        profile = scoring_spec.get_tier_threshold_profile(tier_num)
    vector = CapabilityRequestVector(
        token_count=estimated_tokens,
        reasoning_depth=profile.get("reasoning_depth", ...),
        architecture_score=profile.get("architecture_score", ...),
        tool_calling=has_tools,
        modality="text",
        tier_num=tier_num,
        explanation=tier_explanation,
        domain=detected_domain,
        effort=<effort>,
    )
    # effort overrides (#think/#reason -> medium, #spec/#architect/--deep -> high) STAY in solver:
    # they are effort-mapping, not tier classification.
    ...
```

- `solver.py` drops its imports of `TIER3_PATTERNS, TIER2_PATTERNS, TIER1_PATTERNS` (keep `PROTECTED_DOMAINS` — still used by `arbitrate`'s flash-model floor) and its duplicate `record_incident` call in the trojan-horse branch. **Dead-import hygiene (review §3):** after consolidation, `solver.py`'s `import re`, `detect_tool_errors`, `check_memory`, and `record_incident` become dead (all uses were in the deleted branches, lines 100–179) — remove them.
- **Incident-counting correction (review §3):** nothing in production currently calls both `classify_request` and `extract_vector` on the same messages (`classify_request` appears nowhere in `server.py`/`mcp_server.py`/`federation.py`/`reflex.py`), so there is no live double-record today. Consolidation is behavior-neutral on incident counting, not "strictly more correct."
- `detect_tool_errors` / `classify_domain` remain in `classifier.py` as helpers; solver keeps importing them only where still needed (`classify_domain` for the vector's domain field and the flash floor).

### Blast radius
- **Explanation strings change** on solver-driven paths (e.g. "Matched Tier 3 Architecture pattern: …" → classifier's "Matched Tier 3 pattern: …"). Audited against pinned assertions in `test_router.py`: it asserts `"Tier 0"/"Tier 1"/"Tier 2"/"Tier 3" in reason` (classifier strings contain these ✓), `"Escalated" in reason` (✓), `"Memory Auto-Escalation" in reason` (✓), `"Explicit model override"`, `"Primary Sub"`, `"KV-Cache Latch locked"` (all in `arbitrate`, untouched ✓). **No test_router.py assertion breaks.**
- **Incident double-record removed:** `hit_count` increments once per trojan-horse detection instead of twice — strictly more correct.
- Behavior-neutral by construction **except the memory-escalation branch** (see the
  `"Memory Auto-Escalation"` profile branch above — required, review §3): the
  consolidated code path otherwise executes the same checks in the same order as
  `classify_request` does today. The only string change on solver-driven paths is
  `vector.explanation` embedded in `arbitrate`'s final explanation (`solver.py:428`:
  `Fallback: … [{vector.explanation}]`), e.g. `"Matched Tier 3 Architecture pattern: …"`
  → `"Matched Tier 3 pattern: …"` — no test pins the old strings (verified by grep).

### Rollback
Revert `solver.py` (single file).

---

## Version bump: 2.2.0 → 2.3.0

- **File: `config.py`** — add the single source of truth: `VERSION = "2.3.0"`.
- **File: `server.py`** — `FastAPI(..., version=VERSION)` and `"version": VERSION` in `/health` (replaces the two hardcoded `"2.2.0"` strings).
- **File: `pyproject.toml`** — `version = "2.3.0"` (also fixes existing drift: it says `2.1.0` today).
- `mcp_server.py`'s `version="2.1.0"` is the MCP interface name version — leave untouched, note the distinction here.

Blast radius: none functional. Rollback: revert three strings.

---

## Implementation sequencing (for the implementer)

1. Blocker 1 (regex + test rewrite) — smallest, unlocks the cost fix.
2. Consolidation (solver/classifier) — do BEFORE writing new tests, so tests pin the final explanation strings.
3. Blocker 2 (auth) + `tests/conftest.py` + header updates to existing tests.
4. Blocker 5 (catalog epoch + decision_log + server hooks).
5. Blocker 3 (contract doc + reference client — no code dependencies).
6. Blocker 4 (new regression module).
7. Version bump.
8. Full `pytest` run; all green required.

## Open decisions needing Adam

**None.** Every choice above is within the approved scope: the auth design uses his existing env-file provisioning, the fail-closed behaviors are the ones he already approved in principle (advisory-only wiring, no silent fallbacks), and nothing here posts, purchases, deletes, or touches production.

**Operational note — CORRECTED:** the Mac daemon runs under launchd (`com.reflex.router`
plist), which does NOT source `~/.hermes/.env`. Whoever deploys must put
`REFLEX_GATEWAY_TOKEN` into the plist's `EnvironmentVariables` dict **BEFORE** restarting,
or the daemon will loudly refuse to start (fail-closed, by design). Restart command:
`launchctl kickstart -k gui/$(id -u)/com.reflex.router`. The `~/.hermes/.env` path is
for `start.sh`/manual runs only.
