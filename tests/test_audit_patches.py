"""
Verification test suite for Adversarial Audit Patches (P0-1, P0-2, P0-4, P1-1, P1-2, P1-3).
"""
import pytest
from fastapi.testclient import TestClient
import httpx

from server import app, gateway_pool
from circuit_breaker import ProviderCircuitBreaker, BreakerState, ensure_breaker, BREAKER_REGISTRY
from classifier import classify_request, classify_full_request, PROTECTED_DOMAINS
from solver import ArbitrationSolver, CapabilityRequestVector
from catalog import ModelCatalog, ReflexModelDefinition, resolve_model_alias

client = TestClient(app)


# ---------------------------------------------------------------------------
# P0-1: Domain Hard Floor & Tier 3 High-Consequence Keywords
# ---------------------------------------------------------------------------

def test_p0_1_tier3_high_consequence_keywords():
    """All high-consequence keywords must classify into Tier 3."""
    keywords = [
        "indemnification", "statutory", "reconcile", "ledger",
        "blast radius", "audit", "delaware", "oar 414", "erdc", "subpoena"
    ]
    for kw in keywords:
        prompt = f"Please process the {kw} requirements for this task."
        messages = [{"role": "user", "content": prompt}]
        tier, reason = classify_request(messages)
        assert tier == 3, f"Keyword '{kw}' failed to classify as Tier 3: got Tier {tier} ({reason})"


def test_p0_1_domain_hard_floor_tier2():
    """Protected domains must have an immutable hard floor at Tier 2 (forbidden from Tier 0 Flash)."""
    prompts = {
        "legal": "Review this basic nondisclosure terms and conditions overview",
        "finance": "Check the quarterly financial cash flow calculation methodology",
        "accounting": "Prepare the monthly bookkeeping entries for accounts payable",
        "compliance": "Check our operational procedures against the compliance requirements",
        "security": "Review these infosec procedures and threat model documentation"
    }
    for domain, prompt in prompts.items():
        messages = [{"role": "user", "content": prompt}]
        tier, reason, detected_domain = classify_full_request(messages)
        assert detected_domain == domain or detected_domain in PROTECTED_DOMAINS, f"Domain mismatch for {domain}: got {detected_domain}"
        assert tier >= 2, f"Domain '{domain}' breached hard floor: got Tier {tier} ({reason})"


# ---------------------------------------------------------------------------
# P0-2: Unenforced OPEN Breaker State & Self-Failover Loop (503 Load Shedding)
# ---------------------------------------------------------------------------

def test_p0_2_circuit_open_returns_503_load_shedding(monkeypatch):
    """When all candidate providers are circuit-open, return HTTP 503 with Retry-After: 30."""
    sub_breaker = ensure_breaker("gemini")
    openrouter_breaker = ensure_breaker("openrouter")
    claude_breaker = ensure_breaker("claude-cli")
    opencode_breaker = ensure_breaker("opencode-go")

    # Force all providers into OPEN state
    for b in [sub_breaker, openrouter_breaker, claude_breaker, opencode_breaker]:
        b.state = BreakerState.OPEN
        b.cooldown_until = 9999999999.0
        b.canary_in_flight = False

    try:
        resp = client.post("/v1/chat/completions", json={
            "model": "auto",
            "messages": [{"role": "user", "content": "Format this list: 1 2 3"}]
        })
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") == "30"
        data = resp.json()
        assert "circuit-open" in data["error"]["message"].lower() or "service_unavailable" in data["error"]["type"].lower()
    finally:
        for b in [sub_breaker, openrouter_breaker, claude_breaker, opencode_breaker]:
            b.state = BreakerState.CLOSED
            b.failure_count = 0
            b.cooldown_until = 0.0


# ---------------------------------------------------------------------------
# P0-4: Client 4xx Errors Never Trip Circuit Breakers
# ---------------------------------------------------------------------------

class FakeClientStatus:
    def __init__(self, status_code, content=b'{"error":{"message":"client fault"}}'):
        self.status_code = status_code
        self.content = content
        self.headers = {}

    async def post(self, *args, **kwargs):
        return self

    async def aclose(self):
        self._closed = True
        return None


