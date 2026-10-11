"""Structured, append-only routing decision log (JSONL).

Privacy: the log stores the SHA-256 of the last user message, never raw prompt
text. The one existing leak vector -- memory auto-escalation ``reason`` strings
embedding a 35-char prompt sample -- is redacted at the logging boundary by
``sanitize_reason`` (the in-memory/API reason is unchanged).

Every record carries the catalog epoch + refresh timestamp and the scoring-spec
hash, so any past decision is reproducible:

- Mode A (audit, no prompt needed): rebuild a CapabilityRequestVector from the
  logged fields and run ``solver.arbitrate`` against ``catalog.snapshot(epoch)``.
- Mode B (full replay, caller supplies the prompt): verify
  ``sha256(prompt) == record["prompt_sha256"]``, then run the full
  ``extract_vector`` + ``arbitrate`` pipeline against the epoch snapshot.

Honest non-determinism disclosure: Mode B can still diverge if, at decision
time, (a) a memory incident fired (logged via ``memory_incident_id`` -- replay
with the same DB reproduces it), (b) session-affinity KV latch applied (logged
via ``kv_latch_applied`` + ``session_id``), or (c) circuit breakers were open
(point-in-time state, intentionally not snapshotted). The catalog -- the actual
complaint behind this module -- is fully pinned.

``log_decision`` never raises: a logging failure must never 500 a routing request.
"""
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("reflex.decision_log")

DECISION_LOG_PATH = Path(os.getenv("REFLEX_DECISION_LOG", "~/.reflex/decisions.jsonl")).expanduser()
_MAX_BYTES = 64 * 1024 * 1024
_KEEP_ROTATIONS = 3

# Memory auto-escalation reasons embed a 35-char prompt sample:
#   "Memory Auto-Escalation (Incident #12, sim=0.8): learned from prior failure in '...'"
_SNIPPET_RE = re.compile(r"learned from prior failure in '.*?'")

# Fields every record must carry (None allowed only where marked).
_REQUIRED_FIELDS = (
    "decision_id", "ts", "endpoint", "session_id", "prompt_sha256",
    "tier", "effort", "model", "provider", "billing_type", "reason",
    "route_category", "catalog_epoch", "catalog_refreshed_at",
    "scoring_spec_sha256", "token_count", "latency_ms",
)


def sanitize_reason(reason: str) -> str:
    """Redact prompt fragments from reason strings before they touch disk."""
    return _SNIPPET_RE.sub("learned from prior failure in '[redacted]'", reason or "")


def prompt_sha256(prompt_text: str) -> str:
    """Canonical prompt hash: sha256 of the last user-role message's content string."""
    return hashlib.sha256((prompt_text or "").encode("utf-8")).hexdigest()


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            oldest = path.with_suffix(f".jsonl.{_KEEP_ROTATIONS}")
            if oldest.exists():
                oldest.unlink()
            for i in range(_KEEP_ROTATIONS - 1, 0, -1):
                src = path.with_suffix(f".jsonl.{i}")
                if src.exists():
                    src.rename(path.with_suffix(f".jsonl.{i + 1}"))
            path.rename(path.with_suffix(".jsonl.1"))
    except Exception as e:
        logger.warning(f"decision log rotation failed: {e}")


def log_decision(
    *,
    endpoint: str,
    session_id: str,
    prompt_text: str,
    tier: int,
    effort: str,
    model: str,
    provider: str,
    billing_type: str,
    reason: str,
    route_category: str,
    catalog_epoch: int,
    catalog_refreshed_at: float,
    spec_hash: str,
    token_count: int,
    latency_ms: float,
    kv_latch_applied: bool = False,
    memory_incident_id: Optional[int] = None,
    supersedes: Optional[str] = None,
) -> str:
    """Append one JSONL decision record. Returns decision_id. Never raises."""
    decision_id = str(uuid.uuid4())
    record = {
        "decision_id": decision_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int((time.time() % 1) * 1000):03d}Z",
        "endpoint": endpoint,
        "session_id": session_id or "",
        "prompt_sha256": prompt_sha256(prompt_text),
        "tier": tier,
        "effort": effort,
        "model": model,
        "provider": provider,
        "billing_type": billing_type,
        "reason": sanitize_reason(reason),
        "route_category": route_category,
        "catalog_epoch": catalog_epoch,
        "catalog_refreshed_at": catalog_refreshed_at,
        "scoring_spec_sha256": spec_hash,
        "token_count": token_count,
        "latency_ms": round(latency_ms, 3),
        "kv_latch_applied": bool(kv_latch_applied),
        "memory_incident_id": memory_incident_id,
        "supersedes": supersedes,
    }
    try:
        path = Path(DECISION_LOG_PATH)  # tolerate str or Path
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception as e:
        # Logging must never break routing.
        logger.warning(f"log_decision failed (non-fatal): {e}")
    return decision_id


def replay_decision(
    record: Dict[str, Any],
    catalog,
    solver,
    prompt_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Two-mode reproducibility check. Returns {'match': bool, 'detail': ...}.

    Mode A (audit, prompt_text=None): rebuild the decision from logged fields and
    re-run arbitration against the epoch snapshot.
    Mode B (full replay, prompt_text given): hash-verify the prompt, then run the
    full extract_vector + arbitrate pipeline against the epoch snapshot.
    """
    for field in _REQUIRED_FIELDS:
        if field not in record:
            return {"match": False, "detail": f"record missing required field: {field}"}

    snap = catalog.snapshot(record["catalog_epoch"])
    if not snap:
        return {"match": False, "detail": f"no catalog snapshot for epoch {record['catalog_epoch']}"}

    # Swap the solver's catalog view for the snapshot (restore afterwards).
    orig_cache = solver.catalog._memory_cache
    snap_cache = {f"{m.provider}::{m.id}": m for m in snap}
    try:
        solver.catalog._memory_cache = snap_cache
        if prompt_text is not None:
            # Mode B: hash-verify, then full pipeline.
            if prompt_sha256(prompt_text) != record["prompt_sha256"]:
                return {"match": False, "detail": "prompt hash mismatch"}
            messages = [{"role": "user", "content": prompt_text}]
            vector = solver.extract_vector(messages)
            if vector.tier_num != record["tier"] or vector.effort != record["effort"]:
                return {"match": False,
                        "detail": f"vector diverged: tier {vector.tier_num}!={record['tier']} "
                                  f"or effort {vector.effort}!={record['effort']}"}
        else:
            # Mode A: rebuild vector from logged fields via the tier profile.
            import scoring_spec
            from solver import CapabilityRequestVector
            profile = scoring_spec.get_tier_threshold_profile(record["tier"])
            vector = CapabilityRequestVector(
                token_count=record["token_count"],
                reasoning_depth=profile.get("reasoning_depth", 0.10),
                tool_calling=False,
                architecture_score=profile.get("architecture_score", 0.20),
                modality="text",
                tier_num=record["tier"],
                explanation=f"replay of {record['decision_id']}",
                effort=record["effort"],
            )
        primary, _metered, _explanation = solver.arbitrate(vector, session_id=record.get("session_id") or None)
        match = (primary["model"] == record["model"] and primary["provider"] == record["provider"])
        return {"match": match,
                "detail": f"replay -> {primary['provider']}/{primary['model']}; "
                          f"logged -> {record['provider']}/{record['model']}"}
    except Exception as e:
        return {"match": False, "detail": f"replay raised: {e}"}
    finally:
        solver.catalog._memory_cache = orig_cache
