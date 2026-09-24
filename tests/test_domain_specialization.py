"""
Unit and integration tests for Domain Specialization Engine in Reflex Router.
Tests domain classification, model semantic detection, arbitration domain boosting,
and specialized model override for legal, medical, finance, and math verticals.
Enforces Zero Hardcoded Logic & Math (all thresholds sourced from scoring_spec.yaml).
"""
import pytest
from classifier import classify_domain, classify_full_request
from catalog import extract_model_semantics, ReflexModelDefinition, ModelCatalog
from solver import ArbitrationSolver, CapabilityRequestVector
import scoring_spec


def test_domain_keywords_loaded_from_spec():
    specs = scoring_spec.get_domain_specializations()
    assert "legal" in specs
    assert "medical" in specs
    assert "finance" in specs
    assert "math" in specs

    assert scoring_spec.get_domain_boost("legal") == 0.35
    assert scoring_spec.get_domain_boost("medical") == 0.35
    assert scoring_spec.get_domain_boost("finance") == 0.30
    assert scoring_spec.get_domain_boost("math") == 0.30


def test_classify_domain_legal():
    messages = [{"role": "user", "content": "Review the master services agreement and check for indemnification and jurisdiction."}]
    domain = classify_domain(messages)
    assert domain == "legal"


def test_classify_domain_medical():
    messages = [{"role": "user", "content": "Generate a clinical soap note with icd-10 clinical codes for patient symptoms."}]
    domain = classify_domain(messages)
    assert domain == "medical"


def test_classify_domain_finance():
    messages = [{"role": "user", "content": "Audit the balance sheet and calculate dcf valuation under gaap accounting."}]
    domain = classify_domain(messages)
    assert domain == "finance"


def test_classify_domain_math():
    messages = [{"role": "user", "content": "Provide a rigorous mathematical proof for this calculus theorem."}]
    domain = classify_domain(messages)
    assert domain == "math"


def test_classify_domain_general_none():
    messages = [{"role": "user", "content": "Write a python script to parse a json file and print results."}]
    domain = classify_domain(messages)
    assert domain is None


def test_classify_full_request():
    messages = [{"role": "user", "content": "Implement the legal litigation defense strategy module and refactor the compliance handler"}]
    tier, explanation, domain = classify_full_request(messages)
    assert domain == "legal"
    assert tier == 1


def test_extract_model_semantics_domain_tags():
    # Legal
    sem_legal = extract_model_semantics("equall/saul-7b-instruct", {})
    assert sem_legal["domain_specialization"] == "legal"

    # Medical
    sem_med = extract_model_semantics("mistralai/biomistral-7b", {})
    assert sem_med["domain_specialization"] == "medical"

    # Finance
    sem_fin = extract_model_semantics("meta-llama/finllama-8b", {})
    assert sem_fin["domain_specialization"] == "finance"

    # Math
    sem_math = extract_model_semantics("mistralai/mathstral-7b-v0.1", {})
    assert sem_math["domain_specialization"] == "math"

    # Generalist
    sem_gen = extract_model_semantics("anthropic/claude-3-7-sonnet", {})
    assert sem_gen["domain_specialization"] is None


