#!/usr/bin/env python3
"""
Reflex MCP Server (Plan B Hardened Implementation).
MCP 2.x standard server exposing Reflex CLI Federation & Gateway Routing to AGY and other agents.
All stdio I/O strictly isolated to JSON-RPC. Logging directed to stderr and ~/.reflex/logs/mcp_server.log.
"""
import os
import sys
import json
import time
import uuid
import logging
from pathlib import Path
from typing import Optional, Literal, Dict, Any
from pydantic import Field
from typing_extensions import Annotated

import httpx
from mcp.server.mcpserver import MCPServer

# 1. Stdio Protection: Ensure stdout is NEVER polluted by application logs
LOG_DIR = Path.home() / ".reflex/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "mcp_server.log"

logger = logging.getLogger("reflex.mcp")
logger.setLevel(logging.INFO)
file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(logging.Formatter("[reflex-mcp] %(levelname)s: %(message)s"))
logger.addHandler(file_handler)
logger.addHandler(stderr_handler)

ROUTER_BASE_URL = os.environ.get("REFLEX_ROUTER_URL", "http://127.0.0.1:8787")
MAX_DELEGATION_DEPTH = 2
MAX_INLINE_CHARS = 4000

mcp = MCPServer(
    name="reflex",
    version="2.1.0",
    description="Reflex Gateway & CLI Harness Federation Router"
)

def format_delegation_output(data: Dict[str, Any], delegation_id: str) -> str:
    """Truncates massive tool outputs and persists full transcript to disk."""
    raw_response = data.get("response")
    raw_str = json.dumps(raw_response, indent=2) if isinstance(raw_response, dict) else str(raw_response or "")
    
    transcript_file = LOG_DIR / f"delegation_{delegation_id}.log"
    try:
        transcript_file.write_text(raw_str, encoding="utf-8")
    except Exception as e:
        logger.error(f"Could not persist delegation transcript: {e}")

    truncated = False
    if len(raw_str) > MAX_INLINE_CHARS:
        preview = raw_str[:MAX_INLINE_CHARS] + f"\n\n... [Truncated {len(raw_str) - MAX_INLINE_CHARS} characters] ..."
        truncated = True
    else:
        preview = raw_str

    summary = {
        "status": data.get("status", "unknown"),
        "effective_harness": data.get("effective_harness"),
        "duration_ms": data.get("duration_ms"),
        "delegation_depth": data.get("delegation_depth"),
        "output_preview": preview,
        "transcript_path": str(transcript_file) if truncated else None,
        "attempts": data.get("attempts", [])
    }
    return json.dumps(summary, indent=2)

@mcp.tool(
    name="reflex_delegate",
    description="Delegate a coding task to an installed CLI agent harness (claude, opencode, goose, codex, agy) via Reflex."
)
async def reflex_delegate(
    task: Annotated[str, Field(description="Detailed task instructions and prompt for the delegated subagent.")],
    preferred_model: Annotated[str, Field(default="auto", description="Model name or capability (e.g. 'claude-3.7-sonnet', 'deepseek-v4-pro', 'auto').")] = "auto",
    preferred_harness: Annotated[Optional[Literal["auto", "claude", "agy", "opencode", "goose", "codex"]], Field(default=None, description="Explicit harness override. Defaults to auto.")] = None,
    cwd: Annotated[Optional[str], Field(default=None, description="Working directory for subagent execution. Must be an existing path.")] = None,
    timeout_sec: Annotated[float, Field(default=120.0, ge=5.0, le=600.0, description="Execution timeout in seconds (5-600).")] = 120.0
) -> str:
    # Recursion barrier: Fast local check before hitting the network
    current_depth = int(os.environ.get("REFLEX_DELEGATION_DEPTH", "0"))
    current_chain = os.environ.get("REFLEX_DELEGATION_CHAIN", "")
    if os.environ.get("REFLEX_DISABLE_DELEGATION") == "1" or current_depth >= MAX_DELEGATION_DEPTH:
        return json.dumps({
            "status": "rejected",
            "error": f"RecursionLimitExceeded: Subagent delegation depth limit ({MAX_DELEGATION_DEPTH}) reached. Chain: {current_chain}",
            "delegation_depth": current_depth
        })

    # Validate cwd
    target_cwd = cwd or os.getcwd()
    if not Path(target_cwd).is_dir():
        return json.dumps({
            "status": "error",
            "error": f"DirectoryNotFound: Specified cwd '{target_cwd}' does not exist or is not a directory."
        })

    delegation_id = f"del-{uuid.uuid4().hex[:12]}"
    payload = {
        "task": task,
        "preferred_model": preferred_model,
        "preferred_harness": None if preferred_harness == "auto" else preferred_harness,
        "cwd": target_cwd,
        "timeout_sec": timeout_sec,
        "delegation_depth": current_depth,
        "delegation_chain": current_chain,
        "delegation_id": delegation_id
    }

    headers = {
        "Content-Type": "application/json",
        "X-Reflex-Delegation-Depth": str(current_depth),
        "X-Reflex-Delegation-Chain": current_chain,
        "X-Reflex-Caller": "agy-mcp"
    }

    # Pure RPC execution via HTTP client
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_sec + 15.0, connect=5.0)) as client:
            resp = await client.post(f"{ROUTER_BASE_URL}/v1/delegate", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return format_delegation_output(data, delegation_id)
            else:
                return json.dumps({
                    "status": "http_error",
                    "status_code": resp.status_code,
                    "error": resp.text[:500]
                })
    except httpx.ConnectError:
        return json.dumps({
            "status": "daemon_unreachable",
            "error": f"Could not connect to Reflex Router at {ROUTER_BASE_URL}. Ensure the router daemon is running.",
            "remediation": "Start daemon via: cd /Users/ai/workspace/system1-router && ./start.sh"
        })
    except httpx.TimeoutException:
        return json.dumps({
            "status": "timeout",
            "error": f"Reflex delegation timed out after {timeout_sec}s awaiting response from router daemon."
        })
    except Exception as e:
        return json.dumps({
            "status": "exception",
            "error": str(e)
        })

@mcp.tool(
    name="reflex_status",
    description="Check Reflex gateway health, circuit breaker states, and detected CLI agent harnesses."
)
async def reflex_status() -> str:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            health_res = await client.get(f"{ROUTER_BASE_URL}/health")
            harnesses_res = await client.get(f"{ROUTER_BASE_URL}/v1/harnesses")
            
            return json.dumps({
                "status": "online",
                "router_url": ROUTER_BASE_URL,
                "health": health_res.json() if health_res.status_code == 200 else health_res.text,
                "harnesses": harnesses_res.json() if harnesses_res.status_code == 200 else harnesses_res.text
            }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "offline",
            "router_url": ROUTER_BASE_URL,
            "error": f"Reflex router is unreachable: {str(e)}",
            "remediation": "Start daemon via: cd /Users/ai/workspace/system1-router && ./start.sh"
        }, indent=2)

@mcp.tool(
    name="reflex_route",
    description="Preview Reflex System 1 tier classification, provider selection, and model routing for a prompt."
)
async def reflex_route(
    prompt: Annotated[str, Field(description="The prompt or query to classify and route.")]
) -> str:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            res = await client.post(f"{ROUTER_BASE_URL}/v1/route", json={"prompt": prompt})
            if res.status_code == 200:
                return json.dumps(res.json(), indent=2)
            return json.dumps({"status": "error", "status_code": res.status_code, "error": res.text})
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Reflex router unreachable: {str(e)}"})

if __name__ == "__main__":
    import asyncio
    asyncio.run(mcp.run_stdio_async())
