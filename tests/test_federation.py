"""
Unit and integration tests for Reflex CLI Harness Federation.
"""
import os
import pytest
from unittest.mock import patch, MagicMock

from federation import (
    discover_harnesses,
    find_binary,
    select_harness_for_model,
    build_delegation_env,
    delegate_subagent,
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
