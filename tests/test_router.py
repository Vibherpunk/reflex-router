"""
Unit and integration test suite for Reflex Multi-Provider System 1 Router.
"""
import pytest
from fastapi.testclient import TestClient

from config import CONFIG
from classifier import classify_request, detect_tool_errors
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
    assert "metrics" in data
    assert "recent_incidents" in data
    assert "circuit_breakers" in data
    assert "opencode-go" in data["circuit_breakers"]
