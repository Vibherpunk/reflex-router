# Adversarial Review: "Dynamic Multi-Provider Capability Routing Engine" Proposal

Reviewed against the actual current implementation (`config.py`, `server.py`, `circuit_breaker.py`,
`classifier.py`, `federation.py`), not just the proposal text in isolation. Verdict up front: **do not
merge as written**. The proposal solves the discovery problem for exactly one provider (OpenRouter),
fakes it for the other three, and its integration with the existing async gateway is incomplete to the
point of guaranteed runtime crashes (`AttributeError` on every request once a breaker is present,
`KeyError` on `/health` and `/v1/route`, `KeyError`/no-op on any `cli_harness` model selected for
`/v1/chat/completions`). Below is the line-by-line case.

---

## 1. Architectural & Discovery Flaws

### 1.1 The "zero hardcoded models" claim is false for 3 of 4 discovery functions

- `_discover_opencode_go`: `raw_models = [("deepseek-v4-flash", "DeepSeek V4 Flash", 0.1, 0.4, 0.7, 0.95, 128_000), ...]`
  — seven fully hardcoded model IDs, display names, and *fabricated* capability floats, with no
  citation, benchmark, or API response backing any of the numbers (why is `qwen3.7-max`
  `reasoning_capability=0.95` and `deepseek-v4-pro` `0.9`? There is no way to know; they're invented).
- `_discover_cli_harnesses`: hand-writes two complete `ReflexModelDefinition` literals for
  `claude-sonnet-5` and `claude-opus-5.5`, gated only on `which claude` succeeding. It doesn't ask the
  `claude` binary what models it can actually reach — it just assumes those two IDs exist and are
  current, forever, until someone edits this Python file again.
- `_discover_gemini_models`: same pattern, two hardcoded `ReflexModelDefinition` literals, gated only
  on `GEMINI_API_KEY` being present in the environment.
- Only `_discover_openrouter_models` is genuinely dynamic — it calls `GET /api/v1/models` and builds
  `ReflexModelDefinition`s from the live response (with a hacky-but-live substring heuristic for
  `is_reasoning`/`is_arch`).

This isn't a refinement of the "zero hardcoded model tables" directive — it's the *same* hardcoded
table that lived in `config.py`'s `CONFIG["providers"][x]["models"]` list, just moved into `catalog.py`
and given a friendlier name (`raw_models`). Net hardcoding in the system actually **increases**: the
existing `config.py` model lists and `federation.py`'s `HARNESS_SPECS` (which *also* hardcodes
`"claude": {"models": ["claude-3.7-sonnet", "claude-3.5-sonnet", "claude-3-opus"]}` — note, different
IDs than catalog.py's `claude-sonnet-5`/`claude-opus-5.5`!) are never touched or reconciled. Ship this
and you have **four** independent, silently-diverging sources of truth for "what Claude models exist":
`config.py`, `federation.py`, `catalog.py`, and reality.

### 1.2 How discovery should actually work

- **OpenCode-Go**: it already has an OpenAI-compatible `base_url` (`https://opencode.ai/zen/go/v1`
  per `config.py:72`). Call `GET {base_url}/models` with the stored key, exactly like
  `_discover_openrouter_models` does for OpenRouter. There is no reason this one is hardcoded — the
  gateway almost certainly exposes a models endpoint since it's OpenAI-shaped.
- **Gemini**: call `GET https://generativelanguage.googleapis.com/v1beta/models?key=...`, which
  returns live `inputTokenLimit`/`outputTokenLimit` per model — directly populates `context_window`
  and `max_output_tokens` without guessing.
- **CLI harnesses** (`claude`, `agy`, `goose`, `codex`): these don't expose a models-list subcommand
  reliably, so true dynamic discovery isn't fully achievable — the honest fix is to **stop pretending
  it's discovery** and treat it as configuration: read model identity from each harness's own local
  state (e.g. `claude`'s settings/model config file, or `claude --version`/doctor output if it embeds
  model IDs) instead of inlining them in `catalog.py`, and keep this list versioned in the
  already-declared-but-unused `providers.yaml` (`CONFIG_PROVIDERS_FILE`) so an operator can update it
  without a code change and a deploy.

### 1.3 Capability scores are the real unsolved problem

`reasoning_capability`, `architecture_score`, `coding_score`, `speed_score` are not things any
provider API exposes. No amount of "dynamic querying" produces them — they are inherently a maintained
opinion. The proposal's mistake isn't that it hardcodes these scores; it's that it *also* hardcodes
model identity in the same literals, conflating "which models exist" (should be dynamic) with "how
good is this model at X" (must be config/heuristic). Split them:

```python
# providers.yaml — the ONLY place capability opinions live, keyed by pattern not identity
scoring_overrides:
  - match: "claude-opus.*"      # regex against discovered model id
    reasoning_capability: 0.98
    architecture_score: 0.99
  - match: "claude-sonnet.*"
    reasoning_capability: 0.92
    architecture_score: 0.96
  - match: ".*-flash.*|.*mini.*"
    speed_score: 0.9
    reasoning_capability: 0.3
default:
  reasoning_capability: 0.5
  architecture_score: 0.5
  coding_score: 0.6
  speed_score: 0.6
```

Discovery produces identity + objective metadata (context window, pricing, tool support) from live
APIs; a small regex-based overlay (editable without touching Python) supplies the subjective scores.
Note `CONFIG_PROVIDERS_FILE` is declared in `catalog.py` and **never read anywhere** — it's a decorative
constant implying a config-driven design that doesn't exist yet.

---

## 2. Concurrency & SQLite Safety

### 2.1 No WAL mode, no busy timeout, no shared connection

`_init_db`, `_load_cache`, and `upsert_models` each open a fresh `sqlite3.connect(self.db_path)` with
no `PRAGMA journal_mode=WAL`, no `timeout=` argument (default 5s), and no `check_same_thread=False`
strategy. `refresh_from_providers()` is explicitly meant to run on an hourly **background thread**
(per the proposal's own Module C item 5) while the FastAPI event loop thread concurrently reads via
`list_all()`. Under default rollback-journal mode, a writer transaction (the whole `for m in models:
conn.execute(...)` loop inside one `with sqlite3.connect(...) as conn:` block is one transaction) holds
an exclusive lock for its duration; any second connection attempting to write within that window raises
`sqlite3.OperationalError: database is locked` once the 5s default timeout is exceeded. This gets worse,
not better, if the service is ever run with multiple worker processes (`uvicorn --workers N`), since
each worker keeps its own `ModelCatalog()`/`_memory_cache` and all of them write the same on-disk file
concurrently with no coordination beyond a per-process `threading.Lock` (`self._refresh_lock`), which
provides zero cross-process protection.

**Fix**: enable WAL + busy_timeout, and don't reopen connections per call:

```python
def _connect(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn
```

### 2.2 In-memory cache mutated in place, not swapped atomically

`upsert_models` does `for m in models: self._memory_cache[key] = m` — a partial, incremental mutation
of a dict that `list_all()` iterates from a different thread with no lock at all:

```python
def list_all(self) -> List[ReflexModelDefinition]:
    return list(self._memory_cache.values())
```

`dict.values()` iteration concurrent with another thread inserting keys is exactly the documented
CPython hazard (`RuntimeError: dictionary changed size during iteration` is possible; at minimum,
`list_all()` can return a torn view — half pre-refresh, half post-refresh models — mid-refresh).
**Fix**: build the new dict off to the side and swap the reference once, which *is* atomic under the
GIL:

```python
def upsert_models(self, models: List[ReflexModelDefinition]):
    with self._connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO models VALUES (...)", [...])
    new_cache = dict(self._memory_cache)
    for m in models:
        new_cache[f"{m.provider}::{m.id}"] = m
    self._memory_cache = new_cache  # single atomic pointer swap
```

### 2.3 `session_affinity` — the real risk is downstream of the breaker-integration bug, not the dict itself

As *written*, `ArbitrationSolver.arbitrate()` is fully synchronous with no `await` points, so within a
single call there's no interleaving hazard — asyncio only context-switches at `await`. The danger is
conditional: the moment someone "fixes" the (nonexistent, see §3) `is_healthy()` call by making
`arbitrate()` `async` and awaiting real breaker methods, an `await` lands **between** the KV-latch read
of `self.session_affinity.get(session_id)` and the write of `self.session_affinity[session_id] = {...}`.
Two rapid concurrent requests on the same `session_id` (a normal pattern — an agent loop firing several
tool-result turns back to back) can then both observe "not latched yet," both proceed to arbitrate
independently, and race to set divergent primary/fallback routes — precisely the KV-cache thrashing the
latch exists to prevent. **Fix**: guard with a per-session `asyncio.Lock` (a `defaultdict` of locks
keyed by `session_id`) around the read-check-write span, not just the dict access itself.

---

## 3. Arbitration Edge Cases & Failure Modes

### 3.1 `breaker.is_healthy()` does not exist — this is a hard crash, not an edge case

`circuit_breaker.py`'s `ProviderCircuitBreaker` exposes exactly four methods: `can_attempt()` (async,
mutating — grants/consumes the HALF_OPEN canary slot), `record_success()` (async), `record_failure()`
(async), and `get_status()` (sync, returns a status **dict**, not a bool). There is no `is_healthy`
method anywhere in the codebase. `solver.py` calls `breaker.is_healthy()` twice (once in the KV-latch
branch, once in the main filtering loop) — both calls raise `AttributeError: 'ProviderCircuitBreaker'
object has no attribute 'is_healthy'` unconditionally. Since `BREAKER_REGISTRY` is populated for every
subscription provider at import time (`server.py:32-42`), this attribute error fires on essentially
every call to `arbitrate()` the first time a subscription provider's breaker object is looked up — i.e.
the arbitration solver **cannot execute a single successful request** in its current form. This isn't a
tuning problem, it's non-functional code.

Even after adding the method, don't reach for `can_attempt()` as a substitute: it's **mutating** —
calling it to "peek" during ranking (potentially once per candidate model, per request) will itself
flip `OPEN → HALF_OPEN` and consume the single canary lease meant for the one real request that should
get it, starving the actual retry attempt. A health check for ranking purposes must be a pure read:

```python
# circuit_breaker.py — add a genuine non-mutating read
def is_healthy(self) -> bool:
    return self.state != BreakerState.OPEN
```

### 3.2 Metered providers get no circuit breaker at all — inherited from current code, made worse

`BREAKER_REGISTRY` (`server.py:32-42`) is only built `for name, p in CONFIG["providers"].items() if
p["type"] == "subscription"` — OpenRouter (`"metered"`) is excluded today, and the proposal doesn't
change this. `is_healthy = not breaker or breaker.is_healthy()` treats "no breaker registered" as
"always healthy." Result: a 402 (exhausted balance) or 429 (rate-limited) OpenRouter response never
trips anything, and `eligible_metered` keeps re-ranking and re-selecting that same dead provider on
every subsequent request, with the arbitration layer providing no backoff whatsoever — the actual
retry/backoff logic only exists downstream in `preflight_and_stream()`'s per-request failover, which
still only wraps the **subscription** breaker (`sub_breaker`), never a metered one. This is a
pre-existing gap in `server.py`/`circuit_breaker.py`, and the proposal makes it worse by fanning
`BREAKER_REGISTRY` lookups out across many more dynamically-discovered provider names
(`opencode-go`, `claude-cli`, `gemini`, arbitrary future `openrouter` sub-slugs) most of which will
**never** have a registered breaker because `BREAKER_REGISTRY` is still only built once, statically, at
startup, from `CONFIG["providers"]` — a dict the catalog doesn't populate or extend. Concretely,
`"claude-cli"` (the provider string `_discover_cli_harnesses` emits) has **no entry** in
`CONFIG["providers"]` at all (`config.py`'s provider keys are `opencode-go`, `gemini`, `openrouter`
only), so `BREAKER_REGISTRY.get("claude-cli")` is always `None` → always "healthy" → **zero circuit
protection for the CLI harness route, permanently**, no matter how many times it fails.

**Fix**: register breakers dynamically as new providers appear:

```python
def ensure_breaker(provider: str) -> ProviderCircuitBreaker:
    if provider not in BREAKER_REGISTRY:
        BREAKER_REGISTRY[provider] = ProviderCircuitBreaker(
            name=provider, **CONFIG["circuit_breaker"]
        )
    return BREAKER_REGISTRY[provider]
```
called from `catalog.upsert_models()` (or the solver, on first sight of a provider) for **every**
billing type, not just subscription.

### 3.3 "All providers down" emergency path silently bypasses the breaker anyway

When both `eligible_sub` and `eligible_metered` end up empty (everything unhealthy or over context),
the emergency branch recomputes candidates with **no** `is_healthy` filter at all:

```python
emergency = [m for m in models if m.context_window >= vector.token_count]
...
best = max(emergency, key=lambda x: x.reasoning_capability)
```

This deterministically re-selects the single highest-`reasoning_capability` model — very likely the
exact one that just tripped its breaker seconds ago — with no jitter, no backoff, on every request
during an outage. It's a built-in escape hatch that defeats the entire circuit-breaker subsystem the
rest of Plan B (see `circuit_breaker.py`'s canary/backoff machinery, and the multiple prior commits
hardening HALF_OPEN deadlock behavior) was carefully built to enforce.

### 3.4 `tier_num` breaks two existing endpoints outright

Module C says "Delete `model_tiers` dictionary in `config.py`." But:

- `server.py:143-161` (`/health`) does `"tiers": CONFIG["model_tiers"]` directly — deleting the dict
  makes this a `KeyError` on every health check.
- `server.py:203-218` (`/v1/route`) does `tier_info = CONFIG["model_tiers"][tier_num]` and then reads
  `tier_info["name"]`, `["subscription"]`, `["metered"]` — same `KeyError`, and this is a
  user/harness-facing preview endpoint, not incidental.

Neither endpoint is mentioned in Module C's integration steps. Both need to be rewritten in the same
change that deletes `model_tiers`, or `/health` and `/v1/route` ship broken.

Separately, the derived `tier_num` formula:

```python
tier_num = 3 if vector.architecture_score >= 0.8 else (2 if vector.reasoning_depth >= 0.7 else (1 if vector.reasoning_depth >= 0.3 else 0))
```

is not equivalent to `classifier.py`'s `classify_request()`, which encodes distinct *semantic*
escalation paths (Trojan Horse tool-error detection → forced tier 2, `memory.py`'s learned-incident
auto-escalation to an arbitrary historical tier, then three independent regex families for tiers 3/2/1).
The two numbers will disagree on real inputs (e.g., a security-audit prompt matches classifier's Tier 3
regex directly, but the solver's vector extraction only sets `a_score = 0.90` for a narrower keyword set
— `"security audit"` happens to be in both patterns by luck, but the sets aren't kept in sync anywhere,
and any future edit to one regex list silently desyncs from the other). Since `log_request(tier_num)`
(`server.py:393`) and `memory.py`'s incident tracking key off this integer, the meaning of "tier 2" also
stops being stable over time — it now depends on which candidate models happen to be in the catalog on
a given day, since the 0.7/0.8/0.3 boundary crossings are computed relative to a fitness scale scored
against a live, changing model set.

---

## 4. Concrete Code Bugs & Missing Pieces

1. **`solver.py`: `breaker.is_healthy()` — `AttributeError`, method does not exist.** (§3.1) Blocking bug.
2. **`ReflexModelDefinition` has no `base_url` / auth field.** `server.py`'s entire request path
   (`build_upstream_headers`, and both `preflight_and_stream` and the non-streaming branch of
   `chat_completions`) constructs the upstream URL via `f"{CONFIG['providers'][prov_name]['base_url']}/chat/completions"`.
   The catalog's `primary_route`/`metered_route` dicts (`{"provider", "model", "access_method",
   "billing_type"}`) carry no `base_url`. For a provider name the catalog invented that isn't a key in
   `CONFIG["providers"]` (`"claude-cli"` is the clearest case — it's not `"opencode-go"`, `"gemini"`, or
   `"openrouter"`), this is `CONFIG['providers']["claude-cli"]` → `KeyError`, an unhandled exception on
   every request that arbitrates to it. Module C never shows an updated `preflight_and_stream`/
   `chat_completions` that resolves connection info from the catalog instead of the static dict — the
   proposal only replaces the *selection* function, not the *execution* path that consumes its output.
3. **`access_method="cli_harness"` models are unreachable from `/v1/chat/completions` at all**, even if
   #2 is fixed with a lookup table. `claude-sonnet-5`/`claude-opus-5.5` (discovered via
   `_discover_cli_harnesses`) have no HTTP endpoint — the only existing code path that can invoke the
   `claude` binary is `federation.py`'s `delegate_subagent()` (subprocess spawn, JSON stdout,
   non-streaming, depth-limited), which is structurally incompatible with the streaming
   OpenAI-proxy shape `preflight_and_stream()` assumes. Selecting a `cli_harness` model as
   `primary_route` for a streaming chat request has no defined execution semantics in this proposal.
4. **Explicit `requested_model` that doesn't match any catalog entry silently falls through to
   auto-arbitration with no error and no log line**:
   ```python
   if requested_model and requested_model != "auto":
       for m in models:
           if requested_model == m.id or requested_model in m.id:
               ...
               return route, route, f"Explicit model override: {m.id}"
   # <-- no match: falls straight through into KV-latch / eligible_sub logic below,
   #     silently ignoring the caller's explicit request.
   ```
   Also `requested_model in m.id` is a substring match with no anchoring — a request for model id
   `"3"` would match any model whose id happens to contain the character `"3"` (e.g. `"qwen3.7-max"`,
   `"kimi-k3"`, `"gemini-2.5-pro"`), an unintentionally loose match that can silently route to the wrong
   model on short or generic requested-model strings.
5. **`_discover_cli_harnesses`/`_discover_openrouter_models` do blocking I/O (`subprocess.run`,
   `urllib.request.urlopen(timeout=3.0)`) with no async wrapper.** Fine if `refresh_from_providers()`
   only ever runs on a dedicated background thread as Module C describes — but nothing in the proposal
   stops a future "refresh now" trigger (e.g. hitting `/v1/models` and lazily refreshing on miss) from
   calling it inline from an `async def` route handler, which would block the entire single-threaded
   event loop for the duration of the network calls, stalling every other in-flight request.
6. **`refresh_from_providers`'s TTL gate uses a single global `min()` timestamp across all providers**:
   ```python
   oldest = min((m.last_updated for m in self._memory_cache.values()), default=0)
   if now - oldest < 3600.0: return
   ```
   One provider's stale/failed discovery (e.g. an auth file that briefly failed to parse) pins the
   *global* refresh cadence for every other provider, since there's no per-provider last-refreshed
   tracking. A provider that discovers zero models on a given pass is indistinguishable here from one
   that just hasn't been checked recently.
7. **`CONFIG_PROVIDERS_FILE = REFLEX_DIR / "providers.yaml"`** is declared and never read anywhere in
   the given code — a dead constant that implies a config-driven design that doesn't exist. No `yaml`
   import either, so wiring it up later adds an undeclared new dependency.
8. **`list_models()` / `/v1/models` and streaming are simply not addressed.** Module C's item 4 says
   "Returns all models from `catalog.list_all()`" as a one-line description with no code — it doesn't
   show the OpenAI-shaped mapping (`id`/`object`/`owned_by`/`permission`), doesn't say whether the
   `"auto"` pseudo-model entry (which the solver's own `requested_model == "auto"` branch depends on)
   is still injected, and — combined with bug #2/#3 — doesn't establish that every model the endpoint
   *advertises* is actually invocable through `/v1/chat/completions`. Advertising `claude-sonnet-5` in
   `/v1/models` while `/v1/chat/completions` 500s on it is worse than the current static list, which is
   at least internally consistent.

---

## 5. Concrete Plan B Refinements

The fixes below keep the good ideas (real dynamic discovery where an API supports it, subscription-
first arbitration, KV-cache latching) and remove the parts that don't survive contact with the existing
async gateway. Three principles drive every change: (a) discovery of *identity* must be genuinely live
per source, with subjective scoring split into an editable overlay; (b) the catalog schema must carry
enough to actually place a request — no schema field, no execution path; (c) every provider, metered or
subscription, dynamically discovered or not, goes through one circuit breaker registry that is extended
dynamically, never assumed to pre-exist.

### `catalog.py` (excerpt — the parts that change)

```python
import yaml  # new dependency, declared

