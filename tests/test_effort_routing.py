"""
Unit and integration tests for Reflex Dynamic Reasoning Effort Routing.
Tests:
- Declarative scoring_spec effort mapping and thinking budgets
- CapabilityRequestVector effort assignment by tier
- User prompt overrides (#think, #reason, #spec, #architect, --deep)
- Upstream provider header and payload injection (Gemini, Anthropic, OpenAI, OpenRouter)
- /v1/route and /v1/chat/completions endpoints telemetry headers
"""
import pytest
from fastapi.testclient import TestClient

import scoring_spec
from catalog import ModelCatalog, ReflexModelDefinition
from solver import ArbitrationSolver, CapabilityRequestVector
from server import app, resolve_connection, select_candidate_routes
from circuit_breaker import BreakerState

client = TestClient(app)


def test_scoring_spec_tier_effort_mapping():
    """Verify tier to reasoning effort mapping in scoring_spec."""
    assert scoring_spec.get_tier_effort(0) == "none"
    assert scoring_spec.get_tier_effort(1) == "low"
    assert scoring_spec.get_tier_effort(2) == "medium"
    assert scoring_spec.get_tier_effort(3) == "high"
    # Fallback for unknown tier
    assert scoring_spec.get_tier_effort(99) == "low"


def test_scoring_spec_effort_thinking_budgets():
    """Verify effort to token budget mapping in scoring_spec."""
    assert scoring_spec.get_effort_budget("none") == 0
    assert scoring_spec.get_effort_budget("low") == 2048
    assert scoring_spec.get_effort_budget("medium") == 8192
    assert scoring_spec.get_effort_budget("high") == 32768
    assert scoring_spec.get_effort_budget("max") == 65536
    # Fallback for unknown effort
    assert scoring_spec.get_effort_budget("unknown") == 2048


def test_tier0_prompt_effort():
    """Tier 0 prompt (routine tool churn / formatting) returns effort: none or low."""
    cat = ModelCatalog()
    solver = ArbitrationSolver(cat)
    messages = [{"role": "user", "content": "Format this list of numbers: 1, 2, 3"}]
    vec = solver.extract_vector(messages)
    assert vec.tier_num == 0
    assert vec.effort in ("none", "low")
    assert vec.effort == "none"


def test_tier1_prompt_effort():
    """Tier 1 prompt (feature implementation) returns effort: low."""
    cat = ModelCatalog()
    solver = ArbitrationSolver(cat)
    messages = [{"role": "user", "content": "Implement the user profile view component and refactor tests"}]
    vec = solver.extract_vector(messages)
    assert vec.tier_num == 1
    assert vec.effort == "low"


def test_tier2_prompt_effort():
    """Tier 2 prompt (concurrency / race condition) returns effort: medium."""
    cat = ModelCatalog()
    solver = ArbitrationSolver(cat)
    messages = [{"role": "user", "content": "Find the race condition and deadlock in this channel worker"}]
    vec = solver.extract_vector(messages)
    assert vec.tier_num == 2
    assert vec.effort == "medium"


def test_tier3_prompt_effort():
    """Tier 3 prompt (distributed consensus / formal verification) returns effort: high."""
    cat = ModelCatalog()
    solver = ArbitrationSolver(cat)
    messages = [{"role": "user", "content": "Draft the RFC spec for distributed consensus with zero-downtime"}]
    vec = solver.extract_vector(messages)
    assert vec.tier_num == 3
    assert vec.effort == "high"

    # Formal verification / cryptographic audit
    messages_crypto = [{"role": "user", "content": "Perform cryptographic audit and formal verification of the enclave"}]
    vec_crypto = solver.extract_vector(messages_crypto)
    assert vec_crypto.tier_num == 3
    assert vec_crypto.effort == "high"


def test_prompt_effort_explicit_tags():
    """Tags #think, #reason, #spec, #architect, --deep dynamically escalate effort."""
    cat = ModelCatalog()
    solver = ArbitrationSolver(cat)

    # #think on Tier 0 promotes effort to medium
    vec_think = solver.extract_vector([{"role": "user", "content": "Format this list: 1, 2, 3 #think"}])
    assert vec_think.effort == "medium"

    # #reason on Tier 1 promotes effort to medium
    vec_reason = solver.extract_vector([{"role": "user", "content": "Implement auth middleware #reason"}])
    assert vec_reason.effort == "medium"

    # --deep on Tier 0 promotes effort to high
    vec_deep = solver.extract_vector([{"role": "user", "content": "Format this list: 1, 2, 3 --deep"}])
    assert vec_deep.effort == "high"

    # #spec on Tier 1 promotes effort to high
    vec_spec = solver.extract_vector([{"role": "user", "content": "Implement endpoint #spec"}])
    assert vec_spec.effort == "high"

    # #architect on Tier 1 promotes effort to high
    vec_arch = solver.extract_vector([{"role": "user", "content": "Implement endpoint #architect"}])
    assert vec_arch.effort == "high"


def test_arbitration_attaches_effort_and_explanation():
    """ArbitrationSolver.arbitrate attaches effort to routes and includes Effort= in explanation."""
    cat = ModelCatalog()
    solver = ArbitrationSolver(cat)

    vec = CapabilityRequestVector(
        token_count=100,
        reasoning_depth=0.90,
        tool_calling=False,
        architecture_score=0.75,
        modality="text",
        tier_num=2,
        explanation="Tier 2 task",
        effort="medium"
    )

    primary, metered, explanation = solver.arbitrate(vec, session_id="test-effort-sess")
    assert primary["effort"] == "medium"
    assert metered["effort"] == "medium"
    assert "Effort=medium" in explanation


