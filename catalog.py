"""
Dynamic Model Catalog Subsystem for Reflex.
Discovers, normalizes, and caches available models across all installed CLI harnesses
and API endpoints without hardcoded model tables.
"""
import os
import re
import json
import sqlite3
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional
import yaml

from config import get_key, get_opencode_go_key
from circuit_breaker import BREAKER_REGISTRY, ProviderCircuitBreaker, BreakerState, ensure_breaker

REFLEX_DIR = Path.home() / ".reflex"
CATALOG_DB = REFLEX_DIR / "catalog.db"
CONFIG_PROVIDERS_FILE = REFLEX_DIR / "providers.yaml"
FALLBACK_PROVIDERS_FILE = Path(__file__).parent / "providers.yaml"

@dataclass
class ReflexModelDefinition:
    id: str                        # Full model identifier (e.g. "anthropic/claude-sonnet-5", "deepseek-v4-pro")
    display_name: str              # User-friendly display name
    provider: str                  # e.g. "opencode-go", "claude-cli", "gemini", "openrouter"
    access_method: str             # "http_gateway" or "cli_harness"
    billing_type: str              # "subscription" ($0 marginal) or "metered" (> $0)
    context_window: int            # Max input tokens (e.g. 128_000, 200_000, 1_000_000)
    max_output_tokens: int         # Max output generation tokens
    reasoning_capability: float    # 0.0 to 1.0 (support for thinking/reasoning traces)
    architecture_score: float      # 0.0 to 1.0 (suitability for system design/RFCs)
    coding_score: float            # 0.0 to 1.0 (code generation & debugging)
    speed_score: float             # 0.0 to 1.0 (latency/throughput)
    tool_calling: bool             # Supports function calling / tools
    input_cost_per_m: float        # Cost per 1M input tokens in USD
    output_cost_per_m: float       # Cost per 1M output tokens in USD
    last_updated: float            # Unix timestamp
    base_url: Optional[str] = None # Upstream HTTP URL for http_gateway
    api_key_env: Optional[str] = None # Environment variable holding authentication key
    harness_binary: Optional[str] = None # Harness name for cli_harness dispatch (e.g. "claude")

_SCORING_OVERLAY: List[Dict[str, Any]] = []
_SCORING_DEFAULT: Dict[str, float] = {
    "reasoning_capability": 0.50,
    "architecture_score": 0.50,
    "coding_score": 0.60,
    "speed_score": 0.60,
}

def load_scoring_overlay() -> None:
    global _SCORING_OVERLAY, _SCORING_DEFAULT
    cfg_file = CONFIG_PROVIDERS_FILE if CONFIG_PROVIDERS_FILE.exists() else FALLBACK_PROVIDERS_FILE
    if not cfg_file.exists():
        return
    try:
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        _SCORING_OVERLAY = data.get("scoring_overrides", [])
        _SCORING_DEFAULT.update(data.get("default", {}))
    except Exception:
        pass

def score_model(model_id: str) -> Dict[str, float]:
    """Dynamically applies subjective capability scores via pattern matching overlay."""
    for rule in _SCORING_OVERLAY:
        pattern = rule.get("match", "")
        if pattern and re.search(pattern, model_id):
            res = dict(_SCORING_DEFAULT)
            for k, v in rule.items():
                if k != "match":
                    res[k] = float(v)
            return res
    return dict(_SCORING_DEFAULT)


