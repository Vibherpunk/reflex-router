"""
Reflex CLI Harness Federation Engine (Plan B Hardened Implementation).
Auto-discovers installed coding agent harnesses (agy, claude, opencode, goose, codex),
enforces headless non-interactive execution, isolates child process groups,
and arbitrates cross-harness subagent delegation without raw API keys.
"""
import os
import sys
import json
import time
import shutil
import signal
import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("reflex.federation")

HOME = Path.home()
MANIFEST_FILE = HOME / ".reflex/harnesses.json"
MAX_DELEGATION_DEPTH = 2

# Canonical search paths for developer agent CLIs
SEARCH_PATHS = [
    str(HOME / ".local/bin"),
    str(HOME / ".opencode/bin"),
    str(HOME / ".cargo/bin"),
    str(HOME / ".npm-global/bin"),
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin"
]

# Strict Non-Interactive CLI Command Matrix
HARNESS_SPECS = {
    "claude": {
        "name": "Claude Code",
        "binary": "claude",
        "subscription": "Claude Pro / Teams",
        "models": ["claude-3.7-sonnet", "claude-3.5-sonnet", "claude-3-opus"],
        "auth_files": [HOME / ".claude.json", HOME / ".claude/config.json"],
        "cmd_builder": lambda bin_path, task: [
            bin_path,
            "-p", task,
            "--output-format", "json",
            "--dangerously-skip-permissions",
            "--permission-prompts", "none"
        ]
    },
    "agy": {
        "name": "Antigravity",
        "binary": "agy",
        "subscription": "Google Gemini Ultra / Advanced",
        "models": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-ultra"],
        "auth_files": [HOME / ".gemini/antigravity-cli/antigravity-oauth-token"],
        "cmd_builder": lambda bin_path, task: [
            bin_path,
            "-p", task,
            "--output-format", "json",
            "--dangerously-skip-permissions"
        ]
    },
    "opencode": {
        "name": "OpenCode",
        "binary": "opencode",
        "subscription": "OpenCode-Go",
        "models": ["deepseek-v4-pro", "qwen3.7-max", "deepseek-v4-flash", "glm-5.3"],
        "auth_files": [HOME / ".local/share/opencode/auth.json"],
        "cmd_builder": lambda bin_path, task: [
            bin_path,
            "run", task,
            "--format", "json",
            "--auto"
        ]
    },
    "goose": {
        "name": "Goose",
        "binary": "goose",
        "subscription": "Goose Custom / Reflex",
        "models": ["auto", "deepseek-v4-pro", "qwen3.7-max"],
        "auth_files": [HOME / ".config/goose/config.yaml"],
        "cmd_builder": lambda bin_path, task: [
            bin_path,
            "run",
            "-t", task,
            "--no-session",
            "--output-format", "json",
            "-q"
        ]
    },
    "codex": {
        "name": "Codex",
        "binary": "codex",
        "subscription": "OpenAI Codex",
        "models": ["o3-mini", "o1", "gpt-4o"],
        "auth_files": [HOME / ".codex", HOME / ".codex/installation_id"],
        "cmd_builder": lambda bin_path, task: [
            bin_path,
            "exec", task,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox"
        ]
    }
}

def get_augmented_path() -> str:
    """Builds a comprehensive PATH string incorporating all user agent directories."""
    existing = os.environ.get("PATH", "")
    paths = list(SEARCH_PATHS)
    for p in existing.split(":"):
        if p and p not in paths:
            paths.append(p)
    return ":".join(paths)

def find_binary(binary_name: str) -> Optional[str]:
    """Finds binary across canonical search paths even when launchd PATH is minimal."""
    for p in SEARCH_PATHS:
        candidate = Path(p) / binary_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(binary_name, path=get_augmented_path())

def check_harness_auth(spec: Dict[str, Any]) -> bool:
    """Verifies whether credentials or auth configuration exists on disk."""
    for auth_path in spec.get("auth_files", []):
        if auth_path.exists():
            return True
    return False

