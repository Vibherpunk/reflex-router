"""
Unit tests for Reflex Memory Engine (Skidnir-style recursive learning).
"""
import os
import tempfile
import pytest

from memory import init_db, record_incident, check_memory, get_stats, generate_shingles

@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)

def test_shingle_generation():
    prompt = "Fix the deadlock in the async queue worker"
    shingles = generate_shingles(prompt)
    assert "deadlock" in shingles
    assert "async" in shingles
    assert "queue" in shingles
    assert "deadlock_async" in shingles or "async_queue" in shingles

def test_record_and_exact_match(temp_db):
    prompt = "Validate this broken AST transformation for n8n node"
    inc_id = record_incident(
        prompt=prompt,
        failed_tier=0,
        escalated_tier=2,
        error_signature="SyntaxError: Unexpected token",
        db_path=temp_db
    )
    assert inc_id > 0

    # Checking with identical prompt should yield high similarity and escalate
    match = check_memory(prompt, threshold=0.5, db_path=temp_db)
    assert match is not None
    assert match["incident_id"] == inc_id
    assert match["escalated_tier"] == 2
    assert match["failed_tier"] == 0
    assert match["similarity"] > 0.9

def test_similar_prompt_escalation(temp_db):
    """Slightly rephrased prompt regarding the same failure domain should trigger escalation."""
    original = "Fix race condition in redis token bucket rate limiter"
    record_incident(
        prompt=original,
        failed_tier=1,
        escalated_tier=3,
        error_signature="OverLimitException: concurrent claim failed",
        db_path=temp_db
    )

    # Similar query
    query = "Investigate redis token bucket rate limiter concurrency"
    match = check_memory(query, threshold=0.4, db_path=temp_db)
    assert match is not None
    assert match["escalated_tier"] == 3

def test_unrelated_prompt_no_match(temp_db):
    prompt = "Convert this date string to ISO format"
    match = check_memory(prompt, threshold=0.5, db_path=temp_db)
    assert match is None

def test_stats_and_metrics(temp_db):
    record_incident("Bug 1 in AST linter", 0, 2, "error 1", temp_db)
    record_incident("Bug 2 in AST linter", 1, 3, "error 2", temp_db)
    stats = get_stats(temp_db)
    assert stats["total_incidents"] == 2
    assert len(stats["recent_incidents"]) == 2
