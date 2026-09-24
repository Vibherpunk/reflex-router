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

    # Test flash/mini pattern - modern v4 flash model has high speed and strong reasoning (not crippled to <=0.30)
    s_flash = score_model("deepseek-v4-flash")
    assert s_flash["speed_score"] >= 0.90
    assert s_flash["reasoning_capability"] >= 0.70

    # Test gemini pro pattern
    s_gemini = score_model("gemini-2.5-pro")
    assert s_gemini["reasoning_capability"] >= 0.85


def test_generational_capability_leaps():
    """Verifies that generational progression mathematically outscores older versions without regex maintenance."""
    s_30 = score_model("claude-3-sonnet")
    s_35 = score_model("claude-3-5-sonnet")
    s_37 = score_model("claude-3-7-sonnet")

    assert s_37["reasoning_capability"] > s_35["reasoning_capability"] > s_30["reasoning_capability"]
    assert s_37["generation"] == 3.7
    assert s_35["generation"] == 3.5
    assert s_30["generation"] == 3.0


def test_opus_generational_progression():
    """Verifies that Opus 5.5 mathematically leads earlier Opus releases and avoids ceiling saturation."""
    s_55 = score_model("anthropic/claude-opus-5.5")
    s_50 = score_model("claude-opus-5")
    s_48 = score_model("claude-opus-4-8")
    s_41 = score_model("claude-opus-4-1")

    assert s_55["generation"] == 5.5
    assert s_50["generation"] == 5.0
    assert s_48["generation"] == 4.8
    assert s_41["generation"] == 4.1

    # Asymptotic progression: 5.5 >= 5.0 > 4.8 > 4.1
    assert s_55["reasoning_capability"] == 0.99
    assert s_55["architecture_score"] == 0.99
    assert s_55["coding_score"] == 0.99

    assert s_55["reasoning_capability"] > s_50["reasoning_capability"] >= s_48["reasoning_capability"] > s_41["reasoning_capability"]
    assert s_55["coding_score"] > s_50["coding_score"] >= s_48["coding_score"] > s_41["coding_score"]


def test_specialization_bonuses():
    """Verifies that thinking and reasoning models receive automatic specialization boosts."""
    s_flash = score_model("gemini-2.0-flash")
    s_thinking = score_model("gemini-2.0-flash-thinking")

    # Thinking model should have reasoning >= 0.90, significantly outscoring standard flash
    assert s_thinking["reasoning_capability"] >= 0.90
    assert s_thinking["reasoning_capability"] - s_flash["reasoning_capability"] >= 0.20

    # DeepSeek R1 pure reasoning
    s_r1 = score_model("deepseek/deepseek-r1")
    assert s_r1["reasoning_capability"] >= 0.95
    assert s_r1["tier"] == "Rsng"

    # Coder specialization
    s_coder = score_model("qwen/qwen-2.5-coder-32b-instruct")
    assert s_coder["coding_score"] >= 0.85

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


def test_solver_dynamic_metered_override(tmp_path):
    """
    Verifies that the $0 subscription monopoly is broken for high-tier (Tier 2/3) tasks
    when a frontier metered model exceeds the subscription model's fitness by > 20% (0.20).
    """
    db_path = tmp_path / "test_override_catalog.db"
    cat = ModelCatalog(db_path=db_path)

    # Mediocre subscription model (fitness on Tier 3: 0.50 * 0.6 + 0.50 * 0.4 = 0.50)
    sub_model = ReflexModelDefinition(
        id="mediocre-sub", display_name="Mediocre Sub", provider="sub-provider",
        access_method="http_gateway", billing_type="subscription",
        context_window=100_000, max_output_tokens=4_096,
        reasoning_capability=0.50, architecture_score=0.50, coding_score=0.60, speed_score=0.90,
        tool_calling=True, input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=1000.0
    )

    # Elite frontier metered model (fitness on Tier 3: 0.98 * 0.6 + 0.98 * 0.4 - cost penalty = 0.98 - 0.05 = 0.93)
    metered_frontier = ReflexModelDefinition(
        id="frontier-metered", display_name="Frontier Metered", provider="metered-provider",
        access_method="http_gateway", billing_type="metered",
        context_window=200_000, max_output_tokens=16_384,
        reasoning_capability=0.98, architecture_score=0.98, coding_score=0.98, speed_score=0.50,
        tool_calling=True, input_cost_per_m=2.0, output_cost_per_m=8.0, last_updated=1000.0
    )
    cat.upsert_models([sub_model, metered_frontier])
    solver = ArbitrationSolver(cat)

    # 1. Tier 1 routine task: Sub model SHOULD WIN (no metered override for Tier 0/1)
    vec_t1 = CapabilityRequestVector(
        token_count=500, reasoning_depth=0.5, tool_calling=False,
        architecture_score=0.5, modality="text", tier_num=1, explanation="Routine feature task"
    )
    pri_t1, met_t1, _ = solver.arbitrate(vec_t1, session_id="sess-t1")
    assert pri_t1["provider"] == "sub-provider"
    assert pri_t1["billing_type"] == "subscription"

    # 2. Tier 3 architectural spec task: Metered model SHOULD OVERRIDE (>20% delta)
    vec_t3 = CapabilityRequestVector(
        token_count=500, reasoning_depth=0.95, tool_calling=False,
        architecture_score=0.95, modality="text", tier_num=3, explanation="Frontier RFC Architecture"
    )
    pri_t3, met_t3, reason_t3 = solver.arbitrate(vec_t3, session_id="sess-t3")
    assert pri_t3["provider"] == "metered-provider"
    assert pri_t3["billing_type"] == "metered"
    assert "Metered override due to high capability delta" in reason_t3


def test_reflex_cli_catalog_audit(capsys):
    """Verifies that reflex catalog audit CLI runs and outputs structured data."""
    import argparse
    from reflex import cmd_catalog_audit

    # Test JSON mode
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--sort", default="r_cap")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args(["--limit", "3", "--json"])
    cmd_catalog_audit(args)
    captured = capsys.readouterr()
    assert "reasoning_capability" in captured.out
    assert "tier" in captured.out

    # Test Table mode
    args_tbl = parser.parse_args(["--limit", "3"])
    cmd_catalog_audit(args_tbl)
    captured_tbl = capsys.readouterr()
    assert "Reflex Dynamic Model Catalog Audit" in captured_tbl.out
    assert "R-Cap" in captured_tbl.out
    assert "A-Score" in captured_tbl.out
    assert "C-Score" in captured_tbl.out
