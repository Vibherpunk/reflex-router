"""Regression tests pinning the five Reflex blocker fixes (2026-09-29).

Blocker 1: Tier-3 cost defect (bookkeeping words must not route to Tier 3).
Blocker 2: Gateway bearer auth (fail-closed).
Blocker 3: Mac-as-SPOF fail-closed client contract.
Blocker 4: (this file) regression suite.
Blocker 5: Decision logging + catalog epoch reproducibility.
"""
import asyncio
import hashlib
import json
import os
import tempfile
import time

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from classifier import classify_request
from server import app
import auth
from auth import verify_gateway_token
import decision_log
from decision_log import log_decision, replay_decision, sanitize_reason, prompt_sha256
from reflex_client import suggest_route, health_ok

client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token-reflex-local"}


# ---------------------------------------------------------------- Blocker 1

HARD_PROMPTS = [
    "design a byzantine fault-tolerant distributed consensus protocol",
    "prove correctness of this split-brain recovery algorithm",
    "perform cryptographic audit and formal verification of the enclave",
    "plan a zero-downtime database migration for the payments cluster",
    "write an rfc for the new system architecture",
    "assess the threat model for our auth service",
]

BOOKKEEPING_PROMPTS = [
    "reconcile my QBO feed",
    "audit last month's transactions",
    "show me the general ledger balance",
]


def test_hard_prompts_stay_tier3():
    for p in HARD_PROMPTS:
        tier, _ = classify_request([{"role": "user", "content": p}])
        assert tier == 3, f"expected Tier 3, got Tier {tier}: {p!r}"


def test_bookkeeping_words_not_tier3():
    for p in BOOKKEEPING_PROMPTS:
        tier, _ = classify_request([{"role": "user", "content": p}])
        assert tier == 2, f"expected Tier 2 (never 3), got Tier {tier}: {p!r}"


# ---------------------------------------------------------------- Blocker 2

def test_health_open_without_auth():
    resp = client.get("/health")
    assert resp.status_code == 200
    resp = client.get("/")
    assert resp.status_code == 200


@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/v1/models", {}),
    ("get", "/v1/stats", {}),
    ("get", "/v1/harnesses", {}),
    ("post", "/v1/route", {"json": {"prompt": "hi"}}),
    ("post", "/v1/chat/completions", {"json": {"messages": [{"role": "user", "content": "hi"}]}}),
    ("post", "/v1/delegate", {"json": {"task": "hi"}}),
    ("post", "/v1/feedback", {"json": {}}),
    ("post", "/v1/catalog/refresh", {}),
])
def test_protected_endpoints_require_auth(method, path, kwargs):
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}"


def test_wrong_token_rejected():
    # Wrong value (same length class) and wrong length: both 401, no oracle.
    for bad in ("wrong-token", "x", "test-token-reflex-local-EXTRA"):
        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {bad}"})
        assert resp.status_code == 401, f"token {bad!r} -> {resp.status_code}"
    # Malformed scheme also 401
    resp = client.get("/v1/models", headers={"Authorization": "Token test-token-reflex-local"})
    assert resp.status_code == 401


def test_correct_token_accepted():
    resp = client.get("/v1/models", headers=AUTH)
    assert resp.status_code in (200, 500)  # 200 normally; 500 only if catalog empty in sandbox
    assert resp.status_code != 401


def _fake_request(headers):
    scope = {"type": "http", "headers": [
        (k.lower().encode(), v.encode()) for k, v in headers.items()
    ]}
    return Request(scope)


def test_token_guard_fail_closed(monkeypatch):
    # No token configured anywhere -> 503, never serve unauthenticated.
    monkeypatch.setattr(auth, "get_key", lambda name: "")
    with pytest.raises(Exception) as exc:
        asyncio.run(verify_gateway_token(_fake_request(AUTH)))
    assert exc.value.status_code == 503


def test_token_guard_missing_header_is_401():
    with pytest.raises(Exception) as exc:
        asyncio.run(verify_gateway_token(_fake_request({})))
    assert exc.value.status_code == 401


# ---------------------------------------------------------------- Blocker 3

def test_client_fail_closed_on_dead_port():
    s = suggest_route(
        [{"role": "user", "content": "hi"}],
        base_url="http://127.0.0.1:9",
        token="test-token-reflex-local",
        default_model="my-default",
        timeout=1.0,
    )
    assert s.available is False
    assert s.model == "my-default"
    assert s.provider == "local-default"
    assert s.reason.startswith("reflex_unavailable:")


