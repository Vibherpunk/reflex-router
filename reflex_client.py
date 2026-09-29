"""Reference client for the Reflex Router gateway. Implements CLIENT_CONTRACT.md.

Advisory-only: this module NEVER raises on Reflex outage. On any failure
(transport error, timeout, 503, auth failure after retry budget) it returns
``RouteSuggestion(available=False, ...)`` with the caller's local default.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import httpx

logger = logging.getLogger("reflex.client")

_DEFAULT_BASE_URL = os.environ.get("REFLEX_URL", "http://127.0.0.1:8787")
_RETRY_BACKOFF_S = 0.25
_MAX_ATTEMPTS = 2  # initial + exactly one retry


@dataclass
class RouteSuggestion:
    available: bool
    tier: int
    effort: str
    model: str
    provider: str
    reason: str


def _fallback(default_model: str, cause: str) -> RouteSuggestion:
    return RouteSuggestion(
        available=False,
        tier=0,
        effort="low",
        model=default_model,
        provider="local-default",
        reason=f"reflex_unavailable: {cause}",
    )


def suggest_route(
    messages: List[Dict[str, Any]],
    model: str = "auto",
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 10.0,
    default_model: str = "local-default",
) -> RouteSuggestion:
    """Ask Reflex for a routing suggestion. NEVER raises on Reflex outage.

    Contract: POST /v1/route, connect <= 2s, read <= `timeout`,
    one retry on transport error/timeout/503 (250ms backoff),
    429 honors Retry-After once then retries exactly once,
    then fail-closed to the caller's local default.
    """
    base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    bearer = token if token is not None else os.environ.get("REFLEX_GATEWAY_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    t0 = time.perf_counter()
    attempts = 0
    last_cause = "unknown"
    retried_429 = False

    while attempts < _MAX_ATTEMPTS:
        attempts += 1
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=2.0)) as client:
                resp = client.post(
                    f"{base}/v1/route",
                    json={"messages": messages, "model": model},
                    headers=headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                sub = data.get("subscription_route") or {}
                return RouteSuggestion(
                    available=True,
                    tier=int(data.get("tier", 0)),
                    effort=str(data.get("effort", "low")),
                    model=str(sub.get("model", default_model)),
                    provider=str(sub.get("provider", "unknown")),
                    reason=str(data.get("reason", ""))[:500],
                )
            if resp.status_code == 503:
                last_cause = "503"
                if attempts < _MAX_ATTEMPTS:
                    time.sleep(_RETRY_BACKOFF_S)
                    continue
                break
            if resp.status_code == 429:
                # Honor Retry-After once, then retry exactly once; a second
                # 429 exhausts the budget and falls through to fallback.
                if not retried_429:
                    retried_429 = True
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        time.sleep(min(float(retry_after or 0), 5.0))
                    except (TypeError, ValueError):
                        pass
                    continue
                last_cause = "429"
                break
            # 4xx and other statuses: caller error or unexpected — no retry.
            last_cause = "auth" if resp.status_code in (401, 403) else f"http_{resp.status_code}"
            break
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_cause = "timeout" if isinstance(e, httpx.TimeoutException) else "refused"
            if attempts < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_S)
                continue
            break
        except Exception:
            last_cause = "error"
            break

    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    logger.warning(
        "reflex_unavailable",
        extra={"cause": last_cause, "latency_ms": latency_ms, "retries": attempts - 1},
    )
    return _fallback(default_model, last_cause)


def health_ok(base_url: Optional[str] = None) -> bool:
    """Cheap liveness probe. Never raises."""
    base = (base_url or _DEFAULT_BASE_URL).rstrip("/")
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            return client.get(f"{base}/health").status_code == 200
    except Exception:
        return False
