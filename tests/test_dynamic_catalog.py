"""
Tests for Dynamic Model Catalog, Capability Scoring Overlay, and Arbitration Solver.
"""
import pytest
from pathlib import Path
from catalog import ModelCatalog, ReflexModelDefinition, score_model
from solver import ArbitrationSolver, CapabilityRequestVector
from circuit_breaker import ensure_breaker, BreakerState

def test_score_model_overlay():
    # Test opus pattern
    s_opus = score_model("claude-opus-5")
    assert s_opus["architecture_score"] >= 0.95
    assert s_opus["reasoning_capability"] >= 0.95

    # Test sonnet pattern
    s_sonnet = score_model("claude-sonnet-5")
    assert s_sonnet["architecture_score"] >= 0.90
    assert s_sonnet["coding_score"] >= 0.90

    # Test flash/mini pattern
    s_flash = score_model("deepseek-v4-flash")
    assert s_flash["speed_score"] >= 0.90
    assert s_flash["reasoning_capability"] <= 0.30

    # Test gemini pro pattern
    s_gemini = score_model("gemini-2.5-pro")
    assert s_gemini["reasoning_capability"] >= 0.85

def test_catalog_sqlite_and_cache(tmp_path):
    db_path = tmp_path / "test_catalog.db"
    cat = ModelCatalog(db_path=db_path)
    assert len(cat.list_all()) == 0

    m1 = ReflexModelDefinition(
        id="test-sub-model",
        display_name="Test Sub",
        provider="test-sub-prov",
        access_method="http_gateway",
        billing_type="subscription",
        context_window=128_000,
        max_output_tokens=16_384,
        reasoning_capability=0.8,
        architecture_score=0.8,
        coding_score=0.8,
        speed_score=0.8,
        tool_calling=True,
        input_cost_per_m=0.0,
        output_cost_per_m=0.0,
        last_updated=1000.0,
        base_url="http://test.local",
        api_key_env="TEST_KEY"
    )
    m2 = ReflexModelDefinition(
        id="test-metered-model",
        display_name="Test Metered",
        provider="test-metered-prov",
        access_method="http_gateway",
        billing_type="metered",
        context_window=128_000,
        max_output_tokens=16_384,
        reasoning_capability=0.9,
        architecture_score=0.9,
        coding_score=0.9,
        speed_score=0.7,
        tool_calling=True,
        input_cost_per_m=2.0,
        output_cost_per_m=8.0,
        last_updated=1000.0,
        base_url="http://test.local",
        api_key_env="TEST_KEY"
    )

    cat.upsert_models([m1, m2])
    assert len(cat.list_all()) == 2
    assert cat.get("test-sub-prov", "test-sub-model") is not None
    assert cat.get_by_id("test-metered-model") is not None

    # Verify reloading from disk
    cat2 = ModelCatalog(db_path=db_path)
    assert len(cat2.list_all()) == 2

def test_solver_subscription_priority_and_failover(tmp_path):
    db_path = tmp_path / "test_catalog.db"
    cat = ModelCatalog(db_path=db_path)

    sub_model = ReflexModelDefinition(
        id="sub-fast",
        display_name="Sub Fast",
        provider="sub-provider",
        access_method="http_gateway",
        billing_type="subscription",
        context_window=128_000,
        max_output_tokens=8_192,
        reasoning_capability=0.6,
        architecture_score=0.6,
        coding_score=0.8,
        speed_score=0.9,
        tool_calling=True,
        input_cost_per_m=0.0,
        output_cost_per_m=0.0,
        last_updated=1000.0,
        base_url="http://sub.local"
    )
    metered_model = ReflexModelDefinition(
        id="metered-frontier",
        display_name="Metered Frontier",
        provider="metered-provider",
        access_method="http_gateway",
        billing_type="metered",
        context_window=200_000,
        max_output_tokens=16_384,
        reasoning_capability=0.95,
        architecture_score=0.95,
        coding_score=0.95,
        speed_score=0.6,
        tool_calling=True,
        input_cost_per_m=3.0,
        output_cost_per_m=15.0,
        last_updated=1000.0,
        base_url="http://metered.local"
    )
    cat.upsert_models([sub_model, metered_model])
    solver = ArbitrationSolver(cat)

    # 1. Normal priority: Sub model chosen as primary
    vec = CapabilityRequestVector(
        token_count=100,
        reasoning_depth=0.5,
        tool_calling=False,
        architecture_score=0.5,
        modality="text",
        tier_num=1,
        explanation="Test routine"
    )
    pri, met, reason = solver.arbitrate(vec, session_id="test-session-1")
    assert pri["provider"] == "sub-provider"
    assert pri["billing_type"] == "subscription"
    assert met["provider"] == "metered-provider"
    assert met["billing_type"] == "metered"

    # 2. Circuit breaker failover: Sub provider OPEN trips to metered
    breaker_sub = ensure_breaker("sub-provider")
    breaker_sub.state = BreakerState.OPEN
    pri2, met2, _ = solver.arbitrate(vec, session_id="test-session-2")
    assert pri2["provider"] == "metered-provider"
    assert pri2["billing_type"] == "metered"
    breaker_sub.state = BreakerState.CLOSED

