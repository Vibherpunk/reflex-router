"""
Unit tests for ProviderCircuitBreaker (3-state FSM with canary gate).
"""
import time
import pytest
import asyncio
from circuit_breaker import ProviderCircuitBreaker, BreakerState

@pytest.mark.anyio
async def test_circuit_breaker_closed_state():
    cb = ProviderCircuitBreaker("test-sub", base_cooldown=0.1, jitter=0.0)
    assert cb.state == BreakerState.CLOSED
    assert await cb.can_attempt() is True

@pytest.mark.anyio
async def test_circuit_breaker_trips_to_open():
    cb = ProviderCircuitBreaker("test-sub", base_cooldown=0.5, jitter=0.0)
    backoff = await cb.record_failure(status_code=429)
    assert cb.state == BreakerState.OPEN
    assert cb.failure_count == 1
    assert await cb.can_attempt() is False
    assert backoff >= 0.5

@pytest.mark.anyio
async def test_circuit_breaker_half_open_single_canary():
    # Very short cooldown for testing
    cb = ProviderCircuitBreaker("test-sub", base_cooldown=0.05, max_cooldown=1.0, jitter=0.0)
    await cb.record_failure(status_code=429)
    assert await cb.can_attempt() is False

    # Wait for cooldown to expire
    await asyncio.sleep(0.06)

    # First call enters HALF_OPEN and gets canary slot
    assert await cb.can_attempt() is True
    assert cb.state == BreakerState.HALF_OPEN
    assert cb.canary_in_flight is True

    # Second concurrent call MUST be rejected (routed to metered fallback)
    assert await cb.can_attempt() is False

    # Canary succeeds
    await cb.record_success()
    assert cb.state == BreakerState.CLOSED
    assert cb.failure_count == 0
    assert cb.canary_in_flight is False

@pytest.mark.anyio
async def test_circuit_breaker_canary_lease_expiry_prevents_deadlock():
    """HALF_OPEN must not deadlock when a canary probe is never resolved.

    If the caller times out / crashes / forgets to record success or failure,
    the canary slot is held forever and the provider is locked out of the
    circuit permanently. The lease must expire and grant a replacement canary.
    """
    cb = ProviderCircuitBreaker(
        "test-sub", base_cooldown=0.05, max_cooldown=1.0, jitter=0.0,
        canary_lease_seconds=0.1
    )
    await cb.record_failure(status_code=429)
    await asyncio.sleep(0.06)

    # Cooldown expired -> canary probe granted
    assert await cb.can_attempt() is True
    assert cb.canary_in_flight is True

    # Slot held while the lease is active
    assert await cb.can_attempt() is False

    # Simulate an unresolved canary (no record_success / record_failure ever
    # arrives): after the lease expires, the breaker must recover.
    await asyncio.sleep(0.12)
    assert await cb.can_attempt() is True
    assert cb.canary_in_flight is True

    # Once the new canary resolves, normal FSM semantics apply.
    await cb.record_success()
    assert cb.state == BreakerState.CLOSED

@pytest.mark.anyio
async def test_circuit_breaker_canary_failure_exponential():
    cb = ProviderCircuitBreaker("test-sub", base_cooldown=0.05, max_cooldown=10.0, jitter=0.0)
    await cb.record_failure(status_code=429)
    await asyncio.sleep(0.06)

    # Canary probe allowed
    assert await cb.can_attempt() is True
    # Canary fails
    await cb.record_failure(status_code=429)
    assert cb.state == BreakerState.OPEN
    assert cb.failure_count == 2
    # Second cooldown should be ~0.05 * 2 = 0.1s
    status = cb.get_status()
    assert status["cooldown_remaining_seconds"] > 0.05
