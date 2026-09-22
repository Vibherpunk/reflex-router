"""
Reflex Multi-Provider Subscription-First Gateway (v2.0.0).
Implements Zero-Byte Speculative Pre-Flight Failover, 3-State Canary Circuit Breaker,
SSE Heartbeat Generator, Skidnir Recursive Incident Memory, and KV-Cache Preservation.
"""
import os
import json
import time
import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, AsyncGenerator, Tuple, Optional

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from config import CONFIG
from classifier import classify_request
from memory import record_incident, get_stats, log_request
from circuit_breaker import ProviderCircuitBreaker, BreakerState

logger = logging.getLogger("reflex.server")
logging.basicConfig(level=logging.INFO)

# In-memory session tracking for KV-cache protection
SESSION_AFFINITY: Dict[str, Dict[str, Any]] = {}

# Initialize circuit breakers for subscription providers
BREAKER_REGISTRY: Dict[str, ProviderCircuitBreaker] = {
    name: ProviderCircuitBreaker(
        name=name,
        base_cooldown=CONFIG["circuit_breaker"]["base_cooldown_seconds"],
        max_cooldown=CONFIG["circuit_breaker"]["max_cooldown_seconds"],
        jitter=CONFIG["circuit_breaker"]["jitter_seconds"]
    )
    for name, p in CONFIG["providers"].items()
    if p["type"] == "subscription"
}

class GatewayPool:
    client: Optional[httpx.AsyncClient] = None

gateway_pool = GatewayPool()

@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=300, keepalive_expiry=60.0)
    gateway_pool.client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(120.0, connect=15.0))
    logger.info("Reflex Gateway HTTP connection pool initialized.")
    yield
    if gateway_pool.client:
        await gateway_pool.client.aclose()
    logger.info("Reflex Gateway HTTP connection pool safely closed.")

app = FastAPI(
    title="Reflex Multi-Provider Intelligent Gateway",
    version="2.0.0",
    lifespan=lifespan
)

def get_session_id(payload: Dict[str, Any], headers: Any) -> str:
    """Derives a stable session ID from headers or message prefix."""
    if "x-opencode-session" in headers:
        return headers["x-opencode-session"]

    messages = payload.get("messages", [])
    if messages:
        first_content = str(messages[0].get("content", ""))[:200]
        return "sess-" + hashlib.sha256(first_content.encode()).hexdigest()[:16]
    return "sess-default-stream"

def estimate_tokens(payload: Dict[str, Any]) -> int:
    """Fast character-based token estimator (~3.8 chars/token)."""
    total_chars = sum(len(str(m.get("content", ""))) for m in payload.get("messages", []))
    return int((total_chars / 3.8) * 1.15)

def normalize_error(status_code: int, raw_body: bytes) -> Dict[str, Any]:
    """Ensures raw provider errors (including Cloudflare HTML proxies) format to OpenAI JSON."""
    try:
        data = json.loads(raw_body.decode())
        if "error" in data:
            return data
        return {"error": {"message": str(data), "code": status_code, "type": "upstream_error"}}
    except Exception:
        text = raw_body.decode(errors="replace")[:300].strip()
        return {"error": {"message": f"Upstream error (HTTP {status_code}): {text}", "code": status_code, "type": "upstream_error"}}

def build_upstream_headers(provider_name: str, payload: Dict[str, Any], session_id: str) -> Dict[str, str]:
    prov = CONFIG["providers"][provider_name]
    headers = {
        "Authorization": f"Bearer {prov['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": prov.get("user_agent", "reflex-gateway/2.0"),
        "Accept": "text/event-stream" if payload.get("stream") else "application/json"
    }
    if prov.get("requires_session"):
        headers["x-opencode-session"] = session_id
    if provider_name == "openrouter":
        headers["HTTP-Referer"] = "http://127.0.0.1:8787"
        headers["X-Title"] = "Reflex-Gateway"
    return headers

def select_candidate_routes(payload: Dict[str, Any], session_id: str) -> Tuple[Dict[str, Any], Dict[str, Any], int, str]:
    """
    Selects primary subscription route and metered fallback based on classification,
    explicit model requested, and KV-cache affinity.
    Returns (subscription_route, metered_route, tier_num, explanation).
    """
    tokens = estimate_tokens(payload)
    tier_num, reason = classify_request(payload.get("messages", []))
    tier_info = CONFIG["model_tiers"][tier_num]

    requested_model = payload.get("model", "auto")

    # Invariant 1: Capability Match
    # If the user/harness explicitly demands a frontier metered model not on subscription
    if "claude-3.7" in requested_model or "gpt-4o" in requested_model or "o3-mini" in requested_model:
        forced_metered = {"provider": "openrouter", "model": requested_model}
        return forced_metered, forced_metered, tier_num, f"Capability match for metered model {requested_model}"

    # Invariant 4: KV-Cache Latch
    if tokens > CONFIG["kv_cache_context_threshold"] and session_id in SESSION_AFFINITY:
        aff = SESSION_AFFINITY[session_id]
        locked_sub = aff.get("subscription_route", tier_info["subscription"])
        locked_metered = aff.get("metered_route", tier_info["metered"])
        return locked_sub, locked_metered, tier_num, f"KV-Cache Latch locked to {locked_sub['model']} ({tokens} tokens)"

    sub_route = tier_info["subscription"]
    metered_route = tier_info["metered"]

    SESSION_AFFINITY[session_id] = {
        "subscription_route": sub_route,
        "metered_route": metered_route,
        "last_seen": time.time()
    }

    return sub_route, metered_route, tier_num, f"Tier {tier_num} ({tier_info['name']}): {reason}"

