"""
Unit and integration test suite for Reflex Multi-Provider System 1 Router.
"""
import pytest
from fastapi.testclient import TestClient

from config import CONFIG
from classifier import classify_request, detect_tool_errors
from circuit_breaker import BreakerState
from server import app, estimate_tokens, get_session_id, select_candidate_routes, normalize_error

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert "providers" in data
    assert "opencode-go" in data["providers"]
    assert "openrouter" in data["providers"]
    assert "gemini" in data["providers"]
    assert "tiers" in data

def test_models_endpoint():
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    model_ids = [m["id"] for m in data["data"]]
    assert "auto" in model_ids
    assert "deepseek-v4-flash" in model_ids
    assert "anthropic/claude-3.7-sonnet" in model_ids
    assert "gemini-2.0-flash" in model_ids

def test_classifier_tier0_routine():
    messages = [{"role": "user", "content": "Format this list of numbers: 1, 2, 3"}]
    tier, reason = classify_request(messages)
    assert tier == 0
    assert "Tier 0" in reason

def test_classifier_tier1_feature():
    messages = [{"role": "user", "content": "Implement the user profile view component and refactor tests"}]
    tier, reason = classify_request(messages)
    assert tier == 1
    assert "Tier 1" in reason

def test_classifier_tier2_concurrency():
    messages = [{"role": "user", "content": "Find the race condition and deadlock in this channel worker"}]
    tier, reason = classify_request(messages)
    assert tier == 2
    assert "Tier 2" in reason

def test_classifier_tier3_architecture():
    messages = [{"role": "user", "content": "Draft the RFC spec for distributed consensus with zero-downtime"}]
    tier, reason = classify_request(messages)
    assert tier == 3
    assert "Tier 3" in reason

def test_classifier_explicit_override():
    messages = [{"role": "user", "content": "Fix this 2-line snippet --deep"}]
    tier, reason = classify_request(messages)
    assert tier == 3

def test_trojan_horse_escalation():
    messages = [
        {"role": "user", "content": "Run tests"},
        {"role": "tool", "content": "FAIL: test_worker.py: AssertionError: Expected 200 but got 500. Exit code 1"},
        {"role": "user", "content": "Fix it."}  # 2 words!
    ]
    assert detect_tool_errors(messages) is True
    tier, reason = classify_request(messages)
    assert tier == 2
    assert "Escalated" in reason

def test_capability_matching_metered_override():
    """If user requests a model only on metered, route directly to metered."""
    payload = {
        "model": "anthropic/claude-3.7-sonnet",
        "messages": [{"role": "user", "content": "Hello Claude"}]
    }
    sub_route, metered_route, tier, reason = select_candidate_routes(payload, "sess-test-metered")
    assert sub_route["provider"] == "openrouter"
    assert metered_route["provider"] == "openrouter"
    assert "Capability match" in reason

def test_subscription_priority_default():
    """Default auto tier should prioritize subscription ($0 marginal cost)."""
    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Implement auth middleware"}]
    }
    sub_route, metered_route, tier, reason = select_candidate_routes(payload, "sess-test-sub")
    assert sub_route["provider"] == "opencode-go"
    assert metered_route["provider"] == "openrouter"
    assert "Tier 1" in reason

def test_session_id_deterministic():
    payload = {"messages": [{"role": "user", "content": "Hello session"}]}
    sess1 = get_session_id(payload, {})
    sess2 = get_session_id(payload, {})
    assert sess1 == sess2
    assert sess1.startswith("sess-")

def test_kv_cache_latch():
    session_id = "test-kv-session-002"
    small_payload = {"messages": [{"role": "user", "content": "Refactor this component"}]}
    sub1, _, _, _ = select_candidate_routes(small_payload, session_id)
    assert sub1["model"] == "qwen3.7-plus"

    huge_text = "word " * 25000
    huge_payload = {"messages": [{"role": "user", "content": huge_text}]}
    sub2, _, _, reason2 = select_candidate_routes(huge_payload, session_id)
    assert sub2["model"] == sub1["model"]
    assert "KV-Cache Latch locked" in reason2

def test_normalize_error_html_guard():
    html_body = b"<!DOCTYPE html><html><body>502 Bad Gateway Cloudflare</body></html>"
    err = normalize_error(502, html_body)
    assert "error" in err
    assert err["error"]["code"] == 502
    assert "Upstream error (HTTP 502)" in err["error"]["message"]

