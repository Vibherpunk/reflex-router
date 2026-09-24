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

def load_scoring_spec(force_reload: bool = False) -> Dict[str, Any]:
    """
    Loads scoring specification from ~/.reflex/scoring_spec.yaml if present,
    otherwise falls back to repo scoring_spec.yaml.
    Caches the loaded spec and re-reads on file modification.
    """
    global _CACHED_SPEC, _LAST_MTIME

    target_path = USER_SPEC_FILE if USER_SPEC_FILE.exists() else REPO_SPEC_FILE

    if not target_path.exists():
        # Fallback to empty structure if missing
        return {}

    try:
        mtime = os.path.getmtime(target_path)
        if force_reload or _CACHED_SPEC is None or mtime > _LAST_MTIME:
            with open(target_path, "r", encoding="utf-8") as f:
                _CACHED_SPEC = yaml.safe_load(f) or {}
            _LAST_MTIME = mtime
    except Exception:
        if _CACHED_SPEC is None:
            _CACHED_SPEC = {}

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