@app.get("/health")
@app.get("/")
async def health_check():
    providers_summary = {}
    for name, p in CONFIG["providers"].items():
        breaker = BREAKER_REGISTRY.get(name)
        providers_summary[name] = {
            "name": p["name"],
            "type": p["type"],
            "configured": bool(p["api_key"]),
            "circuit_breaker": breaker.get_status() if breaker else "N/A"
        }
    return {
        "status": "active",
        "service": "Reflex Multi-Provider Intelligent Gateway",
        "version": "2.0.0",
        "providers": providers_summary,
        "tiers": CONFIG["model_tiers"]
    }

@app.get("/v1/models")
async def list_models():
    """Returns OpenAI-compatible model listing."""
    models_list = [
        {"id": "auto", "object": "model", "owned_by": "reflex-router", "permission": []}
    ]
    for p_name, prov in CONFIG["providers"].items():
        for m in prov["models"]:
            models_list.append({"id": m, "object": "model", "owned_by": p_name})
    return {"object": "list", "data": models_list}

@app.post("/v1/feedback")
async def submit_feedback(request: Request):
    data = await request.json()
    prompt = str(data.get("prompt", "")).strip()
    failed_tier = int(data.get("failed_tier", 0))
    escalated_tier = int(data.get("escalated_tier", 2))
    error = str(data.get("error", "Manual or automated feedback override"))
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required for feedback learning")

    inc_id = record_incident(
        prompt=prompt,
        failed_tier=failed_tier,
        escalated_tier=escalated_tier,
        error_signature=error
    )
    return {"status": "recorded", "incident_id": inc_id, "escalated_tier": escalated_tier}

@app.get("/v1/stats")
async def get_router_stats():
    stats = get_stats()
    stats["circuit_breakers"] = {name: b.get_status() for name, b in BREAKER_REGISTRY.items()}
    return stats

