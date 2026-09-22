"""
Unit and integration test suite for OpenCode-Go System 1 Router.
"""
import pytest
from fastapi.testclient import TestClient

from config import CONFIG
from classifier import classify_request, detect_tool_errors
from server import app, estimate_tokens, get_session_id, select_model_and_effort

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["auth_configured"] is True
    assert "tiers" in data

def test_models_endpoint():
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    model_ids = [m["id"] for m in data["data"]]
    assert "auto" in model_ids
    assert "deepseek-v4-flash" in model_ids
    assert "deepseek-v4-pro" in model_ids

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
    """Short prompt should escalate to Tier 2 if prior turn has test failure."""
    messages = [
        {"role": "user", "content": "Run tests"},
        {"role": "tool", "content": "FAIL: test_worker.py: AssertionError: Expected 200 but got 500. Exit code 1"},
        {"role": "user", "content": "Fix it."}  # 2 words!
    ]
    assert detect_tool_errors(messages) is True
    tier, reason = classify_request(messages)
    assert tier == 2
    assert "Escalated" in reason

def test_session_id_deterministic():
    payload = {"messages": [{"role": "user", "content": "Hello session"}]}
    sess1 = get_session_id(payload, {})
    sess2 = get_session_id(payload, {})
    assert sess1 == sess2
    assert sess1.startswith("sess-")

def test_kv_cache_latch():
    session_id = "test-kv-session-001"
    
    # 1. First prompt establishes model
    small_payload = {"messages": [{"role": "user", "content": "Refactor this component"}]}
    model1, _, _ = select_model_and_effort(small_payload, session_id)
    assert model1 == "qwen3.7-plus"

    # 2. Huge prompt (>20,000 tokens) should lock to the established model
    huge_text = "word " * 25000
    huge_payload = {"messages": [{"role": "user", "content": huge_text}]}
    model2, _, reason2 = select_model_and_effort(huge_payload, session_id)
    assert model2 == model1
    assert "KV-Cache Latch locked" in reason2

def test_feedback_and_memory_escalation():
    # Submit feedback about a failed prompt that was misclassified as Tier 0
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

    # Querying a very similar prompt should now auto-escalate via memory
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