def test_p0_4_client_4xx_does_not_trip_circuit_breaker(monkeypatch):
    """Client errors (400, 401, 404, 422) must NEVER trip circuit breakers."""
    sub_breaker = ensure_breaker("opencode-go")
    sub_breaker.state = BreakerState.CLOSED
    sub_breaker.failure_count = 0

    client_error_codes = [400, 401, 404, 422]
    for code in client_error_codes:
        monkeypatch.setattr(gateway_pool, "client", FakeClientStatus(code))
        resp = client.post("/v1/chat/completions", json={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "malformed test request"}]
        })
        assert resp.status_code == code
        # Breaker must remain CLOSED and failure_count must be 0!
        assert sub_breaker.state == BreakerState.CLOSED, f"Status code {code} erroneously tripped breaker to {sub_breaker.state}"
        assert sub_breaker.failure_count == 0, f"Status code {code} increased failure_count to {sub_breaker.failure_count}"


# ---------------------------------------------------------------------------
# P1-1: Stateless Preview KV-Cache Latch
# ---------------------------------------------------------------------------

def test_p1_1_preview_endpoint_does_not_pollute_session_affinity():
    """Route preview endpoint must not latch session affinity."""
    # Call /v1/route with short prompt
    resp1 = client.post("/v1/route", json={"prompt": "Short status check"})
    assert resp1.status_code == 200

    # Call /v1/route with large prompt (>20,000 tokens)
    huge_prompt = "Refactor distributed consensus protocol architecture spec. " + "x " * 25000
    resp2 = client.post("/v1/route", json={"prompt": huge_prompt})
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["tier"] == 3
    # Must NOT say "KV-Cache Latch locked to gemini-3.8-flash"
    assert "KV-Cache Latch locked to gemini" not in data2.get("reason", "")


# ---------------------------------------------------------------------------
# P1-2: Decorative Capability Thresholds Made Hard Eligibility Gates
# ---------------------------------------------------------------------------

def test_p1_2_feasibility_filter_excludes_incapable_models(tmp_path):
    """Models below vector capability thresholds must be excluded before ranking."""
    db_path = tmp_path / "test_eligibility.db"
    cat = ModelCatalog(db_path=db_path)
    cat.upsert_models([
        ReflexModelDefinition(
            id="low-model", display_name="Low", provider="low-prov",
            access_method="http_gateway", billing_type="subscription",
            context_window=200_000, max_output_tokens=4_096,
            reasoning_capability=0.30, architecture_score=0.30, coding_score=0.99, speed_score=0.99,
            tool_calling=True, input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=1000.0
        ),
        ReflexModelDefinition(
            id="high-model", display_name="High", provider="high-prov",
            access_method="http_gateway", billing_type="subscription",
            context_window=200_000, max_output_tokens=4_096,
            reasoning_capability=0.90, architecture_score=0.85, coding_score=0.80, speed_score=0.50,
            tool_calling=True, input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=1000.0
        )
    ])
    solver = ArbitrationSolver(cat)

    # Vector requires reasoning_depth=0.90, architecture_score=0.75 (Tier 2)
    vec = CapabilityRequestVector(
        token_count=100, reasoning_depth=0.90, tool_calling=False,
        architecture_score=0.75, modality="text", tier_num=2, explanation="Tier 2 task"
    )
    pri, _, _ = solver.arbitrate(vec, session_id="test-gate-sess")
    # Low-model has reasoning=0.30 < 0.90, so it MUST be excluded even though coding/speed are 0.99!
    assert pri["model"] == "high-model"


# ---------------------------------------------------------------------------
# P1-3: Model Alias Normalization
# ---------------------------------------------------------------------------

def test_p1_3_model_alias_normalization():
    """Harness model aliases must normalize to active canonical catalog models."""
    assert resolve_model_alias("claude-3.7-sonnet") == "claude-sonnet-5"
    assert resolve_model_alias("claude-3-7-sonnet") == "claude-sonnet-5"
    assert resolve_model_alias("anthropic/claude-3.7-sonnet") == "claude-sonnet-5"
    assert resolve_model_alias("gemini-2.0-flash") == "gemini-2.5-flash"
    assert resolve_model_alias("gemini-2-0-flash") == "gemini-2.5-flash"
    assert resolve_model_alias("google/gemini-2.0-flash") == "gemini-2.5-flash"
