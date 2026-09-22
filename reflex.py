#!/usr/bin/env python3
"""
Reflex Command-Line Interface (CLI).
Enables direct Harness Federation from terminal, scripts, or agent hooks.
"""
import sys
import json
import asyncio
import argparse
from pathlib import Path

from federation import discover_harnesses, delegate_subagent

def cmd_scan(args):
    """Scans and displays all detected CLI agent harnesses."""
    manifest = discover_harnesses(force_rescan=True)
    print("=== Reflex CLI Harness Federation: Discovered Agents ===")
    print(f"Manifest cache: {Path.home() / '.reflex/harnesses.json'}\n")
    harnesses = manifest.get("harnesses", {})
    if not harnesses:
        print("[-] No supported CLI agent harnesses detected.")
        return

    for key, info in harnesses.items():
        auth_status = "✅ Authenticated" if info["authenticated"] else "⚠️ Unauthenticated"
        print(f"• [{key.upper()}] {info['name']}")
        print(f"   Binary:       {info['binary_path']}")
        print(f"   Version:      {info['version']}")
        print(f"   Status:       {auth_status}")
        print(f"   Subscription: {info['subscription']}")
        print(f"   Models:       {', '.join(info['models'])}\n")

def cmd_delegate(args):
    """Delegates a subagent task to an installed CLI harness."""
    task = args.task
    model = args.model
    harness = args.harness
    timeout = args.timeout

    print(f"[*] Reflex Delegating task to {harness or 'auto-matched harness'} (model: {model})...")
    result = asyncio.run(delegate_subagent(
        task=task,
        preferred_model=model,
        preferred_harness=harness,
        timeout_sec=timeout
    ))

    if result.get("status") == "success":
        print("[+] Subagent completed successfully:")
        print(json.dumps(result["response"], indent=2) if isinstance(result["response"], dict) else result["response"])
    else:
        print(f"[-] Subagent finished with status: {result.get('status')}")
        print(json.dumps(result, indent=2))
        sys.exit(1 if result.get("status") == "error" else 0)

def main():
    parser = argparse.ArgumentParser(description="Reflex CLI Harness Federation & Gateway Manager")
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # Scan command
    scan_p = subparsers.add_parser("scan", help="Scan machine for installed agent harnesses")
    scan_p.set_defaults(func=cmd_scan)

    # Delegate command
    del_p = subparsers.add_parser("delegate", help="Delegate a subagent task to an installed CLI harness")
    del_p.add_argument("task", help="The prompt / instructions for the subagent")
    del_p.add_argument("-m", "--model", default="auto", help="Preferred model or capability (e.g. claude, gemini, o3-mini)")
    del_p.add_argument("-H", "--harness", default=None, help="Explicit harness override (claude, agy, opencode, goose, codex)")
    del_p.add_argument("-t", "--timeout", type=float, default=120.0, help="Execution timeout in seconds (default: 120)")
    del_p.set_defaults(func=cmd_delegate)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)

if __name__ == "__main__":
    main()