def test_arbitration_specialized_beats_generalist_on_domain_query():
    """
    Verifies that a specialized model (e.g. Saul-7B for legal) outscores a generalist
    frontier model (e.g. Claude 3.7 Sonnet) when the query is classified into that domain,
    via the declarative domain boost.
    """
    # Generalist subscription model (baseline reasoning=0.88, arch=0.88, coding=0.88)
    generalist_sub = ReflexModelDefinition(
        id="anthropic/claude-3-7-sonnet",
        display_name="Claude 3.7 Sonnet",
        provider="opencode-go",
        access_method="http_gateway",
        billing_type="subscription",
        context_window=200_000,
        max_output_tokens=8_192,
        reasoning_capability=0.88,
        architecture_score=0.88,
        coding_score=0.88,
        speed_score=0.75,
        tool_calling=True,
        input_cost_per_m=0.0,
        output_cost_per_m=0.0,
        last_updated=1000.0,
        generation=3.7,
        domain_specialization=None
    )

    # Purpose-driven legal model (baseline reasoning=0.65, coding=0.65, domain_specialization='legal')
    specialist_metered = ReflexModelDefinition(
        id="openrouter/equall/saul-7b-instruct",
        display_name="Saul 7B Legal Instruct",
        provider="openrouter",
        access_method="http_gateway",
        billing_type="metered",
        context_window=32_000,
        max_output_tokens=4_096,
        reasoning_capability=0.65,
        architecture_score=0.65,
        coding_score=0.65,
        speed_score=0.85,
        tool_calling=True,
        input_cost_per_m=0.20,
        output_cost_per_m=0.20,
        last_updated=1000.0,
        generation=1.0,
        domain_specialization="legal"
    )

    # In-memory mock catalog
    class MockCatalog:
        def list_all(self):
            return [generalist_sub, specialist_metered]

    solver = ArbitrationSolver(MockCatalog())

    # 1. Non-legal query: generalist subscription should win
    messages_gen = [{"role": "user", "content": "Refactor this python web server to use asyncio"}]
    vec_gen = solver.extract_vector(messages_gen)
    assert vec_gen.domain is None
    primary, _, explanation = solver.arbitrate(vec_gen, session_id="test_sess_1")
    assert primary["model"] == "anthropic/claude-3-7-sonnet"

    # 2. Legal query: specialist metered model should get the +0.35 boost and win via domain override
    messages_legal = [{"role": "user", "content": "Draft a mutual indemnification and liability clause for Delaware jurisdiction."}]
    vec_legal = solver.extract_vector(messages_legal)
    assert vec_legal.domain == "legal"
    primary_legal, _, explanation_legal = solver.arbitrate(vec_legal, session_id="test_sess_2")
    assert primary_legal["model"] == "openrouter/equall/saul-7b-instruct"
    assert "Domain specialist override for 'legal'" in explanation_legal


def test_arbitration_subscription_specialist_preferred_over_metered():
    """
    If a user has a local / subscription specialized model (e.g. Unsloth / Ollama saul-7b),
    it should win at $0 marginal cost over the metered equivalent.
    """
    local_sub_specialist = ReflexModelDefinition(
        id="ollama/saul-7b-q8",
        display_name="Local Saul 7B Legal",
        provider="ollama",
        access_method="http_gateway",
        billing_type="subscription",
        context_window=32_000,
        max_output_tokens=4_096,
        reasoning_capability=0.65,
        architecture_score=0.65,
        coding_score=0.65,
        speed_score=0.85,
        tool_calling=True,
        input_cost_per_m=0.0,
        output_cost_per_m=0.0,
        last_updated=1000.0,
        generation=1.0,
        domain_specialization="legal"
    )

    metered_specialist = ReflexModelDefinition(
        id="openrouter/equall/saul-7b-instruct",
        display_name="Cloud Saul 7B Legal",
        provider="openrouter",
        access_method="http_gateway",
        billing_type="metered",
        context_window=32_000,
        max_output_tokens=4_096,
        reasoning_capability=0.65,
        architecture_score=0.65,
        coding_score=0.65,
        speed_score=0.85,
        tool_calling=True,
        input_cost_per_m=0.20,
        output_cost_per_m=0.20,
        last_updated=1000.0,
        generation=1.0,
        domain_specialization="legal"
    )

    class MockCatalog:
        def list_all(self):
            return [local_sub_specialist, metered_specialist]

    solver = ArbitrationSolver(MockCatalog())
    messages = [{"role": "user", "content": "Review this contract agreement and identify statutory risks"}]
    vec = solver.extract_vector(messages)
    assert vec.domain == "legal"

    primary, fallback, explanation = solver.arbitrate(vec, session_id="test_sess_sub")
    assert primary["model"] == "ollama/saul-7b-q8"
    assert primary["billing_type"] == "subscription"
