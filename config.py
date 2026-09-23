"""
Configuration management for Reflex Multi-Provider System 1 Router.
Supports Subscription-First Arbitrage across OpenCode-Go, Gemini, and OpenRouter.
"""
import os
import re
import json
from pathlib import Path
from typing import Dict, Any, Optional

HOME = Path.home()
AUTH_FILE = HOME / ".local/share/opencode/auth.json"
HERMES_ENV_FILE = HOME / ".hermes/.env"

def _load_hermes_env() -> Dict[str, str]:
    env_vars = {}
    if HERMES_ENV_FILE.exists():
        try:
            for line in HERMES_ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip("'\"")
        except Exception:
            pass
    return env_vars

HERMES_ENV = _load_hermes_env()

def get_key(var_name: str, fallback_hermes_name: Optional[str] = None) -> str:
    """Retrieve key from env, hermes .env, or opencode auth.json."""
    if os.environ.get(var_name):
        return os.environ[var_name]
    if fallback_hermes_name and os.environ.get(fallback_hermes_name):
        return os.environ[fallback_hermes_name]
    if var_name in HERMES_ENV:
        return HERMES_ENV[var_name]
    if fallback_hermes_name and fallback_hermes_name in HERMES_ENV:
        return HERMES_ENV[fallback_hermes_name]
    return ""

def get_opencode_go_key() -> str:
    key = get_key("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY")
    if key:
        return key
    if AUTH_FILE.exists():
        try:
            with open(AUTH_FILE, "r") as f:
                data = json.load(f)
                return data.get("opencode-go", {}).get("key", "")
        except Exception:
            pass
    return ""

CONFIG: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8787,
    "user_agent": "opencode/1.18.13",
    "kv_cache_context_threshold": 20_000,  # tokens
    "heartbeat_interval_seconds": 3.0,
    "preamble_buffer_bytes": 512,
    "circuit_breaker": {
        "base_cooldown_seconds": 30.0,
        "max_cooldown_seconds": 300.0,
        "jitter_seconds": 5.0,
        "canary_lease_seconds": 60.0,
    },
    "providers": {
        "opencode-go": {
            "name": "OpenCode-Go",
            "type": "subscription",  # Marginal cost = $0.00
            "base_url": "https://opencode.ai/zen/go/v1",
            "api_key": get_opencode_go_key(),
            "user_agent": "opencode/1.18.13",
            "requires_session": True,
            "models": [
                "deepseek-v4-flash", "glm-5.3-flash", "qwen3.7-plus",
                "glm-5.3", "deepseek-v4-pro", "qwen3.7-max", "kimi-k3"
            ]
        },
        "gemini": {
            "name": "Google Gemini",
            "type": "subscription",  # Marginal cost = $0.00
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key": get_key("GEMINI_API_KEY"),
            "user_agent": "reflex-gateway/2.0",
            "requires_session": False,
            "models": [
                "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"
            ]
        },
        "openrouter": {
            "name": "OpenRouter",
            "type": "metered",  # Marginal cost > $0.00
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": get_key("OPENROUTER_API_KEY"),
            "user_agent": "reflex-gateway/2.0",
            "requires_session": False,
            "models": [
                "anthropic/claude-3.7-sonnet", "deepseek/deepseek-r1",
                "deepseek/deepseek-chat", "openai/gpt-4o", "openai/o3-mini"
            ]
        }
    },
    "model_tiers": {
        0: {
            "name": "Fast / Tool Churn",
            "subscription": {"provider": "opencode-go", "model": "deepseek-v4-flash"},
            "fallback_sub": {"provider": "opencode-go", "model": "glm-5.3-flash"},
            "metered": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
            "reasoning": False
        },
        1: {
            "name": "General Implementation",
            "subscription": {"provider": "opencode-go", "model": "qwen3.7-plus"},
            "fallback_sub": {"provider": "opencode-go", "model": "glm-5.3"},
            "metered": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
            "reasoning": False
        },
        2: {
            "name": "Deep Reasoning / Concurrency / Bugfix",
            "subscription": {"provider": "opencode-go", "model": "deepseek-v4-pro"},
            "fallback_sub": {"provider": "opencode-go", "model": "qwen3.7-max"},
            "metered": {"provider": "openrouter", "model": "deepseek/deepseek-r1"},
            "reasoning": True
        },
        3: {
            "name": "Frontier Architecture / System Specs",
            "subscription": {"provider": "opencode-go", "model": "qwen3.7-max"},
            "fallback_sub": {"provider": "opencode-go", "model": "kimi-k3"},
            "metered": {"provider": "openrouter", "model": "anthropic/claude-3.7-sonnet"},
            "reasoning": True
        }
    }
}