def test_feedback_and_memory_escalation():
    feedback_payload = {
        "prompt": "Parse complex AST and lint TCPA regex rules",
        "failed_tier": 0,
        "escalated_tier": 3,
        "error": "Failed to handle AST node nesting"
    }
    resp = client.post("/v1/feedback", json=feedback_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "recorded"
    assert data["incident_id"] > 0

    similar_messages = [{"role": "user", "content": "Parse complex AST and lint TCPA regex"}]
    tier, reason = classify_request(similar_messages)
    assert tier == 3
    assert "Memory Auto-Escalation" in reason

def test_stats_endpoint():
    resp = client.get("/v1/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_incidents" in data
    assert "circuit_breakers" in data
    assert "opencode-go" in data["circuit_breakers"]

def test_harnesses_endpoint():
    resp = client.get("/v1/harnesses")
    assert resp.status_code == 200
    data = resp.json()
    assert "harnesses" in data
    assert "claude" in data["harnesses"]
    assert "agy" in data["harnesses"]
    assert "opencode" in data["harnesses"]

def test_delegate_endpoint_validation():
    # Empty task returns 400
    resp = client.post("/v1/delegate", json={"task": ""})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Deadlock regression tests: every failure path must release the canary slot
# (or trip the breaker). Otherwise HALF_OPEN + canary_in_flight wedges forever
# and the subscription provider is permanently locked out of routing.
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, content=b'{}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

class FakeClient:
    """Minimal httpx.AsyncClient stand-in: returns canned responses in order."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self._closed = False

    async def post(self, *args, **kwargs):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp

    async def aclose(self):
        """Release resources held by the fake client (mirrors httpx semantics)."""
        self._closed = True
        self._responses.clear()
        return None

class _FakeStreamResponse:
    def __init__(self, status_code=200, body=b""):
        self.status_code = status_code
        self.body = body
        self.headers = {}

    async def aread(self):
        return self.body

    async def aiter_bytes(self):
        yield self.body

class _FakeStreamCtx:
    def __init__(self, status_code=200, body=b""):
        self._resp = _FakeStreamResponse(status_code, body)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False

class FakeStreamClient:
    """Minimal stand-in for httpx's client.stream() context manager."""
    def __init__(self, status_code=200, body=b""):
        self._status_code = status_code
        self._body = body

    def stream(self, *args, **kwargs):
        return _FakeStreamCtx(self._status_code, self._body)


def _reset_breaker(breaker):
    breaker.state = BreakerState.CLOSED
    breaker.failure_count = 0
    breaker.cooldown_until = 0.0
    breaker.canary_in_flight = False
    breaker.canary_granted_at = 0.0


def _half_open_no_canary(breaker):
    """Positions the breaker with a fresh canary slot available (HALF_OPEN)."""
    _reset_breaker(breaker)
    breaker.state = BreakerState.HALF_OPEN
    breaker.canary_in_flight = False


def test_non_streaming_sub_500_trips_breaker_and_releases_canary(monkeypatch):
    """Sub returns 500 (not in the failover list): the breaker must trip and
    clear the canary slot rather than wedging in HALF_OPEN."""
    import server
    breaker = server.BREAKER_REGISTRY["opencode-go"]
    _half_open_no_canary(breaker)
    monkeypatch.setattr(
        server.gateway_pool, "client",
        FakeClient([FakeResponse(500, b'{"error":{"message":"boom"}}')])
    )
    try:
        resp = client.post("/v1/chat/completions", json={
            "model": "auto", "stream": False,
            "messages": [{"role": "user", "content": "Format this list: 1 2 3"}]
        })
        assert resp.status_code == 500
        assert breaker.state == BreakerState.OPEN
        assert breaker.canary_in_flight is False
    finally:
        _reset_breaker(breaker)


def test_non_streaming_sub_504_failover_now_includes_504(monkeypatch):
    """Sub 504 must fail over to metered (was missing from the non-streaming
    failover list) and trip the breaker."""
    import server
    breaker = server.BREAKER_REGISTRY["opencode-go"]
    _half_open_no_canary(breaker)
    monkeypatch.setattr(
        server.gateway_pool, "client",
        FakeClient([
            FakeResponse(504, b'{"error":{"message":"upstream"}}'),
            FakeResponse(200, b'{"id":"ok"}'),
        ])
    )
    try:
        resp = client.post("/v1/chat/completions", json={
            "model": "auto", "stream": False,
            "messages": [{"role": "user", "content": "Format this list: 1 2 3"}]
        })
        assert resp.status_code == 200
        assert resp.headers.get("X-Selected-Provider") == "openrouter"
        assert breaker.state == BreakerState.OPEN
        assert breaker.failure_count == 1
    finally:
        _reset_breaker(breaker)


def test_non_streaming_sub_transport_error_trips_breaker(monkeypatch):
    """A transport failure (timeout / connect error) on the sub route must also
    release the canary slot."""
    import httpx
    import server
    breaker = server.BREAKER_REGISTRY["opencode-go"]
    _half_open_no_canary(breaker)

    class BoomClient:
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(server.gateway_pool, "client", BoomClient())
    try:
        resp = client.post("/v1/chat/completions", json={
            "model": "auto", "stream": False,
            "messages": [{"role": "user", "content": "Format this list: 1 2 3"}]
        })
        assert resp.status_code == 502
        assert breaker.state == BreakerState.OPEN
        assert breaker.canary_in_flight is False
    finally:
        _reset_breaker(breaker)


def test_streaming_sub_500_trips_breaker_and_releases_canary(monkeypatch):
    """Streaming path: a >= 400 response from the subscription provider must
    record failure instead of leaving the canary stuck in HALF_OPEN."""
    import server
    breaker = server.BREAKER_REGISTRY["opencode-go"]
    _half_open_no_canary(breaker)
    monkeypatch.setattr(
        server.gateway_pool, "client",
        FakeStreamClient(500, b'{"error":{"message":"boom"}}')
    )
    try:
        resp = client.post("/v1/chat/completions", json={
            "model": "auto", "stream": True,
            "messages": [{"role": "user", "content": "Format this list: 1 2 3"}]
        })
        assert resp.status_code == 500
        assert breaker.state == BreakerState.OPEN
        assert breaker.canary_in_flight is False
    finally:
        _reset_breaker(breaker)