@dataclass
class ReflexModelDefinition:
    id: str
    display_name: str
    provider: str
    access_method: str          # "http_gateway" | "cli_harness"
    billing_type: str
    context_window: int
    max_output_tokens: int
    reasoning_capability: float
    architecture_score: float
    coding_score: float
    speed_score: float
    tool_calling: bool
    input_cost_per_m: float
    output_cost_per_m: float
    last_updated: float
    base_url: Optional[str] = None       # required for http_gateway execution
    api_key_env: Optional[str] = None    # which env/hermes var holds the credential
    harness_binary: Optional[str] = None # required for cli_harness execution (federation.py dispatch)

_SCORING_OVERLAY: List[Dict[str, Any]] = []
_SCORING_DEFAULT: Dict[str, float] = {
    "reasoning_capability": 0.5, "architecture_score": 0.5,
    "coding_score": 0.6, "speed_score": 0.6,
}

def _load_scoring_overlay() -> None:
    global _SCORING_OVERLAY, _SCORING_DEFAULT
    if not CONFIG_PROVIDERS_FILE.exists():
        return
    data = yaml.safe_load(CONFIG_PROVIDERS_FILE.read_text()) or {}
    _SCORING_OVERLAY = data.get("scoring_overrides", [])
    _SCORING_DEFAULT.update(data.get("default", {}))

