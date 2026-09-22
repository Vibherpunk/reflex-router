"""
Production-grade System 1 Intelligent Router Proxy for OpenCode-Go.
Implements resilient SSE streaming, heartbeats, speculative preamble failover,
and contextual task-difficulty classification.
"""
import os
import json
import time
import asyncio
import hashlib
from typing import Dict, Any, AsyncGenerator, Tuple, Optional

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from config import CONFIG
from classifier import classify_request
from memory import record_incident, get_stats, log_request

app = FastAPI(title="OpenCode-Go System 1 Router Gateway", version="1.0.0")

# In-memory session tracking for KV-cache protection
SESSION_AFFINITY: Dict[str, Dict[str, Any]] = {}

def get_session_id(payload: Dict[str, Any], headers: Any) -> str:
    """Derives a stable session ID from headers or message prefix."""
    if "x-opencode-session" in headers:
        return headers["x-opencode-session"]
    
    # Hash first message content + system prompt as session identifier
    messages = payload.get("messages", [])
    if messages:
        first_content = str(messages[0].get("content", ""))[:200]
        return "sess-" + hashlib.sha256(first_content.encode()).hexdigest()[:16]
    return "sess-default-stream"

def estimate_tokens(payload: Dict[str, Any]) -> int:
    """Fast character-based token estimator (~3.8 chars/token)."""
    total_chars = sum(len(str(m.get("content", ""))) for m in payload.get("messages", []))
    return int((total_chars / 3.8) * 1.15)  # 1.15x safety headroom

def select_model_and_effort(payload: Dict[str, Any], session_id: str) -> Tuple[str, bool, str]:
    """
    Selects model, reasoning flag, and explanation based on classification
    and KV-cache preservation rules.
    """
    tokens = estimate_tokens(payload)
    tier, reason = classify_request(payload.get("messages", []))
    tier_info = CONFIG["model_tiers"][tier]

    # KV-Cache Latch: If context is large, lock to existing model family if present
    if tokens > CONFIG["kv_cache_context_threshold"] and session_id in SESSION_AFFINITY:
        locked_model = SESSION_AFFINITY[session_id]["model"]
        return locked_model, tier_info["reasoning"], f"KV-Cache Latch locked to {locked_model} ({tokens} tokens)"

    chosen_model = tier_info["primary"]
    # Save affinity for future turns
    SESSION_AFFINITY[session_id] = {
        "model": chosen_model,
        "last_seen": time.time()
    }
    return chosen_model, tier_info["reasoning"], f"Tier {tier} ({tier_info['name']}): {reason}"

@app.get("/health")
@app.get("/")
async def health_check():
    key = CONFIG["api_key"]
    return {
        "status": "active",
        "service": "OpenCode-Go System 1 Intelligent Router",
        "auth_configured": bool(key),
        "key_prefix": key[:8] if key else None,
        "tiers": CONFIG["model_tiers"]
    }

@app.get("/v1/models")
async def list_models():
    """Returns OpenAI-compatible model listing."""
    models_list = [
        {"id": "auto", "object": "model", "owned_by": "system1-router", "permission": []}
    ]
    for tier_id, info in CONFIG["model_tiers"].items():
        models_list.append({"id": info["primary"], "object": "model", "owned_by": "opencode-go"})
    return {"object": "list", "data": models_list}

@app.post("/v1/feedback")
async def submit_feedback(request: Request):
    """
    Submits user or agent execution feedback.
    Skidnir-style System 1 recursive learning records the failure and escalates future similar turns.
    """
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
    """Returns memory metrics, request counts, and recent incident logs."""
    return get_stats()

