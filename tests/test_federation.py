"""
Unit and integration tests for Reflex CLI Harness Federation & Failover Waterfall.
"""
import os
import time
import shutil
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from federation import (
    discover_harnesses,
    find_binary,
    select_harness_for_model,
    build_delegation_env,
    delegate_subagent,
    classify_harness_error,
    is_harness_tripped,
    trip_harness_circuit_breaker,
    reset_harness_circuit_breaker,
    get_git_state,
    rollback_git_state,
    MAX_DELEGATION_DEPTH
)

def test_harness_discovery_detects_installed_clis():
    manifest = discover_harnesses(force_rescan=True)
    assert "harnesses" in manifest
    harnesses = manifest["harnesses"]

    # We verified earlier all 5 are installed on this Mac
    assert "agy" in harnesses
    assert "claude" in harnesses
    assert "opencode" in harnesses
    assert "goose" in harnesses
    assert "codex" in harnesses

    assert harnesses["claude"]["binary_path"].endswith("claude")
    assert harnesses["agy"]["binary_path"].endswith("agy")
    assert harnesses["opencode"]["binary_path"].endswith("opencode")

def test_select_harness_for_model():
    manifest = discover_harnesses()
    assert select_harness_for_model("claude-3.7-sonnet", manifest) == "claude"
    assert select_harness_for_model("gemini-2.0-flash", manifest) == "agy"
    assert select_harness_for_model("deepseek-v4-pro", manifest) == "opencode"
    assert select_harness_for_model("o3-mini", manifest) == "codex"

def test_build_delegation_env_augments_path_and_tracks_lineage():
    env = build_delegation_env(current_harness="claude")
    assert ".local/bin" in env["PATH"]
    assert ".opencode/bin" in env["PATH"]
    assert env["TERM"] == "dumb"
    assert env["REFLEX_DELEGATION_DEPTH"] == "1"
    assert env["REFLEX_DELEGATION_CHAIN"] == "claude"

    # Second hop
    env2 = build_delegation_env(parent_env=env, current_harness="opencode")
    assert env2["REFLEX_DELEGATION_DEPTH"] == "2"
    assert env2["REFLEX_DELEGATION_CHAIN"] == "claude:opencode"

@pytest.mark.anyio
async def test_recursion_limit_guard():
    # Force depth = 2 in env
    with patch.dict(os.environ, {"REFLEX_DELEGATION_DEPTH": "2", "REFLEX_DELEGATION_CHAIN": "claude:opencode"}):
        result = await delegate_subagent(task="Fix deadlock", preferred_model="gemini")
        assert result["status"] == "error"
        assert "RecursionLimitExceeded" in result["error"]

@pytest.mark.anyio
async def test_cycle_detection_guard():
    # Chain already contains 'claude'
    with patch.dict(os.environ, {"REFLEX_DELEGATION_DEPTH": "1", "REFLEX_DELEGATION_CHAIN": "claude"}):
        result = await delegate_subagent(task="Fix deadlock", preferred_harness="claude")
        assert result["status"] == "cycle_detected"
        assert "Cycle detected" in result["message"]

def test_classify_harness_error_rate_limits():
    # 429 rate limit
    rec, reason, cd = classify_harness_error("claude", 1, "Rate limit exceeded (429)", "")
    assert rec is True
    assert reason == "rate_limit_exceeded"
    assert cd == 300

    # Weekly rate limit
    rec, reason, cd = classify_harness_error("claude", 1, "You have reached your weekly limit", "")
    assert rec is True
    assert reason == "rate_limit_exceeded"
    assert cd == 3600

    # User test failure is NOT recoverable infrastructure error
    rec, reason, cd = classify_harness_error("claude", 1, "FAILED tests/test_foo.py::test_bar - AssertionError", "")
    assert rec is False
    assert reason == "test_or_code_failure"

    # Auth error
    rec, reason, cd = classify_harness_error("claude", 1, "401 Unauthorized: token expired", "")
    assert rec is True
    assert reason == "auth_failure"

def test_circuit_breaker_trip_and_reset(tmp_path):
    cb_file = tmp_path / "cb.json"
    with patch("federation.CIRCUIT_BREAKER_FILE", cb_file):
        assert is_harness_tripped("test_harness")[0] is False

        trip_harness_circuit_breaker("test_harness", "rate_limit", 300)
        tripped, reason = is_harness_tripped("test_harness")
        assert tripped is True
        assert "rate_limit" in reason

        reset_harness_circuit_breaker("test_harness")
        assert is_harness_tripped("test_harness")[0] is False

def test_selective_git_rollback(tmp_path):
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    tracked = repo / "existing.txt"
    tracked.write_text("initial content\n")
    subprocess.run(["git", "add", "existing.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    # Pre-existing modification by developer
    tracked.write_text("developer uncommitted work\n")

    baseline = get_git_state(str(repo))
    assert baseline is not None
    assert "existing.txt" in baseline["status_lines"][0]

    # Subagent runs and leaves junk
    junk = repo / "bad_file.py"
    junk.write_text("corrupted code\n")

    # Selective rollback should wipe bad_file.py but PRESERVE existing.txt modification
    success = rollback_git_state(str(repo), baseline)
    assert success is True
    assert not junk.exists()
    assert tracked.read_text() == "developer uncommitted work\n"

@pytest.mark.anyio
async def test_failover_waterfall_when_primary_429():
    call_counts = {}

    async def mock_execute(cmd, cwd=None, env=None, timeout_sec=60.0):
        bin_name = os.path.basename(cmd[0])
        call_counts[bin_name] = call_counts.get(bin_name, 0) + 1
        if bin_name == "claude":
            # Simulate 429
            return 1, "Weekly limit reached for Claude Code", ""
        elif bin_name == "opencode":
            # Simulate success on secondary
            return 0, '{"message": "OpenCode completed the task"}', ""
        return 1, "failed", ""

    with patch("federation.execute_harness_safe", side_effect=mock_execute), \
         patch("federation.is_harness_tripped", return_value=(False, None)):
        
        result = await delegate_subagent(
            task="Refactor caching",
            preferred_harness="claude",
            cwd=None
        )

        assert result["status"] == "success"
        assert result["effective_harness"] == "opencode"
        assert len(result["attempts"]) == 2
        assert result["attempts"][0]["harness"] == "claude"
        assert result["attempts"][0]["status"] == "failover"
        assert result["attempts"][0]["reason"] == "rate_limit_exceeded"
        assert result["attempts"][1]["harness"] == "opencode"
        assert result["attempts"][1]["status"] == "success"

@pytest.mark.anyio
async def test_fallback_to_http_gateway_when_all_harnesses_fail():
    async def mock_execute(cmd, cwd=None, env=None, timeout_sec=60.0):
        # All local CLIs hit 429
        return 1, "Rate limit exceeded (429)", ""

    mock_gateway_res = {
        "status": "success",
        "harness": "reflex_http_gateway",
        "response": "Gateway synthesized solution",
        "duration_ms": 250
    }

    with patch("federation.execute_harness_safe", side_effect=mock_execute), \
         patch("federation.call_reflex_http_gateway", AsyncMock(return_value=mock_gateway_res)), \
         patch("federation.is_harness_tripped", return_value=(False, None)):

        result = await delegate_subagent(
            task="Emergency task",
            preferred_harness="claude"
        )

        assert result["status"] == "success"
        assert result["effective_harness"] == "reflex_http_gateway"
        assert result["response"] == "Gateway synthesized solution"
