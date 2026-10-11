"""Tests for agy/muse harness specs, CLI provider discovery fail-soft, and gateway token fallback."""
import importlib
import tempfile
import time
from pathlib import Path

import pytest

import config
import federation
import catalog as catalog_mod


# ---------- token fallback ----------

def test_gateway_token_file_fallback(monkeypatch, tmp_path):
    tok = tmp_path / ".gateway_token"
    tok.write_text("  unit-test-token-value \n")
    monkeypatch.delenv("REFLEX_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(config, "HERMES_ENV", {})
    monkeypatch.setattr(config, "GATEWAY_TOKEN_FILE", tok)
    assert config.get_key("REFLEX_GATEWAY_TOKEN") == "unit-test-token-value"


def test_gateway_token_env_wins_over_file(monkeypatch, tmp_path):
    tok = tmp_path / ".gateway_token"
    tok.write_text("file-value")
    monkeypatch.setattr(config, "GATEWAY_TOKEN_FILE", tok)
    monkeypatch.setenv("REFLEX_GATEWAY_TOKEN", "env-value")
    assert config.get_key("REFLEX_GATEWAY_TOKEN") == "env-value"


def test_gateway_token_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("REFLEX_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(config, "HERMES_ENV", {})
    monkeypatch.setattr(config, "GATEWAY_TOKEN_FILE", tmp_path / "nope")
    assert config.get_key("REFLEX_GATEWAY_TOKEN") == ""


def test_token_file_not_used_for_other_keys(monkeypatch, tmp_path):
    tok = tmp_path / ".gateway_token"
    tok.write_text("secret")
    monkeypatch.setattr(config, "GATEWAY_TOKEN_FILE", tok)
    monkeypatch.setattr(config, "HERMES_ENV", {})
    monkeypatch.delenv("SOME_OTHER_KEY", raising=False)
    assert config.get_key("SOME_OTHER_KEY") == ""


# ---------- harness specs ----------

def _set_harness_cfg(monkeypatch, name, **kw):
    harnesses = dict(config.CONFIG.get("harnesses", {}))
    harnesses[name] = {**harnesses.get(name, {}), **kw}
    monkeypatch.setitem(config.CONFIG, "harnesses", harnesses)


def test_muse_spec_registered_everywhere():
    assert "muse" in federation.HARNESS_SPECS
    assert federation.HARNESS_FAILOVER_CHAINS["muse"][0] == "muse"
    assert federation.HARNESS_SPECS["muse"]["auth_files"]


def test_muse_cmd_restrictive_by_default(monkeypatch):
    _set_harness_cfg(monkeypatch, "muse", allow_yolo=False, default_model=None)
    cmd = federation.build_harness_cmd("muse", "/bin/muse", "-do thing", model=None, cwd="/tmp/ws")
    assert cmd[:2] == ["/bin/muse", "exec"]
    assert "--yolo" not in cmd
    assert "--disable-sandbox" not in cmd and "--disable-approval" not in cmd
    assert cmd[cmd.index("--workspace") + 1] == "/tmp/ws"
    assert cmd[cmd.index("--approval-mode") + 1] == "on-request"
    assert "--model" not in cmd
    # task is passed after `--` so a leading dash can't be parsed as a flag
    assert cmd[-2:] == ["--", "-do thing"]


def test_muse_yolo_requires_explicit_opt_in(monkeypatch):
    _set_harness_cfg(monkeypatch, "muse", allow_yolo="yes")  # truthy but not True
    assert "--yolo" not in federation.build_harness_cmd("muse", "m", "t", cwd="/tmp")
    _set_harness_cfg(monkeypatch, "muse", allow_yolo=True)
    assert "--yolo" in federation.build_harness_cmd("muse", "m", "t", cwd="/tmp")


def test_muse_model_passthrough(monkeypatch):
    _set_harness_cfg(monkeypatch, "muse", default_model=None)
    cmd = federation.build_harness_cmd("muse", "m", "t", model="muse-spark-1.3", cwd="/tmp")
    assert cmd[cmd.index("--model") + 1] == "muse-spark-1.3"


def test_agy_cmd_model_and_permissions(monkeypatch):
    _set_harness_cfg(monkeypatch, "agy", skip_permissions=False, default_model=None)
    cmd = federation.build_harness_cmd("agy", "/bin/agy", "task", model="gemini-3.1-pro-high", cwd="/tmp")
    assert cmd[:3] == ["/bin/agy", "-p", "task"]
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-high"


def test_legacy_specs_still_two_arg():
    cmd = federation.build_harness_cmd("claude", "/bin/claude", "task", model="x", cwd="/tmp")
    assert cmd[:3] == ["/bin/claude", "-p", "task"]
    assert "x" not in cmd


def test_model_for_harness_gates_foreign_models(monkeypatch):
    info = {"models": ["muse-spark-1.3"]}
    assert federation._model_for_harness("muse", info, "auto") is None
    assert federation._model_for_harness("muse", info, "muse-spark-1.3") == "muse-spark-1.3"

    class _EmptyCatalog:
        """In-memory catalog double with no rows for any provider."""
        def __init__(self):
            self._rows = {}

        def get(self, provider, model_id):
            return self._rows.get((provider, model_id))
    monkeypatch.setattr(catalog_mod, "ModelCatalog", lambda *a, **k: _EmptyCatalog())
    assert federation._model_for_harness("muse", info, "deepseek-v4-pro") is None


def test_select_harness_for_muse_model():
    manifest = {"harnesses": {"muse": {"authenticated": True, "models": ["muse-spark-1.3"]}}}
    assert federation.select_harness_for_model("muse-spark-1.3", manifest) == "muse"


def test_list_cli_models_agy_parses_and_fails_soft(monkeypatch):
    class R:
        returncode = 0
        stdout = "Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\nclaude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)\n"
    monkeypatch.setattr(federation.subprocess, "run", lambda *a, **k: R())
    rows = federation.list_cli_models("agy", "/bin/agy")
    assert rows == [("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
                    ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)")]

    def boom(*a, **k):
        raise OSError("no exec")
    monkeypatch.setattr(federation.subprocess, "run", boom)
    assert federation.list_cli_models("agy", "/bin/agy") == []


# ---------- catalog discovery fail-soft ----------

@pytest.fixture
def cat(tmp_path):
    return catalog_mod.ModelCatalog(db_path=tmp_path / "catalog.db")


def test_discovery_missing_binary_is_unavailable(monkeypatch, cat):
    monkeypatch.setattr(federation, "find_binary", lambda name: None)
    assert cat._discover_agy_cli(time.time()) == []
    assert cat._discover_muse_cli(time.time()) == []


def test_discovery_missing_auth_is_unavailable(monkeypatch, cat):
    monkeypatch.setattr(federation, "find_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(federation, "check_harness_auth", lambda spec: False)
    assert cat._discover_agy_cli(time.time()) == []
    assert cat._discover_muse_cli(time.time()) == []


def test_discovery_exception_does_not_raise(monkeypatch, cat):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(federation, "find_binary", boom)
    assert cat._discover_agy_cli(time.time()) == []


def test_muse_discovery_registers_cli_harness_provider(monkeypatch, cat):
    monkeypatch.setattr(federation, "find_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(federation, "check_harness_auth", lambda spec: True)
    models = cat._discover_muse_cli(time.time())
    assert models and all(m.provider == "muse-cli" for m in models)
    assert all(m.access_method == "cli_harness" and m.harness_binary == "muse" for m in models)
    assert all(m.billing_type == "subscription" for m in models)


def test_agy_discovery_uses_live_list(monkeypatch, cat):
    monkeypatch.setattr(federation, "find_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(federation, "check_harness_auth", lambda spec: True)
    monkeypatch.setattr(federation, "list_cli_models",
                        lambda key, bin_path=None, timeout=15.0: [("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)")])
    models = cat._discover_agy_cli(time.time())
    assert [m.id for m in models] == ["gemini-3.1-pro-high"]
    assert models[0].provider == "agy-cli" and models[0].harness_binary == "agy"


def test_refresh_loop_registers_new_sources(cat):
    import inspect
    src = inspect.getsource(catalog_mod.ModelCatalog.refresh_from_providers)
    assert "_discover_agy_cli" in src and "_discover_muse_cli" in src