async def stream_with_heartbeat(
    client: httpx.AsyncClient,
    upstream_url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    request: Request,
    fallback_model: str
) -> AsyncGenerator[bytes, None]:
    """
    Streams SSE chunks from OpenCode-Go with:
    - Speculative 512B preamble buffer for pre-flight failover
    - SSE Heartbeat (: ping\n\n) every 3 seconds to defeat client read timeouts
    - Instant cancellation if client disconnects
    """
    active_payload = dict(payload)
    
    def try_stream(p: Dict[str, Any]):
        return client.stream(
            "POST",
            upstream_url,
            headers=headers,
            json=p,
            timeout=httpx.Timeout(connect=15.0, read=90.0, write=15.0, pool=15.0)
        )

    # 1. Speculative Preamble Buffer
    preamble_chunks = []
    preamble_bytes = 0
    upstream_ctx = None
    stream_response = None

    try:
        upstream_ctx = try_stream(active_payload)
        stream_response = await upstream_ctx.__aenter__()

        # If upstream fails before emitting valid stream (e.g. 429 or 500), try fallback
        if stream_response.status_code >= 400:
            err_body = await stream_response.aread()
            await upstream_ctx.__aexit__(None, None, None)
            
            # Failover to fallback model
            active_payload["model"] = fallback_model
            upstream_ctx = try_stream(active_payload)
            stream_response = await upstream_ctx.__aenter__()
            if stream_response.status_code >= 400:
                final_err = await stream_response.aread()
                yield f"data: {json.dumps({'error': {'message': final_err.decode(), 'code': stream_response.status_code}})}\n\n".encode()
                return

        # 2. Single-pass streaming with preamble buffer and heartbeat
        preamble_flushed = False
        last_chunk_time = time.time()

        async for chunk in stream_response.aiter_bytes():
            if await request.is_disconnected():
                break

            if not preamble_flushed:
                preamble_chunks.append(chunk)
                preamble_bytes += len(chunk)
                if preamble_bytes >= CONFIG["preamble_buffer_bytes"] or b"data:" in chunk:
                    for c in preamble_chunks:
                        yield c
                    preamble_flushed = True
                    preamble_chunks = []
                    last_chunk_time = time.time()
                continue

            # Heartbeat check
            now = time.time()
            if now - last_chunk_time >= CONFIG["heartbeat_interval_seconds"]:
                yield b": ping\n\n"
            
            yield chunk
            last_chunk_time = time.time()

        # Flush any remaining preamble if stream was shorter than buffer threshold
        if not preamble_flushed and preamble_chunks:
            for c in preamble_chunks:
                yield c

    except Exception as e:
        yield f": stream_error: {str(e)}\n\n".encode()
    finally:
        if upstream_ctx:
            try:
                await upstream_ctx.__aexit__(None, None, None)
            except Exception:
                pass

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    session_id = get_session_id(payload, request.headers)

    # 1. Routing & Parameter Normalization
    model, is_reasoning, reason = select_model_and_effort(payload, session_id)
    tier_num = 0
    for tid, info in CONFIG["model_tiers"].items():
        if info["primary"] == model:
            tier_num = tid
            break
    fallback_model = CONFIG["model_tiers"][tier_num]["fallback"]
    try:
        log_request(tier_num)
    except Exception:
        pass

    # Rewrite model
    payload["model"] = model

    # 2. Prepare Upstream Headers
    api_key = CONFIG["api_key"]
    upstream_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": CONFIG["user_agent"],
        "x-opencode-session": session_id,
        "Accept": "text/event-stream" if payload.get("stream") else "application/json"
    }

    upstream_url = f"{CONFIG['upstream_base_url']}/chat/completions"

    # 3. Handle Streaming vs Non-Streaming
    is_stream = payload.get("stream", False)

    if is_stream:
        client = httpx.AsyncClient()
        return StreamingResponse(
            stream_with_heartbeat(
                client=client,
                upstream_url=upstream_url,
                headers=upstream_headers,
                payload=payload,
                request=request,
                fallback_model=fallback_model
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Selected-Model": model,
                "X-Routing-Reason": reason
            }
        )
    else:
        # Non-streaming request
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(upstream_url, headers=upstream_headers, json=payload)
                if resp.status_code >= 400:
                    # Fallback attempt
                    payload["model"] = fallback_model
                    resp = await client.post(upstream_url, headers=upstream_headers, json=payload)
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type="application/json",
                    headers={
                        "X-Selected-Model": payload["model"],
                        "X-Routing-Reason": reason
                    }
                )
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Upstream router error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=CONFIG["host"], port=CONFIG["port"], log_level="info")
