"""
Declarative Scoring and Arbitration Specification Loader.
Eliminates hardcoded math, magic numbers, and embedded heuristics across Reflex.
Supports user override via ~/.reflex/scoring_spec.yaml.
"""
import os
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
import yaml

REFLEX_DIR = Path.home() / ".reflex"
USER_SPEC_FILE = REFLEX_DIR / "scoring_spec.yaml"
REPO_SPEC_FILE = Path(__file__).parent / "scoring_spec.yaml"

_CACHED_SPEC: Optional[Dict[str, Any]] = None
_LAST_MTIME: float = 0.0

def _deep_merge(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for k, v in overlay.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def load_scoring_spec(force_reload: bool = False) -> Dict[str, Any]:
    """
    Loads base scoring specification from repo scoring_spec.yaml and overlays
    user overrides from ~/.reflex/scoring_spec.yaml if present.
    Caches the loaded spec and re-reads on file modification.
    """
    global _CACHED_SPEC, _LAST_MTIME

    repo_mtime = os.path.getmtime(REPO_SPEC_FILE) if REPO_SPEC_FILE.exists() else 0.0
    user_mtime = os.path.getmtime(USER_SPEC_FILE) if USER_SPEC_FILE.exists() else 0.0
    max_mtime = max(repo_mtime, user_mtime)

    if force_reload or _CACHED_SPEC is None or max_mtime > _LAST_MTIME:
        base_spec = {}
        if REPO_SPEC_FILE.exists():
            try:
                with open(REPO_SPEC_FILE, "r", encoding="utf-8") as f:
                    base_spec = yaml.safe_load(f) or {}
            except Exception:
                base_spec = {}

        if USER_SPEC_FILE.exists():
            try:
                with open(USER_SPEC_FILE, "r", encoding="utf-8") as f:
                    user_spec = yaml.safe_load(f) or {}
                    base_spec = _deep_merge(base_spec, user_spec)
            except Exception:
                pass

        _CACHED_SPEC = base_spec
        _LAST_MTIME = max_mtime

    return _CACHED_SPEC or {}


def get_clamp_bounds() -> Tuple[float, float]:
    spec = load_scoring_spec().get("scoring", {}).get("clamp", {})
    return float(spec.get("min", 0.10)), float(spec.get("max", 0.99))


def get_default_scores() -> Dict[str, float]:
    spec = load_scoring_spec().get("scoring", {}).get("default", {})
    return {
        "reasoning_capability": float(spec.get("reasoning_capability", 0.50)),
        "architecture_score": float(spec.get("architecture_score", 0.50)),
        "coding_score": float(spec.get("coding_score", 0.60)),
        "speed_score": float(spec.get("speed_score", 0.60)),
    }


def get_family_config(family: str) -> Dict[str, Any]:
    families = load_scoring_spec().get("scoring", {}).get("families", {})
    if family in families:
        return families[family]
    return families.get("unknown", {
        "baseline": {"reasoning": 0.60, "architecture": 0.60, "coding": 0.65, "speed": 0.65},
        "anchor_generation": 1.0,
        "growth_rate": 0.35,
        "max_bonus": 0.10,
        "min_delta": -1.0
    })


def get_tier_deltas(tier: str) -> Dict[str, float]:
    tiers = load_scoring_spec().get("scoring", {}).get("tier_deltas", {})
    data = tiers.get(tier, tiers.get("base", {}))
    return {
        "reasoning": float(data.get("reasoning", 0.0)),
        "architecture": float(data.get("architecture", 0.0)),
        "coding": float(data.get("coding", 0.0)),
        "speed": float(data.get("speed", 0.0)),
    }


def get_specialization_bonuses() -> Dict[str, Any]:
    return load_scoring_spec().get("scoring", {}).get("specialization_bonuses", {
        "reasoning": {"reasoning_boost": 0.25, "architecture_boost": 0.08, "coding_boost": 0.06, "max_speed": 0.65},
        "coder": {"coding_boost": 0.15}
    })


def get_arbitration_weights(tier: int) -> Dict[str, float]:
    tier_key = f"tier_{tier}"
    weights = load_scoring_spec().get("arbitration", {}).get("tier_fitness_weights", {}).get(tier_key, {})
    return {k: float(v) for k, v in weights.items()}


def get_cost_penalty_divisor() -> float:
    return float(load_scoring_spec().get("arbitration", {}).get("cost_penalty_divisor", 200.0))


def get_fitness_override_threshold() -> float:
    return float(load_scoring_spec().get("arbitration", {}).get("fitness_override_threshold", 0.20))


def get_domain_override_threshold() -> float:
    return float(load_scoring_spec().get("arbitration", {}).get("domain_override_threshold", 0.0))


def get_tier_threshold_profile(tier: int) -> Dict[str, float]:
    tier_key = f"tier_{tier}"
    profiles = load_scoring_spec().get("arbitration", {}).get("tier_threshold_profiles", {})
    data = profiles.get(tier_key, profiles.get("tier_0", {"reasoning_depth": 0.10, "architecture_score": 0.20}))
    return {k: float(v) for k, v in data.items()}


def get_memory_escalation_defaults(tier: int) -> Dict[str, float]:
    tier_key = f"tier_{tier}"
    defaults = load_scoring_spec().get("arbitration", {}).get("memory_escalation_defaults", {})
    data = defaults.get(tier_key, {"reasoning_depth": 0.60, "architecture_score": 0.70})
    return {k: float(v) for k, v in data.items()}


def get_classification_param(param_name: str, fallback: Any) -> Any:
    return load_scoring_spec().get("classification", {}).get(param_name, fallback)


def get_domain_specializations() -> Dict[str, Any]:
    return load_scoring_spec().get("domain_specializations", {})


def get_domain_config(domain: str) -> Dict[str, Any]:
    return get_domain_specializations().get(domain, {})


def get_domain_boost(domain: str) -> float:
    return float(get_domain_config(domain).get("boost", 0.0))