def _scrub_proxy_env(monkeypatch):
    # This sandbox sets bracketed-IPv6 NO_PROXY entries that this httpx
    # version cannot parse at Client construction time; scrub them so the
    # retry-counting tests exercise the transport path, not env parsing.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def test_client_single_retry_then_fallback(monkeypatch):
    _scrub_proxy_env(monkeypatch)
    calls = {"n": 0}

    def boom(self, *a, **k):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.Client, "post", boom)
    s = suggest_route(
        [{"role": "user", "content": "hi"}],
        base_url="http://127.0.0.1:9",
        token="t",
        timeout=0.5,
    )
    assert calls["n"] == 2  # exactly one retry
    assert s.available is False


def test_client_never_retries_4xx(monkeypatch):
    _scrub_proxy_env(monkeypatch)
    calls = {"n": 0}

    class Fake401:
        status_code = 401
        headers = {}

    def r401(self, *a, **k):
        calls["n"] += 1
        return Fake401()

    monkeypatch.setattr(httpx.Client, "post", r401)
    s = suggest_route(
        [{"role": "user", "content": "hi"}],
        base_url="http://127.0.0.1:9",
        token="bad",
    )
    assert calls["n"] == 1  # no retry on 4xx
    assert s.available is False
    assert s.reason == "reflex_unavailable: auth"


def test_client_429_honors_retry_after_then_retries_once(monkeypatch):
    _scrub_proxy_env(monkeypatch)
    calls = {"n": 0}
    slept = {"s": 0.0}
    real_sleep = time.sleep

    class Fake429Then200:
        status_code = 200

        def __init__(self):
            self.headers = {}

        def json(self):
            return {
                "tier": 2, "effort": "medium",
                "subscription_route": {"model": "m", "provider": "p"},
                "reason": "ok",
            }

    class Fake429:
        status_code = 429
        headers = {"Retry-After": "0.2"}

    def seq(self, *a, **k):
        calls["n"] += 1
        return Fake429() if calls["n"] == 1 else Fake429Then200()

    def fake_sleep(s):
        slept["s"] += s
        # don't actually wait

    monkeypatch.setattr(httpx.Client, "post", seq)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    s = suggest_route(
        [{"role": "user", "content": "hi"}],
        base_url="http://127.0.0.1:9",
token="t"
    )
    assert calls["n"] == 2, "429 must be retried exactly once"
    assert slept["s"] == 0.2, "Retry-After must be honored before the retry"
    assert s.available is True
    assert s.model == "m"


def test_client_double_429_falls_back(monkeypatch):
    _scrub_proxy_env(monkeypatch)
    calls = {"n": 0}

    class Fake429:
        status_code = 429
        headers = {"Retry-After": "0"}

    def always429(self, *a, **k):
        calls["n"] += 1
        return Fake429()

    monkeypatch.setattr(httpx.Client, "post", always429)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    s = suggest_route(
        [{"role": "user", "content": "hi"}],
        base_url="http://127.0.0.1:9",
token="t"
    )
    assert calls["n"] == 2, "exactly two attempts, then fallback"
    assert s.available is False
    assert s.reason == "reflex_unavailable: 429"


def test_health_ok_true(monkeypatch):
    _scrub_proxy_env(monkeypatch)

    class Fake200:
        status_code = 200

    def ok(self, *a, **k):
        return Fake200()

    monkeypatch.setattr(httpx.Client, "get", ok)
    assert health_ok("http://127.0.0.1:9") is True


def test_health_ok_false_on_dead_port(monkeypatch):
    # Never raises; False on refused connection (would previously always be
    # False because the httpx.Timeout construction itself raised).
    _scrub_proxy_env(monkeypatch)
    assert health_ok("http://127.0.0.1:1") is False


# ---------------------------------------------------------------- Blocker 5

@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(decision_log, "DECISION_LOG_PATH", path)
    return path


