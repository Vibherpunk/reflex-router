"""Test-suite fixtures for the Reflex router.

Sets the gateway auth token BEFORE any test module imports `server`
(conftest loads first), so the fail-closed startup guard and the
per-request Bearer <redacted> both see a configured token.
"""
import os

os.environ.setdefault("REFLEX_GATEWAY_TOKEN", "test-token-reflex-local")

import shutil

import pytest


def requires_harnesses(*names, authenticated=True):
    """Skip a test unless the named CLI harnesses exist on this host.

    The federation/harness tests were written against Adam's Mac (agy,
    opencode, goose installed + authenticated). On hosts without them the
    tests skip with a clear reason instead of failing on environment.
    """
    missing = []
    manifest = {}
    if authenticated:
        from federation import discover_harnesses

        manifest = discover_harnesses().get("harnesses", {})
    for n in names:
        if authenticated:
            h = manifest.get(n)
            if not (h and h.get("authenticated")):
                missing.append(n)
        elif shutil.which(n) is None:
            missing.append(n)
    return pytest.mark.skipif(
        bool(missing),
        reason=f"harness(es) not installed/authenticated on this host: {missing}",
    )


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer <redacted>"}


@pytest.fixture
def bad_auth_headers():
    return {"Authorization": "Bearer wrong-token"}


@pytest.fixture(scope="session", autouse=True)
def _isolated_decision_log(tmp_path_factory):
    """Keep the suite off the real audit trail.

    Endpoint tests in test_router.py / test_audit_patches.py /
    test_effort_routing.py exercise the server decision-log hook; without
    this fixture they append synthetic records to ~/.reflex/decisions.jsonl
    (20 observed in one run) referencing catalog models that the seed
    fixture later deletes — unreplayable entries corrupting the audit
    trail the module exists to protect.
    """
    import decision_log

    tmp = tmp_path_factory.mktemp("reflex_decisions")
    old = decision_log.DECISION_LOG_PATH
    decision_log.DECISION_LOG_PATH = tmp / "decisions.jsonl"
    yield
    decision_log.DECISION_LOG_PATH = old


# ---------------------------------------------------------------------------
# Hermetic catalog seed.
#
# Many routing tests were written against the live daemon's populated catalog
# (~/.reflex/catalog.db). In a fresh sandbox/CI checkout the catalog is empty
# (no provider credentials or CLI harnesses), which made 16 tests fail with
# "Reflex Model Catalog is empty" — an environment precondition, not a code
# bug. This session-scoped fixture upserts a small synthetic catalog ONLY when
# the live catalog is empty, and removes the seeded rows afterwards, so the
# suite is hermetic everywhere without touching real catalog data.
# ---------------------------------------------------------------------------

_SEED_MODELS = [
    # (id, provider, billing_type, reasoning, architecture, context_window)
    ("deepseek-v4-flash", "opencode-go", "subscription", 0.90, 0.85, 200_000),
    ("deepseek-v4-pro", "opencode-go", "subscription", 0.97, 0.95, 200_000),
    ("gemini-2.5-flash", "gemini", "subscription", 0.85, 0.80, 1_000_000),
    ("claude-opus-4", "claude-cli", "subscription", 0.97, 0.97, 200_000),
    ("openai/gpt-5", "openrouter", "metered", 0.97, 0.96, 400_000),
]


def _seed_definitions():
    from catalog import ReflexModelDefinition

    defs = []
    for mid, prov, billing, rc, arch, ctx in _SEED_MODELS:
        defs.append(ReflexModelDefinition(
            id=mid, display_name=mid, provider=prov,
            access_method="http_gateway", billing_type=billing,
            context_window=ctx, max_output_tokens=4000,
            reasoning_capability=rc, architecture_score=arch,
            coding_score=0.9, speed_score=0.9, tool_calling=True,
            input_cost_per_m=0.0 if billing == "subscription" else 3.0,
            output_cost_per_m=0.0 if billing == "subscription" else 15.0,
            last_updated=1.0, base_url="http://127.0.0.1:9",
            api_key_env="REFLEX_TEST_DUMMY_KEY",
        ))
    return defs


@pytest.fixture(scope="session", autouse=True)
def seeded_catalog():
    """Seed a synthetic catalog when (and only when) the live one is empty."""
    import server

    seeded = False
    if not server.catalog.list_all():
        server.catalog.upsert_models(_seed_definitions())
        seeded = True
    yield
    if seeded:
        # Remove only the rows this fixture added; leave real data untouched.
        ids = [m[0] for m in _SEED_MODELS]
        with server.catalog._connect() as conn:
            conn.executemany("DELETE FROM models WHERE id = ?", [(i,) for i in ids])
        server.catalog._load_cache()
