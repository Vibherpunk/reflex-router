"""
Reflex Multi-Provider System 1 Router & Gateway Server.
FastAPI ASGI proxy supporting Dynamic Capability Arbitration, Streaming SSE,
Speculative Failover, and Non-Interactive Subagent CLI Harness Federation.
"""
import os
import re
import json
import time
import uuid
import httpx
import logging
import asyncio
import threading
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, AsyncGenerator, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse

from config import CONFIG, get_key
from memory import record_incident, get_stats, log_request
from circuit_breaker import ProviderCircuitBreaker, BreakerState, ensure_breaker, BREAKER_REGISTRY
from federation import discover_harnesses, delegate_subagent
from catalog import ModelCatalog, ReflexModelDefinition
from solver import ArbitrationSolver, CapabilityRequestVector

logger = logging.getLogger("reflex.server")
logging.basicConfig(level=logging.INFO)

# Global dynamic model catalog and arbitration solver
catalog = ModelCatalog()
solver = ArbitrationSolver(catalog)

class GatewayPool:
    client: Optional[httpx.AsyncClient] = None

gateway_pool = GatewayPool()

def _background_catalog_refresh():
    """Periodic worker to keep model catalog fresh every hour."""
    while True:
        try:
            time.sleep(3600)
            catalog.refresh_from_providers()
            logger.info("Reflex background catalog refresh completed.")
        except Exception as e:
            logger.warning(f"Error in background catalog refresh: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=300, keepalive_expiry=60.0)
    gateway_pool.client = httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(120.0, connect=15.0))
    logger.info("Reflex Gateway HTTP connection pool initialized.")
    # Initialize catalog in background thread on startup
    threading.Thread(target=catalog.refresh_from_providers, kwargs={"force": False}, daemon=True).start()
    threading.Thread(target=_background_catalog_refresh, daemon=True).start()
    yield
    if gateway_pool.client:
        await gateway_pool.client.aclose()
    logger.info("Reflex Gateway HTTP connection pool safely closed.")

app = FastAPI(
    title="Reflex System 1 Router",
    version="2.2.0",
    description="Intelligent Multi-Provider Capability Router with Dynamic Arbitration",
    lifespan=lifespan
)

def estimate_tokens(payload: Dict[str, Any]) -> int:
    """Fast, lightweight token count estimation based on char length."""
    total_chars = sum(len(str(m.get("content", ""))) for m in payload.get("messages", []))
    return max(1, total_chars // 4)

def get_session_id(payload: Dict[str, Any], headers: Any) -> str:
    """Deterministic session extractor across OpenCode, Goose, and direct clients."""
    if "session_id" in payload:
        return str(payload["session_id"])
    if "metadata" in payload and isinstance(payload["metadata"], dict) and "session_id" in payload["metadata"]:
        return str(payload["metadata"]["session_id"])
    if "x-opencode-session" in headers:
        return str(headers["x-opencode-session"])
    if "x-session-id" in headers:
        return str(headers["x-session-id"])
    return "session-ephemeral-default"

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

def resolve_connection(route: Dict[str, Any], payload: Dict[str, Any], session_id: str) -> Tuple[str, Dict[str, str]]:
    """Resolves upstream execution URL and headers dynamically from catalog or config."""
    prov_name = route["provider"]
    model_id = route["model"]

    m = catalog.get(prov_name, model_id) or catalog.get_by_id(model_id)
    base_url = None
    api_key = ""
    user_agent = "reflex-gateway/2.2"
    requires_session = False

    if m and m.base_url:
        base_url = m.base_url
        if m.api_key_env:
            api_key = get_key(m.api_key_env)

    if prov_name in CONFIG.get("providers", {}):
        cfg_prov = CONFIG["providers"][prov_name]
        base_url = base_url or cfg_prov.get("base_url")
        api_key = api_key or cfg_prov.get("api_key", "")
        user_agent = cfg_prov.get("user_agent", user_agent)
        requires_session = cfg_prov.get("requires_session", False)

    if "opencode" in prov_name.lower() or (base_url and "opencode.ai" in base_url):
        requires_session = True
        user_agent = "opencode/1.18.13"
        if not api_key:
            from config import get_opencode_go_key
            api_key = get_opencode_go_key()

    if not base_url:
        raise HTTPException(status_code=502, detail=f"No upstream HTTP execution path configured for provider '{prov_name}'")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
        "Accept": "text/event-stream" if payload.get("stream") else "application/json"
    }
    if requires_session:
        headers["x-opencode-session"] = session_id or "goose-session"
    if prov_name == "openrouter":
        headers["HTTP-Referer"] = "http://127.0.0.1:8787"
        headers["X-Title"] = "Reflex-Gateway"

    return f"{base_url.rstrip('/')}/chat/completions", headers