async def preflight_and_stream(
    sub_route: Dict[str, Any],
    metered_route: Dict[str, Any],
    payload: Dict[str, Any],
    session_id: str,
    request: Request
) -> Tuple[AsyncGenerator[bytes, None], str, str, str]:
    """
    Executes true Zero-Byte Speculative Pre-Flight:
    Verifies upstream status code AND first chunk BEFORE committing downstream headers.
    """
    client = gateway_pool.client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    sub_prov_name = sub_route["provider"]
    metered_prov_name = metered_route["provider"]
    sub_breaker = BREAKER_REGISTRY.get(sub_prov_name)

    # 1. Evaluate Circuit Breaker for subscription
    can_try_sub = await sub_breaker.can_attempt() if sub_breaker else False
    if can_try_sub and CONFIG["providers"][sub_prov_name]["api_key"]:
        target_route = sub_route
        fallback_route = metered_route
        route_category = "subscription_zero_marginal_cost"
    else:
        target_route = metered_route
        fallback_route = None
        route_category = "metered_circuit_open_or_unconfigured"

    active_payload = dict(payload)
    active_payload["model"] = target_route["model"]
    prov_name = target_route["provider"]
    upstream_url = f"{CONFIG['providers'][prov_name]['base_url']}/chat/completions"
    headers = build_upstream_headers(prov_name, active_payload, session_id)

    upstream_ctx = client.stream("POST", upstream_url, headers=headers, json=active_payload)
    stream_response = await upstream_ctx.__aenter__()

    # 2. Speculative Failover Trigger (Zero bytes committed to client so far)
    if stream_response.status_code in (429, 502, 503, 504) and fallback_route:
        err_bytes = await stream_response.aread()
        await upstream_ctx.__aexit__(None, None, None)

        retry_after = None
        if "retry-after" in stream_response.headers:
            try:
                retry_after = float(stream_response.headers["retry-after"])
            except ValueError:
                pass

        if sub_breaker:
            await sub_breaker.record_failure(stream_response.status_code, retry_after)

        logger.warning(
            f"Subscription {sub_prov_name} returned HTTP {stream_response.status_code}. "
            f"Failing over cleanly to metered {metered_prov_name}."
        )

        # Switch to Metered
        target_route = fallback_route
        route_category = "failover_after_subscription_rate_limit"
        active_payload["model"] = target_route["model"]
        prov_name = target_route["provider"]
        upstream_url = f"{CONFIG['providers'][prov_name]['base_url']}/chat/completions"
        headers = build_upstream_headers(prov_name, active_payload, session_id)

        upstream_ctx = client.stream("POST", upstream_url, headers=headers, json=active_payload)
        stream_response = await upstream_ctx.__aenter__()

    # If status code >= 400 even on fallback, abort before committing stream
    if stream_response.status_code >= 400:
        err_bytes = await stream_response.aread()
        await upstream_ctx.__aexit__(None, None, None)
        err_obj = normalize_error(stream_response.status_code, err_bytes)
        raise HTTPException(status_code=stream_response.status_code, detail=err_obj["error"])

    # If subscription succeeded, confirm breaker health
    if target_route["provider"] == sub_prov_name and sub_breaker:
        await sub_breaker.record_success()

    # 3. Read First Chunk to Guarantee Stream Validity
    aiter = stream_response.aiter_bytes().__aiter__()
    try:
        first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=20.0)
    except (asyncio.TimeoutError, StopAsyncIteration):
        await upstream_ctx.__aexit__(None, None, None)
        raise HTTPException(status_code=504, detail="Upstream gateway timed out waiting for initial chunk.")

    # 4. Stream Generator (Headers commit ONLY after this point)
    async def stream_generator() -> AsyncGenerator[bytes, None]:
        nonlocal upstream_ctx
        try:
            yield first_chunk
            last_chunk_time = time.time()

            while True:
                if await request.is_disconnected():
                    logger.info("Client disconnected. Aborting upstream stream context.")
                    break

                try:
                    chunk = await asyncio.wait_for(
                        aiter.__anext__(),
                        timeout=CONFIG["heartbeat_interval_seconds"]
                    )
                    yield chunk
                    last_chunk_time = time.time()
                except asyncio.TimeoutError:
                    # Non-breaking SSE heartbeat comment
                    yield b": ping\n\n"
                except StopAsyncIteration:
                    break

        except Exception as e:
            logger.error(f"Mid-stream protocol exception: {str(e)}")
            yield f"event: error\ndata: {json.dumps({'error': {'message': 'Stream aborted mid-flight', 'code': 502}})}\n\n".encode()
        finally:
            await upstream_ctx.__aexit__(None, None, None)

    return stream_generator, target_route["model"], target_route["provider"], route_category

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    session_id = get_session_id(payload, request.headers)

    sub_route, metered_route, tier_num, explanation = select_candidate_routes(payload, session_id)
    try:
        log_request(tier_num)
    except Exception:
        pass

    is_stream = payload.get("stream", False)

    if is_stream:
        generator_func, model_used, provider_used, category = await preflight_and_stream(
            sub_route=sub_route,
            metered_route=metered_route,
            payload=payload,
            session_id=session_id,
            request=request
        )
        return StreamingResponse(
            generator_func(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Selected-Model": model_used,
                "X-Selected-Provider": provider_used,
                "X-Routing-Category": category,
                "X-Routing-Reason": explanation
            }
        )
    else:
        # Non-streaming implementation with pre-flight failover
        client = gateway_pool.client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
        sub_prov_name = sub_route["provider"]
        sub_breaker = BREAKER_REGISTRY.get(sub_prov_name)
        can_try_sub = await sub_breaker.can_attempt() if sub_breaker else False

        target = sub_route if can_try_sub and CONFIG["providers"][sub_prov_name]["api_key"] else metered_route
        active_payload = dict(payload)
        active_payload["model"] = target["model"]
        p_name = target["provider"]
        upstream_url = f"{CONFIG['providers'][p_name]['base_url']}/chat/completions"
        headers = build_upstream_headers(p_name, active_payload, session_id)

        try:
            resp = await client.post(upstream_url, headers=headers, json=active_payload)
            if resp.status_code in (429, 502, 503) and target["provider"] == sub_prov_name:
                if sub_breaker:
                    await sub_breaker.record_failure(resp.status_code)
                target = metered_route
                active_payload["model"] = target["model"]
                p_name = target["provider"]
                upstream_url = f"{CONFIG['providers'][p_name]['base_url']}/chat/completions"
                headers = build_upstream_headers(p_name, active_payload, session_id)
                resp = await client.post(upstream_url, headers=headers, json=active_payload)

            if resp.status_code >= 400:
                err_obj = normalize_error(resp.status_code, resp.content)
                return JSONResponse(status_code=resp.status_code, content=err_obj)

            if target["provider"] == sub_prov_name and sub_breaker:
                await sub_breaker.record_success()

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type="application/json",
                headers={
                    "X-Selected-Model": target["model"],
                    "X-Selected-Provider": target["provider"],
                    "X-Routing-Reason": explanation
                }
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Upstream router error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"], log_level="info")