def test_solver_kv_cache_latching(tmp_path):
    db_path = tmp_path / "test_catalog.db"
    cat = ModelCatalog(db_path=db_path)
    cat.upsert_models([
        ReflexModelDefinition(
            id="model-a", display_name="A", provider="prov-a",
            access_method="http_gateway", billing_type="subscription",
            context_window=200_000, max_output_tokens=8_192,
            reasoning_capability=0.7, architecture_score=0.7, coding_score=0.8, speed_score=0.8,
            tool_calling=True, input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=1000.0,
            base_url="http://a.local"
        ),
        ReflexModelDefinition(
            id="model-b", display_name="B", provider="prov-b",
            access_method="http_gateway", billing_type="metered",
            context_window=200_000, max_output_tokens=8_192,
            reasoning_capability=0.9, architecture_score=0.9, coding_score=0.9, speed_score=0.6,
            tool_calling=True, input_cost_per_m=1.0, output_cost_per_m=2.0, last_updated=1000.0,
            base_url="http://b.local"
        )
    ])
    solver = ArbitrationSolver(cat)

    sess = "sess-latch-test"
    v_small = CapabilityRequestVector(token_count=1000, reasoning_depth=0.5, tool_calling=False, architecture_score=0.5, modality="text", tier_num=1, explanation="small")
    pri1, _, _ = solver.arbitrate(v_small, session_id=sess)
    assert pri1["model"] == "model-a"

    # Now simulate >20,000 tokens in same session
    v_huge = CapabilityRequestVector(token_count=25000, reasoning_depth=0.9, tool_calling=False, architecture_score=0.9, modality="text", tier_num=3, explanation="huge")
    pri2, _, reason2 = solver.arbitrate(v_huge, session_id=sess)
    assert pri2["model"] == "model-a"
    assert "KV-Cache Latch locked" in reason2

def test_solver_explicit_model_validation(tmp_path):
    db_path = tmp_path / "test_catalog.db"
    cat = ModelCatalog(db_path=db_path)
    cat.upsert_models([
        ReflexModelDefinition(
            id="valid-model", display_name="Valid", provider="p",
            access_method="http_gateway", billing_type="subscription",
            context_window=100_000, max_output_tokens=4_096,
            reasoning_capability=0.5, architecture_score=0.5, coding_score=0.5, speed_score=0.5,
            tool_calling=True, input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=1000.0
        )
    ])
    solver = ArbitrationSolver(cat)

    vec = CapabilityRequestVector(token_count=10, reasoning_depth=0.1, tool_calling=False, architecture_score=0.1, modality="text", tier_num=0, explanation="test")

    # Valid model succeeds
    pri, _, _ = solver.arbitrate(vec, session_id="s", requested_model="valid-model")
    assert pri["model"] == "valid-model"

    # Nonexistent model raises ValueError
    with pytest.raises(ValueError, match="not found in active model catalog"):
        solver.arbitrate(vec, session_id="s", requested_model="nonexistent-model-xyz")