def score_model(model_id: str) -> Dict[str, float]:
    """The ONLY place capability opinions live — identity stays dynamic, scores are config."""
    for rule in _SCORING_OVERLAY:
        if re.search(rule["match"], model_id):
            return {**_SCORING_DEFAULT, **rule}
    return dict(_SCORING_DEFAULT)

class ModelCatalog:
    def __init__(self, db_path: Path = CATALOG_DB):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        _load_scoring_overlay()
        self._init_db()
        self._memory_cache: Dict[str, ReflexModelDefinition] = {}
        self._per_provider_refresh: Dict[str, float] = {}
        self._load_cache()
        self._refresh_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS models (
                id TEXT, provider TEXT, display_name TEXT, access_method TEXT,
                billing_type TEXT, context_window INTEGER, max_output_tokens INTEGER,
                reasoning_capability REAL, architecture_score REAL, coding_score REAL,
                speed_score REAL, tool_calling INTEGER, input_cost_per_m REAL,
                output_cost_per_m REAL, last_updated REAL, base_url TEXT,
                api_key_env TEXT, harness_binary TEXT,
                PRIMARY KEY (id, provider)
            )""")

    def upsert_models(self, models: List[ReflexModelDefinition]):
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO models VALUES (:id,:provider,:display_name,:access_method,"
                ":billing_type,:context_window,:max_output_tokens,:reasoning_capability,"
                ":architecture_score,:coding_score,:speed_score,:tool_calling,:input_cost_per_m,"
                ":output_cost_per_m,:last_updated,:base_url,:api_key_env,:harness_binary)",
                [{**asdict(m), "tool_calling": int(m.tool_calling)} for m in models],
            )
        new_cache = dict(self._memory_cache)  # atomic swap, not in-place mutation
        now = time.time()
        for m in models:
            new_cache[f"{m.provider}::{m.id}"] = m
            self._per_provider_refresh[m.provider] = now
        self._memory_cache = new_cache
        for m in models:
            ensure_breaker(m.provider)  # every discovered provider gets circuit protection

    def refresh_from_providers(self, force: bool = False):
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            now = time.time()
            discovered: List[ReflexModelDefinition] = []
            for source, ttl in (
                (self._discover_opencode_go, 3600), (self._discover_cli_harnesses, 3600),
                (self._discover_gemini_models, 3600), (self._discover_openrouter_models, 3600),
            ):
                key = source.__name__
                last = self._per_provider_refresh.get(key, 0)
                if force or now - last >= ttl:
                    discovered.extend(source(now))
            if discovered:
                self.upsert_models(discovered)
        finally:
            self._refresh_lock.release()

    def _discover_opencode_go(self, timestamp: float) -> List[ReflexModelDefinition]:
        """Live GET /models against the opencode-go gateway — no hardcoded model tuples."""
        key = get_opencode_go_key()
        if not key:
            return []
        try:
            req = urllib.request.Request(
                "https://opencode.ai/zen/go/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            return []
        results = []
        for item in data.get("data", []):
            mid = item["id"]
            scores = score_model(mid)
            results.append(ReflexModelDefinition(
                id=mid, display_name=item.get("name", mid), provider="opencode-go",
                access_method="http_gateway", billing_type="subscription",
                context_window=int(item.get("context_length", 128_000)),
                max_output_tokens=int(item.get("max_output_tokens", 16_384)),
                tool_calling=bool(item.get("supports_tools", True)),
                input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=timestamp,
                base_url="https://opencode.ai/zen/go/v1", api_key_env="OPENCODE_GO_API_KEY",
                **scores,
            ))
        return results

    def _discover_cli_harnesses(self, timestamp: float) -> List[ReflexModelDefinition]:
        """Identity still comes from local harness config, not code — see providers.yaml note."""
        results = []
        if shutil.which("claude") or (Path.home() / ".local/bin/claude").exists():
            for mid in _read_harness_model_ids("claude"):  # reads harness's own config, not literals
                scores = score_model(mid)
                results.append(ReflexModelDefinition(
                    id=mid, display_name=mid, provider="claude-cli",
                    access_method="cli_harness", billing_type="subscription",
                    context_window=200_000, max_output_tokens=16_384, tool_calling=True,
                    input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=timestamp,
                    harness_binary="claude", **scores,
                ))
        return results
```

### `circuit_breaker.py` (one addition)

```python
def is_healthy(self) -> bool:
    """Pure read, no mutation — safe to call for ranking/filtering."""
    return self.state != BreakerState.OPEN
```

### `server.py` (breaker registry becomes dynamic; execution path resolves from catalog)

```python
BREAKER_REGISTRY: Dict[str, ProviderCircuitBreaker] = {}

def ensure_breaker(provider: str) -> ProviderCircuitBreaker:
    if provider not in BREAKER_REGISTRY:
        BREAKER_REGISTRY[provider] = ProviderCircuitBreaker(
            name=provider, **CONFIG["circuit_breaker"]
        )
    return BREAKER_REGISTRY[provider]

def resolve_connection(route: Dict[str, Any]) -> Tuple[str, str]:
    """Replaces CONFIG['providers'][name]['base_url'] lookups with a catalog-backed one."""
    m = catalog.get(route["provider"], route["model"])
    if m.access_method != "http_gateway" or not m.base_url:
        raise HTTPException(status_code=502, detail=f"{route['model']} has no HTTP execution path")
    api_key = get_key(m.api_key_env) if m.api_key_env else ""
    return f"{m.base_url}/chat/completions", api_key

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    session_id = get_session_id(payload, request.headers)
    sub_route, metered_route, tier_num, explanation = select_candidate_routes(payload, session_id)

    # cli_harness models never enter the HTTP proxy path — dispatch via federation instead
    primary_model = catalog.get(sub_route["provider"], sub_route["model"])
    if primary_model.access_method == "cli_harness":
        result = await delegate_subagent(
            task=payload["messages"][-1]["content"],
            preferred_model=primary_model.id, preferred_harness=primary_model.harness_binary,
            timeout_sec=120.0, delegation_depth=0, delegation_chain="", delegation_id=f"chat-{session_id}",
        )
        return JSONResponse(content=result)
    # ... existing preflight_and_stream/non-streaming path, using resolve_connection() instead of
    #     CONFIG['providers'][prov_name]['base_url']

# /health and /v1/route must not reference the deleted CONFIG["model_tiers"]:
@app.get("/health")
async def health_check():
    providers_summary = {
        name: {"circuit_breaker": b.get_status()} for name, b in BREAKER_REGISTRY.items()
    }
    return {"status": "active", "providers": providers_summary, "catalog_size": len(catalog.list_all())}

@app.post("/v1/route")
async def route_preview(request: Request):
    data = await request.json()
    messages = data.get("messages", [{"role": "user", "content": str(data.get("prompt", ""))}])
    vector = solver.extract_vector(messages, data.get("model", "auto"))
    primary, metered, reason = solver.arbitrate(vector, session_id="preview", requested_model="auto")
    return {"status": "success", "reason": reason, "subscription_route": primary, "metered_route": metered}
```

### `solver.py` (breaker filtering applies to every provider; explicit-model miss is an error)

```python
def arbitrate(self, vector, session_id, requested_model="auto"):
    models = self.catalog.list_all()

    if requested_model and requested_model != "auto":
        matches = [m for m in models if requested_model == m.id]  # exact match only, no substring
        if not matches:
            raise ValueError(f"Requested model '{requested_model}' not found in catalog")
        m = matches[0]
        route = {"provider": m.provider, "model": m.id, "access_method": m.access_method}
        return route, route, f"Explicit model override: {m.id}"

    ...
    for m in models:
        breaker = BREAKER_REGISTRY.get(m.provider) or ensure_breaker(m.provider)
        is_healthy = breaker.is_healthy()  # applies uniformly — subscription AND metered
        ...
    # emergency fallback must still respect health, or it defeats the breaker entirely
    emergency = [m for m in models if m.context_window >= vector.token_count
                 and (BREAKER_REGISTRY.get(m.provider) or ensure_breaker(m.provider)).is_healthy()]
    if not emergency:
        emergency = models  # truly nothing is healthy — last resort, unchanged
```

### Net effect

- Discovery is honest: OpenRouter, OpenCode-Go, and Gemini are queried live; CLI harnesses fall back to
  reading the harness's own local config rather than inventing IDs in Python, and *all* subjective
  scoring moves to `providers.yaml`, which is now actually read.
- The catalog schema carries `base_url`/`api_key_env`/`harness_binary`, so every route the solver
  returns is provably executable, and `cli_harness` models are routed through `federation.py` instead
  of silently `KeyError`-ing in the HTTP proxy path.
- `BREAKER_REGISTRY` is extended dynamically for every discovered provider (metered included), and
  `is_healthy()` is a real, non-mutating method — no `AttributeError`, no canary-starvation via ranking.
- `/health` and `/v1/route` are rewritten in the same change that deletes `model_tiers`, so nothing
  404s/500s post-merge.
- SQLite runs in WAL mode with atomic in-memory cache swaps, eliminating both the lock contention and
  the torn-read hazard between the background refresh thread and request-serving reads.
