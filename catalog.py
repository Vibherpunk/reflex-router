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
    generation: Optional[float] = None # Extracted model generation float (e.g. 3.7, 2.0)
    tier: str = "Base"             # Architectural tier (Flagship, Mid, Flash, Mini, Rsng, Base)

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


def extract_model_semantics(model_id: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Semantic Version & Feature Extractor.
    Parses arbitrary model IDs into 4 orthogonal dimensions:
    - Family (Claude, GPT, Gemini, Llama, DeepSeek, Qwen, Mistral, etc.)
    - Generation (float e.g. 3.7, 3.5, 2.0, 4.0, or None)
    - Variant / Tier (Flagship, Mid, Flash, Mini, Rsng, Base)
    - Specialization (is_reasoning, is_coder)
    Integrates upstream metadata (modality, instruct_type, description) when available.
    """
    mid = model_id.lower()
    clean_id = mid.split("/")[-1]

    # Strip timestamps and common non-generational suffixes
    clean_base = re.sub(r"[-_](20\d{6}|\d{4,8})", "", clean_id)
    clean_base = re.sub(r"(:batch|-preview|-exp.*|@\d+)", "", clean_base)

    # 1. Family detection
    family = "unknown"
    if any(k in clean_id for k in ["claude", "anthropic"]):
        family = "claude"
    elif any(k in clean_id for k in ["gemini", "gemma", "google"]):
        family = "gemini"
    elif any(k in clean_id for k in ["deepseek"]):
        family = "deepseek"
    elif any(k in clean_id for k in ["gpt", "openai", "o1", "o3", "o4"]):
        family = "openai"
    elif any(k in clean_id for k in ["llama", "meta"]):
        family = "llama"
    elif any(k in clean_id for k in ["qwen"]):
        family = "qwen"
    elif any(k in clean_id for k in ["mistral", "mixtral", "codestral"]):
        family = "mistral"
    elif any(k in clean_id for k in ["grok"]):
        family = "grok"

    # 2. Generation extraction
    gen = None
    # Skip generation parsing for pure reasoning tags like r1
    if not re.search(r"\br1\b", clean_base):
        # Match X.Y or X-Y e.g., 3.7, 3-7, 3.5, 2.5, 2.0, 1.5, 4.5, 3.3, 3.1
        m_gen = re.search(r"(?:^|[-_a-z])(?:v|version)?(\d+)[._-](\d+)", clean_base)
        if m_gen:
            try:
                val = float(f"{m_gen.group(1)}.{m_gen.group(2)}")
                if 1.0 <= val <= 10.0:
                    gen = val
            except ValueError:
                pass

        if gen is None:
            # Check single digit e.g. claude-3, gpt-4, llama-3, v4, v3, opus-5, o3, o1
            m_single = re.search(r"(?:claude|gpt|llama|deepseek|qwen|mistral|gemini|opus|sonnet|haiku|o|v)[-_]?(\d+)(?![\d])", clean_base)
            if m_single:
                try:
                    val = float(m_single.group(1))
                    if 1.0 <= val <= 10.0:
                        gen = val
                except ValueError:
                    pass

    # Upstream telemetry inspection
    upstream_text = ""
    if metadata and isinstance(metadata, dict):
        arch = metadata.get("architecture")
        arch_str = json.dumps(arch) if isinstance(arch, dict) else str(arch or "")
        upstream_text = f"{metadata.get('description', '')} {arch_str} {metadata.get('displayName', '')}".lower()

    # 3. Specialization
    is_reasoning = bool(
        re.search(r"(?:-thinking|thinking|reasoning|reasoner|\br1\b|\bo1\b|\bo3\b|\bo4\b)", clean_id)
        or re.search(r"(?:chain-of-thought|reasoning model|thinking process|deepseek-r1)", upstream_text)
    )
    is_coder = bool(
        re.search(r"(?:coder|codestral|code)", clean_id)
        or "code generation" in upstream_text
    )

    # 4. Variant / Tier (Word boundary protected to avoid "mini" in "gemini")
    is_flash = bool(re.search(r"(?:^|[-_.])(?:flash|mini|haiku|small|lite|nano|micro|8b|7b|3b|1b)(?:[-_.]|$)", clean_id))
    is_flagship = bool(re.search(r"(?:^|[-_.])(?:opus|max|ultra|large|405b|o1-pro|o3-pro)(?:[-_.]|$)", clean_id))
    is_mid = bool(re.search(r"(?:^|[-_.])(?:sonnet|pro|plus|70b|medium|gpt-4o|4o)(?:[-_.]|$)", clean_id))

    tier = "base"
    display_tier = "Base"

    if is_flagship:
        tier = "flagship"
        display_tier = "Flagship"
    elif is_flash:
        tier = "flash"
        display_tier = "Flash" if "flash" in clean_id else "Mini"
    elif is_reasoning:
        tier = "reasoning"
        display_tier = "Rsng"
    elif is_mid:
        tier = "mid"
        display_tier = "Mid"

    return {
        "family": family,
        "generation": gen,
        "tier": tier,
        "display_tier": display_tier,
        "is_reasoning": is_reasoning,
        "is_coder": is_coder
    }


def compute_scores(semantics: Dict[str, Any]) -> Dict[str, float]:
    """
    Mathematical capability matrix:
    Base_Score = Family_Baseline * Generation_Multiplier * Tier_Weight * Specialization_Bonus
    """
    family = semantics.get("family", "unknown")
    gen = semantics.get("generation")
    tier = semantics.get("tier", "base")
    is_reasoning = semantics.get("is_reasoning", False)
    is_coder = semantics.get("is_coder", False)

    # 1. Family Baselines
    family_baselines = {
        "claude":   {"r": 0.83, "a": 0.85, "c": 0.84, "s": 0.75},
        "openai":   {"r": 0.81, "a": 0.83, "c": 0.83, "s": 0.75},
        "gemini":   {"r": 0.80, "a": 0.81, "c": 0.82, "s": 0.80},
        "deepseek": {"r": 0.82, "a": 0.80, "c": 0.82, "s": 0.75},
        "llama":    {"r": 0.74, "a": 0.76, "c": 0.77, "s": 0.75},
        "qwen":     {"r": 0.74, "a": 0.76, "c": 0.78, "s": 0.75},
        "mistral":  {"r": 0.74, "a": 0.76, "c": 0.77, "s": 0.75},
        "grok":     {"r": 0.75, "a": 0.75, "c": 0.75, "s": 0.70},
        "unknown":  {"r": 0.60, "a": 0.60, "c": 0.65, "s": 0.65},
    }
    base = family_baselines.get(family, family_baselines["unknown"]).copy()

    # 2. Generation Multiplier
    gen_mult = 1.0
    if gen is not None:
        if family == "claude":
            gen_mult = 1.0 + max(-0.15, (gen - 3.0) * 0.12)
        elif family == "gemini":
            gen_mult = 1.0 + max(-0.15, (gen - 1.5) * 0.10)
        elif family == "deepseek":
            gen_mult = 1.0 + max(-0.15, (gen - 3.0) * 0.10)
        elif family == "openai":
            if gen >= 4.0:
                gen_mult = 1.0 + (gen - 4.0) * 0.10
            elif 1.0 < gen < 4.0:
                gen_mult = 0.90
        elif family in ("llama", "qwen", "mistral"):
            gen_mult = 1.0 + max(-0.15, (gen - 3.0) * 0.08)
        else:
            gen_mult = 1.0 + max(-0.15, min(0.3, (gen - 2.0) * 0.08))

    r = base["r"] * gen_mult
    a = base["a"] * gen_mult
    c = base["c"] * gen_mult
    s = base["s"]

    # 3. Tier Weight
    tier_weights = {
        "flagship": {"r": 1.10, "a": 1.12, "c": 1.08, "s": 0.70},
        "mid":      {"r": 1.04, "a": 1.06, "c": 1.06, "s": 0.95},
        "flash":    {"r": 0.85, "a": 0.82, "c": 0.90, "s": 1.25},
        "reasoning":{"r": 1.15, "a": 1.10, "c": 1.10, "s": 0.75},
        "base":     {"r": 1.00, "a": 1.00, "c": 1.00, "s": 1.00},
    }
    tw = tier_weights.get(tier, tier_weights["base"])
    r *= tw["r"]
    a *= tw["a"]
    c *= tw["c"]
    s *= tw["s"]

    # 4. Specialization Bonuses
    if is_reasoning:
        # Automatic +0.25 reasoning boost as mandated by blueprint
        r += 0.25
        a += 0.10
        c += 0.08
        s = min(s, 0.65)

    if is_coder:
        c += 0.15

    return {
        "reasoning_capability": round(min(0.99, max(0.10, r)), 2),
        "architecture_score": round(min(0.99, max(0.10, a)), 2),
        "coding_score": round(min(0.99, max(0.10, c)), 2),
        "speed_score": round(min(0.99, max(0.10, s)), 2),
    }


def score_model(model_id: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Dynamically scores models via Semantic Version & Capability Extractor,
    with opt-in explicit overrides from providers.yaml taking precedence if defined.
    """
    semantics = extract_model_semantics(model_id, metadata=metadata)
    computed = compute_scores(semantics)

    res = {
        "reasoning_capability": computed["reasoning_capability"],
        "architecture_score": computed["architecture_score"],
        "coding_score": computed["coding_score"],
        "speed_score": computed["speed_score"],
        "generation": semantics.get("generation"),
        "tier": semantics.get("display_tier", "Base"),
    }

    # Explicit opt-in overrides in providers.yaml
    for rule in _SCORING_OVERLAY:
        pattern = rule.get("match", "")
        if pattern and re.search(pattern, model_id):
            for k, v in rule.items():
                if k != "match":
                    if k in ("reasoning_capability", "architecture_score", "coding_score", "speed_score"):
                        res[k] = float(v)
                    elif k == "generation":
                        res[k] = float(v) if v is not None else None
                    elif k == "tier":
                        res[k] = str(v)
            break

    return res


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
                    generation REAL,
                    tier TEXT,
                    PRIMARY KEY (id, provider)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_billing ON models(billing_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON models(provider)")
            try:
                conn.execute("ALTER TABLE models ADD COLUMN generation REAL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE models ADD COLUMN tier TEXT")
            except sqlite3.OperationalError:
                pass

    def _load_cache(self):
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM models")
            new_cache = {}
            for row in cursor.fetchall():
                d = dict(row)
                d["tool_calling"] = bool(d["tool_calling"])
                if d.get("generation") is None or not d.get("tier") or d.get("tier") == "Base":
                    sem = extract_model_semantics(d["id"])
                    if d.get("generation") is None:
                        d["generation"] = sem.get("generation")
                    if not d.get("tier") or d.get("tier") == "Base":
                        d["tier"] = sem.get("display_tier", "Base")
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
                    :base_url, :api_key_env, :harness_binary, :generation, :tier
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

    def rescore_all(self):
        """Re-evaluates all cached models with current heuristic scoring and persists them."""
        models = self.list_all()
        updated = []
        for m in models:
            scores = score_model(m.id)
            for k, v in scores.items():
                if hasattr(m, k):
                    setattr(m, k, v)
            updated.append(m)
        self.upsert_models(updated)

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
                scores = score_model(mid, metadata=item)
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
                scores = score_model(mid, metadata=item)
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
                    scores = score_model(mid, metadata=m)
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
                scores = score_model(mid, metadata=item)

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
