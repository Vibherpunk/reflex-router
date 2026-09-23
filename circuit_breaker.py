"""
3-State Resilient Circuit Breaker with Single-Canary Gate and Jittered Exponential Backoff.
Prevents Thundering Herd and Flapping Oscillations across Subscriptions and Metered Providers.
"""
import time
import random
import asyncio
import logging
from enum import Enum
from typing import Dict, Any, Optional

logger = logging.getLogger("reflex.circuit_breaker")

class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class ProviderCircuitBreaker:
    def __init__(
        self,
        name: str,
        base_cooldown: float = 30.0,
        max_cooldown: float = 300.0,
        jitter: float = 5.0,
        canary_lease_seconds: float = 60.0
    ):
        self.name = name
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self.jitter = jitter
        self.canary_lease_seconds = canary_lease_seconds

        self.state = BreakerState.CLOSED
        self.failure_count = 0
        self.cooldown_until = 0.0
        self.canary_in_flight = False
        self.canary_granted_at = 0.0
        self._lock = asyncio.Lock()

    async def can_attempt(self) -> bool:
        """
        Determines if a request can be routed to this provider.
        In HALF_OPEN, allows exactly ONE single canary probe request.
        """
        async with self._lock:
            now = time.time()
            if self.state == BreakerState.CLOSED:
                return True

            if self.state == BreakerState.OPEN:
                if now >= self.cooldown_until:
                    self.state = BreakerState.HALF_OPEN
                    self.canary_in_flight = True
                    self.canary_granted_at = now
                    logger.info(f"[{self.name}] Cooldown expired. Entering HALF_OPEN. Dispatched canary probe.")
                    return True
                return False

            if self.state == BreakerState.HALF_OPEN:
                # In HALF_OPEN, only allow exactly 1 canary probe in flight.
                if self.canary_in_flight:
                    # Liveness guard: if the in-flight canary's lease has expired
                    # (caller timed out, crashed, or never recorded success/failure),
                    # the slot must be released so the provider is not deadlocked
                    # out of the circuit forever.
                    if now - self.canary_granted_at < self.canary_lease_seconds:
                        return False
                    logger.warning(
                        f"[{self.name}] Canary lease expired after "
                        f"{self.canary_lease_seconds:.0f}s without a recorded outcome. "
                        "Granting replacement canary probe."
                    )
                self.canary_in_flight = True
                self.canary_granted_at = now
                return True

            return False

    async def record_success(self) -> None:
        """Resets the circuit breaker upon confirmed operational health."""
        async with self._lock:
            if self.state != BreakerState.CLOSED:
                logger.info(f"[{self.name}] Canary probe succeeded. Resetting circuit breaker to CLOSED.")
            self.state = BreakerState.CLOSED
            self.failure_count = 0
            self.canary_in_flight = False
            self.canary_granted_at = 0.0

    async def record_failure(self, status_code: int, retry_after: Optional[float] = None) -> float:
        """Trips breaker with exponential backoff and randomized jitter."""
        async with self._lock:
            self.failure_count += 1
            if retry_after and retry_after > 0:
                backoff = retry_after
            else:
                backoff = min(
                    self.max_cooldown,
                    self.base_cooldown * (2 ** (self.failure_count - 1))
                ) + random.uniform(0, self.jitter)

            self.cooldown_until = time.time() + backoff
            self.state = BreakerState.OPEN
            self.canary_in_flight = False
            self.canary_granted_at = 0.0
            logger.warning(
                f"[{self.name}] Trip failure (status={status_code}, count={self.failure_count}). "
                f"Entering OPEN state for {backoff:.2f}s."
            )
            return backoff

    def get_status(self) -> Dict[str, Any]:
        """Returns diagnostic telemetry for this breaker."""
        now = time.time()
        remaining = max(0.0, self.cooldown_until - now)
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "cooldown_remaining_seconds": round(remaining, 2),
            "canary_in_flight": self.canary_in_flight
        }