def select_candidate_routes(
    payload: Dict[str, Any],
    session_id: str,
    access_method: Optional[str] = "http_gateway"
) -> Tuple[Dict[str, Any], Dict[str, Any], int, str]:
    """
    Selects primary subscription route and metered fallback based on dynamic capability arbitration.
    Returns (subscription_route, metered_route, tier_num, explanation).
    """
    requested_model = payload.get("model", "auto")
    messages = payload.get("messages", [])
    vector = solver.extract_vector(messages, requested_model)
    primary, metered, reason = solver.arbitrate(
        vector,
        session_id=session_id,
        requested_model=requested_model,
        preferred_access_method=access_method
    )
    return primary, metered, vector.tier_num, reason

@app.get("/health")
@app.get("/")
async def health_check():
    providers_summary = {}
    for m in catalog.list_all():
        if m.provider not in providers_summary:
            breaker = ensure_breaker(m.provider)
            providers_summary[m.provider] = {
                "name": m.provider,
                "type": m.billing_type,
                "circuit_breaker": breaker.get_status()
            }
    return {
        "status": "active",
        "service": "Reflex Multi-Provider Intelligent Gateway",
        "version": "2.2.0",
        "catalog_size": len(catalog.list_all()),
        "providers": providers_summary
    }

@app.get("/v1/models")
async def list_models():
    """Returns OpenAI-compatible model listing from dynamic catalog."""
    models_list = [
        {"id": "auto", "object": "model", "owned_by": "reflex-router", "permission": []}
    ]
    for m in catalog.list_all():
        models_list.append({
            "id": m.id,
            "object": "model",
            "owned_by": m.provider,
            "billing_type": m.billing_type,
            "context_window": m.context_window,
            "reasoning_capability": m.reasoning_capability,
            "architecture_score": m.architecture_score,
            "access_method": m.access_method
        })
    return {"object": "list", "data": models_list}

@app.post("/v1/catalog/refresh")
async def refresh_catalog(force: bool = True):
    """Refreshes live model catalog across all harnesses and APIs."""
    catalog.refresh_from_providers(force=force)
    return {"status": "refreshed", "catalog_size": len(catalog.list_all())}

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
    stats["catalog_size"] = len(catalog.list_all())
    return stats

@app.get("/v1/harnesses")
async def get_harnesses(rescan: bool = False):
    """Returns all auto-discovered local CLI agent harnesses on this machine."""
    return discover_harnesses(force_rescan=rescan)

@app.post("/v1/route")
async def route_preview(request: Request):
    """Previews dynamic capability arbitration and provider routing without execution."""
    data = await request.json()
    prompt = str(data.get("prompt", "")).strip()
    messages = data.get("messages", [{"role": "user", "content": prompt}])
    requested_model = data.get("model", "auto")
    vector = solver.extract_vector(messages, requested_model)
    primary, metered, reason = solver.arbitrate(
        vector,
        session_id="preview",
        requested_model=requested_model
    )
    tier_names = {
        0: "Fast / Tool Churn",
        1: "General Implementation",
        2: "Deep Reasoning / Concurrency / Bugfix",
        3: "Frontier Architecture / System Specs"
    }
    return {
        "status": "success",
        "tier": vector.tier_num,
        "tier_name": tier_names.get(vector.tier_num, "Custom"),
        "reason": reason,
        "subscription_route": primary,
        "metered_route": metered
    }

