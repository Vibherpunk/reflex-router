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

def build_delegation_env(parent_env: Optional[Dict[str, str]] = None, current_harness: str = "") -> Dict[str, str]:
    """
    Builds a secure, non-interactive runtime environment for child agent processes.
    Enforces recursion limits and cycle detection.
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
    depth = int(env.get("REFLEX_DELEGATION_DEPTH", "0"))
    chain = env.get("REFLEX_DELEGATION_CHAIN", "")

    env["REFLEX_DELEGATION_DEPTH"] = str(depth + 1)
    env["REFLEX_DELEGATION_CHAIN"] = f"{chain}:{current_harness}" if chain else current_harness

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

async def delegate_subagent(
    task: str,
    preferred_model: str = "auto",
    preferred_harness: Optional[str] = None,
    cwd: Optional[str] = None,
    timeout_sec: float = 120.0
) -> Dict[str, Any]:
    """
    Main delegation coordinator.
    Enforces recursion depth, cycle prevention, process isolation, and returns a structured envelope.
    """
    depth = int(os.environ.get("REFLEX_DELEGATION_DEPTH", "0"))
    chain = os.environ.get("REFLEX_DELEGATION_CHAIN", "")

    if depth >= MAX_DELEGATION_DEPTH:
        return {
            "status": "error",
            "error": f"RecursionLimitExceeded: Max delegation depth ({MAX_DELEGATION_DEPTH}) reached. Chain: {chain}",
            "delegation_depth": depth
        }

    manifest = discover_harnesses()
    installed = manifest.get("harnesses", {})

    # Select harness
    harness_key = preferred_harness
    if not harness_key or harness_key not in installed:
        harness_key = select_harness_for_model(preferred_model, manifest)

    if not harness_key or harness_key not in installed:
        return {
            "status": "unsupported",
            "message": f"No authenticated CLI harness installed for model '{preferred_model}'. Falling back to Reflex HTTP gateway.",
            "available_harnesses": list(installed.keys())
        }

    # Cycle Detection
    chain_elements = chain.split(":") if chain else []
    if harness_key in chain_elements:
        return {
            "status": "cycle_detected",
            "message": f"Cycle detected in delegation chain '{chain}'. Refusing to re-invoke '{harness_key}'.",
            "delegation_depth": depth
        }

    h_info = installed[harness_key]
    spec = HARNESS_SPECS[harness_key]
    cmd = spec["cmd_builder"](h_info["binary_path"], task)
    env = build_delegation_env(os.environ, current_harness=harness_key)

    start_time = time.time()
    try:
        returncode, stdout, stderr = await execute_harness_safe(
            cmd=cmd,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec
        )
        duration_ms = int((time.time() - start_time) * 1000)

        # Parse JSON if output format was structured
        parsed_response = None
        try:
            parsed_response = json.loads(stdout.strip())
        except Exception:
            parsed_response = stdout.strip()

        return {
            "status": "success" if returncode == 0 else "failed",
            "exit_code": returncode,
            "harness": harness_key,
            "subscription": h_info["subscription"],
            "model_requested": preferred_model,
            "duration_ms": duration_ms,
            "delegation_depth": depth + 1,
            "response": parsed_response,
            "raw_stderr": stderr[:500] if returncode != 0 else None
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "harness": harness_key,
            "duration_ms": int((time.time() - start_time) * 1000),
            "delegation_depth": depth + 1
        }