def discover_harnesses(force_rescan: bool = False) -> Dict[str, Any]:
    """
    Scans host machine for installed and authenticated CLI agent harnesses.
    Caches verified manifest to ~/.reflex/harnesses.json.
    """
    if not force_rescan and MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass

    manifest: Dict[str, Any] = {
        "updated_at": time.time(),
        "harnesses": {}
    }

    for key, spec in HARNESS_SPECS.items():
        bin_path = find_binary(spec["binary"])
        if not bin_path:
            continue

        authenticated = check_harness_auth(spec)
        version_str = "unknown"

        try:
            res = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=3.0)
            if res.returncode == 0:
                version_str = res.stdout.strip().split("\n")[0]
        except Exception:
            pass

        manifest["harnesses"][key] = {
            "name": spec["name"],
            "binary_path": bin_path,
            "version": version_str,
            "authenticated": authenticated,
            "subscription": spec["subscription"],
            "models": spec["models"]
        }

    os.makedirs(MANIFEST_FILE.parent, exist_ok=True)
    try:
        with open(MANIFEST_FILE, "w") as f:
            json.dump(manifest, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not persist harness manifest: {e}")

    return manifest

def build_delegation_env(
    parent_env: Optional[Dict[str, str]] = None,
    current_harness: str = "",
    depth: Optional[int] = None,
    chain: Optional[str] = None
) -> Dict[str, str]:
    """
    Builds a secure, non-interactive runtime environment for child agent processes.
    Enforces recursion limits, anti-loop suppression, and cycle detection.
    """
    base_env = dict(parent_env or os.environ)
    env = base_env.copy()

    # 1. Inject full search PATH
    env["PATH"] = get_augmented_path()

    # 2. Suppress interactive TTY behaviors
    env["TERM"] = "dumb"
    env["CI"] = "1"
    env["NO_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    # 3. Recursion Tracking
    curr_depth = depth if depth is not None else int(env.get("REFLEX_DELEGATION_DEPTH", "0"))
    curr_chain = chain if chain is not None else env.get("REFLEX_DELEGATION_CHAIN", "")

    new_depth = curr_depth + 1
    new_chain = f"{curr_chain}:{current_harness}" if curr_chain else current_harness

    env["REFLEX_DELEGATION_DEPTH"] = str(new_depth)
    env["REFLEX_DELEGATION_CHAIN"] = new_chain

    # Anti-loop guard: child agents must not re-invoke reflex delegation tool
    if new_depth >= MAX_DELEGATION_DEPTH:
        env["REFLEX_DISABLE_DELEGATION"] = "1"

    return env

async def execute_harness_safe(
    cmd: List[str],
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout_sec: float = 120.0
) -> Tuple[int, str, str]:
    """
    Executes a child agent harness in a detached process group.
    Guarantees clean termination of all descendant processes on timeout.
    """
    workdir = cwd or os.getcwd()
    run_env = env or build_delegation_env()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        env=run_env,
        stdin=asyncio.subprocess.DEVNULL,  # Prevent blocking on stdin prompts
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid  # Create separate process group
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_sec
        )
        return proc.returncode, stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            # Kill entire process group
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            await asyncio.sleep(1.0)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise TimeoutError(f"Harness execution timed out after {timeout_sec}s")

def select_harness_for_model(requested_model: str, manifest: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Matches a requested model to the best available installed and authenticated harness."""
    data = manifest or discover_harnesses()
    installed = data.get("harnesses", {})

    target = requested_model.lower().strip()

    # Exact or keyword mapping
    if "claude" in target or "sonnet" in target or "opus" in target:
        if "claude" in installed and installed["claude"]["authenticated"]:
            return "claude"

    if "gemini" in target or "ultra" in target:
        if "agy" in installed and installed["agy"]["authenticated"]:
            return "agy"

    if "deepseek" in target or "qwen" in target or "glm" in target or "kimi" in target:
        if "opencode" in installed and installed["opencode"]["authenticated"]:
            return "opencode"

    if "o3" in target or "o1" in target:
        if "codex" in installed and installed["codex"]["authenticated"]:
            return "codex"

    # Default fallback: check if any harness declares the model
    for h_name, h_info in installed.items():
        if h_info["authenticated"] and any(target in m.lower() for m in h_info.get("models", [])):
            return h_name

    return None

CIRCUIT_BREAKER_FILE = HOME / ".reflex/harness_health.json"

HARNESS_FAILOVER_CHAINS = {
    "claude": ["claude", "opencode", "goose"],
    "opencode": ["opencode", "goose", "agy"],
    "agy": ["agy", "opencode", "goose"],
    "goose": ["goose", "opencode", "claude"],
    "codex": ["codex", "opencode", "goose"]
}

def get_circuit_breaker() -> Dict[str, Any]:
    if CIRCUIT_BREAKER_FILE.exists():
        try:
            with open(CIRCUIT_BREAKER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def is_harness_tripped(harness: str) -> Tuple[bool, Optional[str]]:
    cb = get_circuit_breaker()
    entry = cb.get(harness)
    if not entry:
        return False, None
    tripped_at = entry.get("tripped_at", 0)
    cooldown_sec = entry.get("cooldown_sec", 300)
    now = time.time()
    if now < (tripped_at + cooldown_sec):
        rem = int((tripped_at + cooldown_sec) - now)
        return True, f"{entry.get('reason', 'rate_limited')} (cooling down, {rem}s remaining)"
    return False, None

def trip_harness_circuit_breaker(harness: str, reason: str, cooldown_sec: int = 3600):
    cb = get_circuit_breaker()
    cb[harness] = {
        "state": "TRIPPED",
        "tripped_at": time.time(),
        "cooldown_sec": cooldown_sec,
        "reason": reason
    }
    CIRCUIT_BREAKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(CIRCUIT_BREAKER_FILE, "w") as f:
            json.dump(cb, f, indent=2)
    except Exception:
        pass

def reset_harness_circuit_breaker(harness: str):
    cb = get_circuit_breaker()
    if harness in cb:
        cb.pop(harness, None)
        try:
            with open(CIRCUIT_BREAKER_FILE, "w") as f:
                json.dump(cb, f, indent=2)
        except Exception:
            pass

def classify_harness_error(harness: str, returncode: int, stdout: str, stderr: str) -> Tuple[bool, str, int]:
    """
    Determines if an error is an infrastructure/rate-limit/auth failure eligible for failover,
    versus user code or test execution failure.
    Returns (is_recoverable: bool, reason: str, cooldown_sec: int).
    """
    combined = f"{stderr}\n{stdout}".lower()

    if returncode == 127:
        return True, "binary_not_found", 86400

    if any(sig in combined for sig in [
        "429", "rate limit", "weekly limit", "daily limit",
        "quota exceeded", "resource has been exhausted", "credit balance too low",
        "rate_limit_error", "out of credits", "insufficient_quota"
    ]):
        cooldown = 3600 if "weekly" in combined else 300
        return True, "rate_limit_exceeded", cooldown

    if any(sig in combined for sig in [
        "401", "unauthorized", "auth token expired", "authentication failed",
        "invalid api key", "oauth token expired", "login required"
    ]):
        return True, "auth_failure", 3600

    if any(sig in combined for sig in [
        "connection refused", "network error", "timed out", "econnrefused",
        "failed to resolve host", "socket hang up"
    ]):
        return True, "network_error", 60

    # Guard: if the harness ran code/tests that failed, do NOT treat as infra error
    if any(sig in combined for sig in ["pytest", "assertionerror", "failed (failures=", "exit status 1", "test failed"]):
        return False, "test_or_code_failure", 0

    if returncode != 0:
        return True, f"harness_crash_exit_{returncode}", 120

    return False, "no_error", 0

def get_git_state(cwd: Optional[str]) -> Optional[Dict[str, Any]]:
    """Takes a lightweight snapshot of git HEAD and status in cwd."""
    target_dir = cwd or os.getcwd()
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
        ).stdout.strip()
        status_raw = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
        ).stdout.strip()
        status_lines = [line.strip() for line in status_raw.splitlines() if line.strip()]
        return {"is_git": True, "head": head, "status_lines": status_lines, "cwd": target_dir}
    except Exception:
        return None

def rollback_git_state(cwd: Optional[str], baseline: Optional[Dict[str, Any]]) -> bool:
    """
    Rolls back only NEW uncommitted modifications/untracked files introduced during the harness run.
    Safeguards pre-existing dirty files that were already modified before execution.
    """
    if not baseline or not baseline.get("is_git"):
        return False
    target_dir = baseline.get("cwd") or cwd or os.getcwd()
    try:
        curr_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2.0
        )
        if curr_res.returncode != 0:
            return False
        curr_lines = [line.strip() for line in curr_res.stdout.splitlines() if line.strip()]
        baseline_lines = set(baseline.get("status_lines", []))

        # Identify newly modified or created items
        new_entries = [line for line in curr_lines if line not in baseline_lines]
        if not new_entries:
            return True

        for entry in new_entries:
            parts = entry.split(None, 1)
            if len(parts) < 2:
                continue
            status_code, filepath = parts[0], parts[1]
            full_path = Path(target_dir) / filepath
            if status_code.startswith("??"):
                # Untracked file created by failed harness: safely remove
                if full_path.is_file():
                    full_path.unlink(missing_ok=True)
                elif full_path.is_dir():
                    shutil.rmtree(full_path, ignore_errors=True)
            else:
                # Tracked file modified by failed harness: restore to git index/HEAD
                subprocess.run(
                    ["git", "checkout", "--", filepath],
                    cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5.0
                )

        logger.warning(f"Selectively rolled back {len(new_entries)} files introduced by failed harness in {target_dir}")
        return True
    except Exception as e:
        logger.error(f"Failed to rollback git state: {e}")
        return False

async def call_reflex_http_gateway(task: str, preferred_model: str = "auto", timeout_sec: float = 30.0) -> Dict[str, Any]:
    """Fallback to Reflex HTTP Gateway on 127.0.0.1:8787 using urllib."""
    import urllib.request
    import urllib.error

    target = preferred_model.lower()
    if "claude" in target or "sonnet" in target:
        remote_model = "anthropic/claude-3.7-sonnet"
    elif "deepseek" in target or "r1" in target:
        remote_model = "deepseek/deepseek-r1"
    else:
        remote_model = "deepseek/deepseek-chat"

    gateway_url = "http://127.0.0.1:8787/v1/chat/completions"
    payload = {
        "model": remote_model,
        "messages": [{"role": "user", "content": task}],
        "stream": False
    }
    t0 = time.time()
    try:
        req = urllib.request.Request(
            gateway_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {
                "status": "success",
                "harness": "reflex_http_gateway",
                "model": remote_model,
                "response": choice,
                "duration_ms": int((time.time() - t0) * 1000)
            }
    except Exception as e:
        return {
            "status": "failed",
            "harness": "reflex_http_gateway",
            "error": str(e),
            "duration_ms": int((time.time() - t0) * 1000)
        }

async def delegate_subagent(
    task: str,
    preferred_model: str = "auto",
    preferred_harness: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_sec: float = 120.0,
    delegation_depth: Optional[int] = None,
    delegation_chain: Optional[str] = None,
    delegation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Main delegation coordinator with automatic failover waterfall and git rollback barrier.
    """
    depth = delegation_depth if delegation_depth is not None else int(os.environ.get("REFLEX_DELEGATION_DEPTH", "0"))
    chain = delegation_chain if delegation_chain is not None else os.environ.get("REFLEX_DELEGATION_CHAIN", "")

    if depth >= MAX_DELEGATION_DEPTH:
        return {
            "status": "error",
            "error": f"RecursionLimitExceeded: Max delegation depth ({MAX_DELEGATION_DEPTH}) reached. Chain: {chain}",
            "delegation_depth": depth
        }

    chain_elements = chain.split(":") if chain else []

    # Fast-path cycle guard if caller requested a harness already in the active delegation chain
    if preferred_harness and preferred_harness in chain_elements:
        return {
            "status": "cycle_detected",
            "message": f"Cycle detected in delegation chain '{chain}'. Refusing to re-invoke '{preferred_harness}'.",
            "delegation_depth": depth
        }

    manifest = discover_harnesses()
    installed = manifest.get("harnesses", {})

    primary_harness = preferred_harness
    if not primary_harness or primary_harness not in installed:
        if preferred_model == "auto":
            try:
                from classifier import classify_request
                from config import CONFIG
                tier_num, reason = classify_request([{"role": "user", "content": task}])
                tier_info = CONFIG["model_tiers"][tier_num]
                rec_model = tier_info["subscription"]["model"]
                primary_harness = select_harness_for_model(rec_model, manifest)
            except Exception:
                pass
        if not primary_harness:
            primary_harness = select_harness_for_model(preferred_model, manifest)

    if not primary_harness:
        primary_harness = "claude" if ("claude" in preferred_model or "sonnet" in preferred_model) else "opencode"

    # Build failover candidate list
    candidate_chain = list(HARNESS_FAILOVER_CHAINS.get(primary_harness, [primary_harness, "opencode", "goose"]))
    if primary_harness not in candidate_chain:
        candidate_chain.insert(0, primary_harness)

    attempts = []
    overall_start = time.time()
    workdir = cwd or os.getcwd()

    for h_name in candidate_chain:
        # Check cycle detection
        if h_name in chain_elements:
            continue

        # Check installation and authentication
        if h_name not in installed or not installed[h_name].get("authenticated"):
            attempts.append({
                "harness": h_name,
                "status": "skipped",
                "reason": "not_installed_or_unauthenticated"
            })
            continue

        # Check circuit breaker
        tripped, trip_reason = is_harness_tripped(h_name)
        if tripped:
            attempts.append({
                "harness": h_name,
                "status": "circuit_breaker_skipped",
                "reason": trip_reason
            })
            continue

        h_info = installed[h_name]
        spec = HARNESS_SPECS[h_name]
        cmd = spec["cmd_builder"](h_info["binary_path"], task)
        env = build_delegation_env(os.environ, current_harness=h_name, depth=depth, chain=chain)

        # 1. Snapshot Git state before execution
        baseline_git = get_git_state(workdir)

        h_start = time.time()
        try:
            returncode, stdout, stderr = await execute_harness_safe(
                cmd=cmd,
                cwd=workdir,
                env=env,
                timeout_sec=min(timeout_sec, 60.0)
            )
            h_duration_ms = int((time.time() - h_start) * 1000)

            if returncode == 0:
                reset_harness_circuit_breaker(h_name)
                parsed_response = None
                try:
                    parsed_response = json.loads(stdout.strip())
                except Exception:
                    parsed_response = stdout.strip()

                attempts.append({
                    "harness": h_name,
                    "status": "success",
                    "duration_ms": h_duration_ms
                })

                return {
                    "status": "success",
                    "effective_harness": h_name,
                    "subscription": h_info["subscription"],
                    "model_requested": preferred_model,
                    "duration_ms": int((time.time() - overall_start) * 1000),
                    "delegation_depth": depth + 1,
                    "response": parsed_response,
                    "attempts": attempts
                }

            # Non-zero exit code: classify error
            is_recoverable, reason, cooldown = classify_harness_error(h_name, returncode, stdout, stderr)

            if not is_recoverable:
                # User code or test failure: do NOT fail over, do NOT roll back
                attempts.append({
                    "harness": h_name,
                    "status": "failed",
                    "reason": reason,
                    "duration_ms": h_duration_ms
                })
                return {
                    "status": "failed",
                    "effective_harness": h_name,
                    "exit_code": returncode,
                    "reason": reason,
                    "raw_stderr": stderr[:500],
                    "raw_stdout": stdout[:500],
                    "attempts": attempts,
                    "duration_ms": int((time.time() - overall_start) * 1000),
                    "delegation_depth": depth + 1
                }

            # Recoverable infra error (429, timeout, crash): trip circuit breaker & selective rollback
            trip_harness_circuit_breaker(h_name, reason, cooldown)
            rolled_back = rollback_git_state(workdir, baseline_git)

            attempts.append({
                "harness": h_name,
                "status": "failover",
                "reason": reason,
                "duration_ms": h_duration_ms,
                "rolled_back": rolled_back
            })
            logger.warning(f"Harness {h_name} failed with {reason}. Rolled back new mutations and failing over.")

        except TimeoutError:
            trip_harness_circuit_breaker(h_name, "timeout", 300)
            rolled_back = rollback_git_state(workdir, baseline_git)
            attempts.append({
                "harness": h_name,
                "status": "timeout_failover",
                "duration_ms": int((time.time() - h_start) * 1000),
                "rolled_back": rolled_back
            })

        except Exception as e:
            attempts.append({
                "harness": h_name,
                "status": "exception",
                "error": str(e),
                "duration_ms": int((time.time() - h_start) * 1000)
            })

    # All CLI harnesses exhausted: Fall back to remote HTTP gateway
    logger.info("All CLI subscription harnesses exhausted. Falling back to Reflex HTTP Gateway.")
    gateway_res = await call_reflex_http_gateway(task, preferred_model)
    attempts.append({
        "harness": "reflex_http_gateway",
        "status": gateway_res.get("status"),
        "duration_ms": gateway_res.get("duration_ms")
    })

    return {
        "status": gateway_res.get("status", "failed"),
        "effective_harness": "reflex_http_gateway",
        "response": gateway_res.get("response") or gateway_res.get("error"),
        "model_requested": preferred_model,
        "attempts": attempts,
        "duration_ms": int((time.time() - overall_start) * 1000),
        "delegation_depth": depth + 1
    }