def test_v1_route_endpoint_returns_effort_and_budget():
    """The /v1/route endpoint must return effort and reasoning_budget in the JSON response."""
    # Tier 0
    resp0 = client.post("/v1/route", json={"prompt": "Format this list of numbers: 1, 2, 3"})
    assert resp0.status_code == 200
    data0 = resp0.json()
    assert data0["tier"] == 0
    assert data0["effort"] in ("none", "low")
    assert data0["reasoning_budget"] == 0
    assert data0["subscription_route"]["effort"] == data0["effort"]
    assert data0["metered_route"]["effort"] == data0["effort"]

    # Tier 1
    resp1 = client.post("/v1/route", json={"prompt": "Implement the user profile view component and refactor tests"})
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["tier"] == 1
    assert data1["effort"] == "low"
    assert data1["reasoning_budget"] == 2048

    # Tier 2
    resp2 = client.post("/v1/route", json={"prompt": "Find the race condition and deadlock in this channel worker"})
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["tier"] == 2
    assert data2["effort"] == "medium"
    assert data2["reasoning_budget"] == 8192

    # Tier 3
    resp3 = client.post("/v1/route", json={"prompt": "Draft the RFC spec for distributed consensus with zero-downtime"})
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["tier"] == 3
    assert data3["effort"] == "high"
    assert data3["reasoning_budget"] == 32768


def test_resolve_connection_gemini_headers():
    """Gemini and Antigravity bridges receive x-gemini-reasoning-effort header."""
    route_off = {"provider": "gemini", "model": "gemini-2.5-flash", "effort": "none"}
    payload_off = {"model": "gemini-2.5-flash", "messages": []}
    _, headers_off = resolve_connection(route_off, payload_off, "s1")
    assert headers_off["x-gemini-reasoning-effort"] == "off"

    route_low = {"provider": "gemini", "model": "gemini-2.5-flash", "effort": "low"}
    payload_low = {"model": "gemini-2.5-flash", "messages": []}
    _, headers_low = resolve_connection(route_low, payload_low, "s2")
    assert headers_low["x-gemini-reasoning-effort"] == "low"

    route_high = {"provider": "gemini", "model": "gemini-2.5-pro", "effort": "high"}
    payload_high = {"model": "gemini-2.5-pro", "messages": []}
    _, headers_high = resolve_connection(route_high, payload_high, "s3")
    assert headers_high["x-gemini-reasoning-effort"] == "high"


def test_resolve_connection_anthropic_payload():
    """Anthropic Claude routes receive thinking budget in payload."""
    # Medium effort: 8192 tokens
    route_med = {"provider": "anthropic", "model": "claude-3-7-sonnet", "effort": "medium"}
    payload_med = {"model": "claude-3-7-sonnet", "messages": []}
    # Catalog or mock provider setup for testing
    from config import CONFIG
    CONFIG["providers"]["anthropic"] = {
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "test-key"
    }
    _, _ = resolve_connection(route_med, payload_med, "s1")
    assert payload_med.get("thinking") == {"type": "enabled", "budget_tokens": 8192}

    # None effort: thinking omitted
    route_none = {"provider": "anthropic", "model": "claude-3-7-sonnet", "effort": "none"}
    payload_none = {"model": "claude-3-7-sonnet", "messages": [], "thinking": {"type": "enabled"}}
    _, _ = resolve_connection(route_none, payload_none, "s2")
    assert "thinking" not in payload_none


def test_resolve_connection_openai_and_openrouter_payload():
    """OpenAI o-series and OpenRouter routes receive reasoning_effort in payload."""
    from config import CONFIG
    CONFIG["providers"]["openai"] = {
        "base_url": "https://api.openai.com/v1",
        "api_key": "test-key"
    }

    # OpenAI o3-mini with medium effort
    route_o3 = {"provider": "openai", "model": "o3-mini", "effort": "medium"}
    payload_o3 = {"model": "o3-mini", "messages": []}
    _, _ = resolve_connection(route_o3, payload_o3, "s1")
    assert payload_o3.get("reasoning_effort") == "medium"

    # OpenAI o1 with effort: none -> reasoning_effort removed
    route_o1_none = {"provider": "openai", "model": "o1", "effort": "none"}
    payload_o1_none = {"model": "o1", "messages": [], "reasoning_effort": "high"}
    _, _ = resolve_connection(route_o1_none, payload_o1_none, "s2")
    assert "reasoning_effort" not in payload_o1_none

    # OpenRouter metered route
    route_or = {"provider": "openrouter", "model": "deepseek/deepseek-r1", "effort": "high"}
    payload_or = {"model": "deepseek/deepseek-r1", "messages": []}
    _, _ = resolve_connection(route_or, payload_or, "s3")
    assert payload_or.get("reasoning_effort") == "high"


def test_chat_completions_response_headers(monkeypatch):
    """chat_completions responses include x-reflex-effort and x-reflex-tier headers."""
    import server

    class FakeResponse:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content
            self.headers = {}

    class FakeClient:
        async def post(self, *args, **kwargs):
            return FakeResponse(200, b'{"id":"chatcmpl-test","choices":[{"message":{"content":"ok"}}]}')

    monkeypatch.setattr(server.gateway_pool, "client", FakeClient())

    resp = client.post("/v1/chat/completions", json={
        "model": "auto",
        "stream": False,
        "session_id": "test-headers-sess",
        "messages": [{"role": "user", "content": "Find the race condition and deadlock in this channel worker"}]
    })

    assert resp.status_code == 200
    assert resp.headers.get("x-reflex-effort") == "medium"
    assert resp.headers.get("x-reflex-tier") == "2"