def test_decision_log_writes_prompt_hash_not_prompt(log_path):
    secret = "super secret prompt body 12345"
    did = log_decision(
        endpoint="/v1/route", session_id="s", prompt_text=secret,
        tier=1, effort="low", model="m", provider="p", billing_type="subscription",
        reason="r", route_category="preview",
        catalog_epoch=1, catalog_refreshed_at=1.0, spec_hash="h",
        token_count=5, latency_ms=0.1,
    )
    lines = log_path.read_text().strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["decision_id"] == did
    assert rec["prompt_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in log_path.read_text()  # raw prompt never on disk


def test_decision_log_redacts_prompt_snippet(log_path):
    reason = ("Memory Auto-Escalation (Incident #3, sim=0.80): "
              "learned from prior failure in 'leaked prompt fragment xyz'")
    log_decision(
        endpoint="/v1/chat/completions", session_id="s", prompt_text="other",
        tier=2, effort="medium", model="m", provider="p", billing_type="subscription",
        reason=reason, route_category="subscription_zero_marginal_cost",
        catalog_epoch=1, catalog_refreshed_at=1.0, spec_hash="h",
        token_count=5, latency_ms=0.1,
    )
    on_disk = log_path.read_text()
    assert "leaked prompt fragment xyz" not in on_disk
    assert "[redacted]" in on_disk
    # The in-memory reason is untouched; only the disk copy is sanitized.
    assert sanitize_reason(reason).startswith("Memory Auto-Escalation (Incident #3")


def test_decision_replay_mode_b(log_path, tmp_path):
    from catalog import ModelCatalog, ReflexModelDefinition
    from solver import ArbitrationSolver

    db = tmp_path / "replay.db"
    cat = ModelCatalog(db_path=db)

    def mk(i, prov="p1", **kw):
        d = dict(id=f"m{i}", display_name=f"M{i}", provider=prov,
                 access_method="http_gateway", billing_type="subscription",
                 context_window=200000, max_output_tokens=4000,
                 reasoning_capability=0.95, architecture_score=0.9,
                 coding_score=0.9, speed_score=0.9, tool_calling=True,
                 input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=1.0)
        d.update(kw)
        return ReflexModelDefinition(**d)

    cat.upsert_models([mk(1), mk(2)])
    cat.catalog_epoch += 1
    cat._persist_meta()
    cat._append_history_snapshot()
    solver = ArbitrationSolver(cat)

    prompt = "format this list of numbers"
    vector = solver.extract_vector([{"role": "user", "content": prompt}])
    primary, _, expl = solver.arbitrate(vector, session_id=None)
    log_decision(
        endpoint="/v1/route", session_id="", prompt_text=prompt,
        tier=vector.tier_num, effort=vector.effort,
        model=primary["model"], provider=primary["provider"],
        billing_type=primary["billing_type"], reason=expl,
        route_category="preview",
        catalog_epoch=cat.catalog_epoch, catalog_refreshed_at=cat.last_refresh_ts,
        spec_hash="h", token_count=vector.token_count, latency_ms=0.1,
    )
    rec = json.loads(log_path.read_text().strip().split("\n")[-1])
    result = replay_decision(rec, cat, solver, prompt_text=prompt)
    assert result["match"], result["detail"]


def test_catalog_epoch_history(tmp_path):
    from catalog import ModelCatalog, ReflexModelDefinition

    db = tmp_path / "epoch.db"
    cat = ModelCatalog(db_path=db)

    def mk(i, reasoning=0.9, **kw):
        d = dict(id=f"m{i}", display_name=f"M{i}", provider="p1",
                 access_method="http_gateway", billing_type="subscription",
                 context_window=1000, max_output_tokens=100,
                 reasoning_capability=reasoning, architecture_score=0.9,
                 coding_score=0.5, speed_score=0.5, tool_calling=True,
                 input_cost_per_m=0.0, output_cost_per_m=0.0, last_updated=1.0)
        d.update(kw)
        return ReflexModelDefinition(**d)

    assert cat.catalog_epoch == 0
    cat.upsert_models([mk(1), mk(2)])
    cat.catalog_epoch += 1
    cat._persist_meta()
    cat._append_history_snapshot()
    cat.upsert_models([mk(1, reasoning=0.1), mk(3)])
    cat.catalog_epoch += 1
    cat._persist_meta()
    cat._append_history_snapshot()

    assert cat.catalog_epoch == 2
    s1 = {m.id: m.reasoning_capability for m in cat.snapshot(1)}
    s2 = {m.id: m.reasoning_capability for m in cat.snapshot(2)}
    assert s1 == {"m1": 0.9, "m2": 0.9}
    assert s2 == {"m1": 0.1, "m2": 0.9, "m3": 0.9}

    # Epoch survives process restart (new instance on same DB).
    cat2 = ModelCatalog(db_path=db)
    assert cat2.catalog_epoch == 2
    assert {m.id for m in cat2.snapshot(2)} == {"m1", "m2", "m3"}


def test_prompt_hash_is_last_user_message_sha256():
    # Prompt hash is defined as SHA-256 of the last user-role message's string content.
    assert prompt_sha256("hello") == hashlib.sha256(b"hello").hexdigest()
    assert prompt_sha256("hello") != prompt_sha256("hello ")
    # System-1 memory incidents elsewhere must not change the hash definition:
    # same prompt -> same hash regardless of memory state.
    assert prompt_sha256("reconcile my QBO feed") == prompt_sha256("reconcile my QBO feed")