@app.post("/v1/delegate")
async def handle_delegation(request: Request):
    """
    CLI Harness Federation: Delegates a subagent task to an installed CLI agent harness.
    Enforces non-interactive execution, process group isolation, and recursion depth limits.
    """
    data = await request.json()
    task = str(data.get("task", "")).strip()
    if not task:
        raise HTTPException(status_code=400, detail="Task prompt is required")

    delegation_depth = int(data.get("delegation_depth", 0))
    delegation_chain = str(data.get("delegation_chain", ""))
    delegation_id = str(data.get("delegation_id", f"del-{int(time.time()*1000)}"))

    if delegation_depth >= 2:
        return {
            "status": "error",
            "error": f"RecursionLimitExceeded: Max delegation depth (2) reached across HTTP boundary. Chain: {delegation_chain}",
            "delegation_depth": delegation_depth
        }

    preferred_model = data.get("preferred_model", "auto")
    preferred_harness = data.get("preferred_harness")
    cwd = data.get("cwd")
    timeout_sec = float(data.get("timeout_sec", 120.0))

    # Dynamic capability routing if preferred_model is "auto"
    if preferred_model == "auto":
        messages = [{"role": "user", "content": task}]
        vector = solver.extract_vector(messages)
        primary_route, _, _ = solver.arbitrate(vector, session_id=delegation_id)
        preferred_model = primary_route["model"]
        if not preferred_harness and primary_route.get("access_method") == "cli_harness":
            m_def = catalog.get(primary_route["provider"], primary_route["model"])
            if m_def and m_def.harness_binary:
                preferred_harness = m_def.harness_binary

    result = await delegate_subagent(
        task=task,
        preferred_model=preferred_model,
        preferred_harness=preferred_harness,
        cwd=cwd,
        timeout_sec=timeout_sec,
        delegation_depth=delegation_depth,
        delegation_chain=delegation_chain,
        delegation_id=delegation_id
    )

    return result

async def preflight_and_stream(
    sub_route: Dict[str, Any],
    metered_route: Dict[str, Any],
    payload: Dict[str, Any],
    session_id: str,
    request: Request
) -> Tuple[AsyncGenerator[bytes, None], str, str, str]:
    """
    Reflex Speculative Failover Protocol:
    Verifies upstream status code AND first chunk BEFORE committing downstream headers.
    """
    client = gateway_pool.client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    sub_prov_name = sub_route["provider"]
    metered_prov_name = metered_route["provider"]
    sub_breaker = ensure_breaker(sub_prov_name)

    # 1. Evaluate Circuit Breaker for subscription
    can_try_sub = await sub_breaker.can_attempt()
    if can_try_sub:
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
    upstream_url, headers = resolve_connection(target_route, active_payload, session_id)

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
        upstream_url, headers = resolve_connection(target_route, active_payload, session_id)

        upstream_ctx = client.stream("POST", upstream_url, headers=headers, json=active_payload)
        stream_response = await upstream_ctx.__aenter__()

    # If status code >= 400 even on fallback, abort before committing stream
    if stream_response.status_code >= 400:
        err_bytes = await stream_response.aread()
        await upstream_ctx.__aexit__(None, None, None)
        if target_route["provider"] == sub_prov_name and sub_breaker:
            await sub_breaker.record_failure(stream_response.status_code)
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
        if target_route["provider"] == sub_prov_name and sub_breaker:
            await sub_breaker.record_failure(504)
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
        sub_breaker = ensure_breaker(sub_prov_name)
        can_try_sub = await sub_breaker.can_attempt()

        target = sub_route if can_try_sub else metered_route
        active_payload = dict(payload)
        active_payload["model"] = target["model"]
        upstream_url, headers = resolve_connection(target, active_payload, session_id)

        try:
            resp = await client.post(upstream_url, headers=headers, json=active_payload)
            if resp.status_code in (429, 502, 503, 504) and target["provider"] == sub_prov_name:
                await sub_breaker.record_failure(resp.status_code)
                target = metered_route
                active_payload["model"] = target["model"]
                upstream_url, headers = resolve_connection(target, active_payload, session_id)
                resp = await client.post(upstream_url, headers=headers, json=active_payload)

            if resp.status_code >= 400:
                if target["provider"] == sub_prov_name:
                    await sub_breaker.record_failure(resp.status_code)
                err_obj = normalize_error(resp.status_code, resp.content)
                return JSONResponse(status_code=resp.status_code, content=err_obj)

            if target["provider"] == sub_prov_name:
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
            if target["provider"] == sub_prov_name:
                await sub_breaker.record_failure(0)  # 0 = non-HTTP transport failure
            raise HTTPException(status_code=502, detail=f"Upstream router error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"], log_level="info")
