"""
Configuration management for OpenCode-Go System 1 Router.
"""
import os
import json
from pathlib import Path
from typing import Dict, Any

AUTH_FILE = Path.home() / ".local/share/opencode/auth.json"

def get_opencode_go_key() -> str:
    """Retrieve API key from env or ~/.local/share/opencode/auth.json."""
    if os.environ.get("OPENCODE_GO_API_KEY"):
        return os.environ["OPENCODE_GO_API_KEY"]
    if os.environ.get("OPENCODE_API_KEY"):
        return os.environ["OPENCODE_API_KEY"]
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
    "upstream_base_url": "https://opencode.ai/zen/go/v1",
    "user_agent": "opencode/1.18.13",
    "api_key": get_opencode_go_key(),
    "kv_cache_context_threshold": 20_000,  # tokens
    "heartbeat_interval_seconds": 3.0,
    "preamble_buffer_bytes": 512,
    "model_tiers": {
        0: {
            "name": "Fast / Tool Churn",
            "primary": "deepseek-v4-flash",
            "fallback": "glm-5.3-flash",
            "reasoning": False
        },
        1: {
            "name": "General Implementation",
            "primary": "qwen3.7-plus",
            "fallback": "glm-5.3",
            "reasoning": False
        },
        2: {
            "name": "Deep Reasoning / Concurrency / Bugfix",
            "primary": "deepseek-v4-pro",
            "fallback": "qwen3.7-max",
            "reasoning": True
        },
        3: {
            "name": "Frontier Architecture / System Specs",
            "primary": "qwen3.7-max",
            "fallback": "kimi-k3",
            "reasoning": True
        }
    }
}