class ModelCatalog:
    def __init__(self, db_path: Path = CATALOG_DB):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        load_scoring_overlay()
        self._init_db()
        self._memory_cache: Dict[str, ReflexModelDefinition] = {}
        self._per_provider_refresh: Dict[str, float] = {}
        self._load_cache()
        self._refresh_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT,
                    provider TEXT,
                    display_name TEXT,
                    access_method TEXT,
                    billing_type TEXT,
                    context_window INTEGER,
                    max_output_tokens INTEGER,
                    reasoning_capability REAL,
                    architecture_score REAL,
                    coding_score REAL,
                    speed_score REAL,
                    tool_calling INTEGER,
                    input_cost_per_m REAL,
                    output_cost_per_m REAL,
                    last_updated REAL,
                    base_url TEXT,
                    api_key_env TEXT,
                    harness_binary TEXT,
                    PRIMARY KEY (id, provider)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_billing ON models(billing_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON models(provider)")

    def _load_cache(self):
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM models")
            new_cache = {}
            for row in cursor.fetchall():
                d = dict(row)
                d["tool_calling"] = bool(d["tool_calling"])
                key = f"{d['provider']}::{d['id']}"
                new_cache[key] = ReflexModelDefinition(**d)
                ensure_breaker(d["provider"])
            self._memory_cache = new_cache

    def list_all(self) -> List[ReflexModelDefinition]:
        return list(self._memory_cache.values())

    def get(self, provider: str, model_id: str) -> Optional[ReflexModelDefinition]:
        key = f"{provider}::{model_id}"
        if key in self._memory_cache:
            return self._memory_cache[key]
        for m in self._memory_cache.values():
            if m.id == model_id and m.provider == provider:
                return m
        return None

    def get_by_id(self, model_id: str) -> Optional[ReflexModelDefinition]:
        for m in self._memory_cache.values():
            if m.id == model_id:
                return m
        return None

    def upsert_models(self, models: List[ReflexModelDefinition]):
        if not models:
            return
        with self._connect() as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO models VALUES (
                    :id, :provider, :display_name, :access_method, :billing_type,
                    :context_window, :max_output_tokens, :reasoning_capability,
                    :architecture_score, :coding_score, :speed_score,
                    :tool_calling, :input_cost_per_m, :output_cost_per_m, :last_updated,
                    :base_url, :api_key_env, :harness_binary
                )
            """, [{**asdict(m), "tool_calling": int(m.tool_calling)} for m in models])

        # Atomic dictionary swap to eliminate torn reads across threads
        new_cache = dict(self._memory_cache)
        now = time.time()
        for m in models:
            new_cache[f"{m.provider}::{m.id}"] = m
            self._per_provider_refresh[m.provider] = now
            ensure_breaker(m.provider)
        self._memory_cache = new_cache

    def refresh_from_providers(self, force: bool = False):
        """Discovers models from all available live CLI harnesses and APIs."""
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            now = time.time()
            discovered: List[ReflexModelDefinition] = []

            for source, ttl in (
                (self._discover_opencode_go, 3600),
                (self._discover_gemini_models, 3600),
                (self._discover_claude_cli, 3600),
                (self._discover_openrouter_models, 3600),
            ):
                key = source.__name__
                last = self._per_provider_refresh.get(key, 0)
                if force or (now - last) >= ttl:
                    try:
                        batch = source(now)
                        if batch:
                            discovered.extend(batch)
                            self._per_provider_refresh[key] = now
                    except Exception:
                        pass

            if discovered:
                self.upsert_models(discovered)
        finally:
            self._refresh_lock.release()

    def _discover_opencode_go(self, timestamp: float) -> List[ReflexModelDefinition]:
        """Live GET /models against the OpenCode-Go gateway (Subscription $0.00)."""
        key = get_opencode_go_key()
        if not key:
            return []
        try:
            req = urllib.request.Request(
                "https://opencode.ai/zen/go/v1/models",
                headers={"Authorization": f"Bearer {key}", "User-Agent": "opencode/1.18.13"}
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("data", []):
                mid = item.get("id")
                if not mid:
                    continue
                scores = score_model(mid)
                results.append(ReflexModelDefinition(
                    id=mid,
                    display_name=item.get("name", mid),
                    provider="opencode-go",
                    access_method="http_gateway",
                    billing_type="subscription",
                    context_window=int(item.get("context_length") or 128_000),
                    max_output_tokens=int(item.get("max_output_tokens") or 16_384),
                    tool_calling=True,
                    input_cost_per_m=0.0,
                    output_cost_per_m=0.0,
                    last_updated=timestamp,
                    base_url="https://opencode.ai/zen/go/v1",
                    api_key_env="OPENCODE_GO_API_KEY",
                    **scores
                ))
            return results
        except Exception:
            return []

    def _discover_gemini_models(self, timestamp: float) -> List[ReflexModelDefinition]:
        """Live GET /models against Google Gemini API (Subscription $0.00)."""
        key = get_key("GEMINI_API_KEY")
        if not key:
            return []
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            req = urllib.request.Request(url, headers={"User-Agent": "reflex-gateway/2.0"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("models", []):
                raw_name = item.get("name", "")
                mid = raw_name.replace("models/", "")
                # Only include generative text/chat models
                methods = item.get("supportedGenerationMethods", [])
                if "generateContent" not in methods:
                    continue
                scores = score_model(mid)
                results.append(ReflexModelDefinition(
                    id=mid,
                    display_name=item.get("displayName", mid),
                    provider="gemini",
                    access_method="http_gateway",
                    billing_type="subscription",
                    context_window=int(item.get("inputTokenLimit") or 1_048_576),
                    max_output_tokens=int(item.get("outputTokenLimit") or 65_536),
                    tool_calling=True,
                    input_cost_per_m=0.0,
                    output_cost_per_m=0.0,
                    last_updated=timestamp,
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    api_key_env="GEMINI_API_KEY",
                    **scores
                ))
            return results
        except Exception:
            return []

    def _discover_claude_cli(self, timestamp: float) -> List[ReflexModelDefinition]:
        """Live discovery from local Claude Code cached published catalog (Subscription $0.00)."""
        results = []
        claude_bin = Path.home() / ".local/bin/claude"
        if not (claude_bin.exists() or shutil.which("claude")):
            return []

        catalog_dir = Path.home() / ".claude/cache/model-catalog"
        if not catalog_dir.exists():
            return []

        # Find newest published catalog json file (excluding floor manifest)
        pub_files = [f for f in catalog_dir.glob("published-*.json") if f.name != "published-floor.json"]
        if not pub_files:
            pub_files = list(catalog_dir.glob("published-*.json"))
        if not pub_files:
            return []
        pub_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        try:
            data = json.loads(pub_files[0].read_text(encoding="utf-8"))
            surfaces = data.get("document", {}).get("surfaces", {})
            cc_config = surfaces.get("cc", {}).get("model_selector_config", [])
            seen_ids = set()
            for cfg in cc_config:
                for m in cfg.get("models", []):
                    mid = m.get("id")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    scores = score_model(mid)
                    ctx = int(m.get("hard_limit") or 200_000)
                    results.append(ReflexModelDefinition(
                        id=mid,
                        display_name=m.get("name", mid),
                        provider="claude-cli",
                        access_method="cli_harness",
                        billing_type="subscription",
                        context_window=ctx,
                        max_output_tokens=16_384,
                        tool_calling=True,
                        input_cost_per_m=0.0,
                        output_cost_per_m=0.0,
                        last_updated=timestamp,
                        harness_binary="claude",
                        **scores
                    ))
        except Exception:
            pass
        return results

    def _discover_openrouter_models(self, timestamp: float) -> List[ReflexModelDefinition]:
        """Live GET /api/v1/models against OpenRouter (Metered Fallback)."""
        key = get_key("OPENROUTER_API_KEY")
        if not key:
            return []
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}", "User-Agent": "Reflex-Gateway/2.0"}
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("data", []):
                mid = item.get("id")
                if not mid:
                    continue
                pricing = item.get("pricing", {})
                inp = float(pricing.get("prompt", 0.0)) * 1_000_000
                outp = float(pricing.get("completion", 0.0)) * 1_000_000
                ctx = int(item.get("context_length") or 128_000)
                scores = score_model(mid)

                results.append(ReflexModelDefinition(
                    id=mid,
                    display_name=item.get("name", mid),
                    provider="openrouter",
                    access_method="http_gateway",
                    billing_type="metered",
                    context_window=ctx,
                    max_output_tokens=int(item.get("top_provider", {}).get("max_completion_tokens") or 8_192),
                    tool_calling=True,
                    input_cost_per_m=inp,
                    output_cost_per_m=outp,
                    last_updated=timestamp,
                    base_url="https://openrouter.ai/api/v1",
                    api_key_env="OPENROUTER_API_KEY",
                    **scores
                ))
            return results
        except Exception:
            return []
