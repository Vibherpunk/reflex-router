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

def cmd_catalog_audit(args):
    """Audits the dynamic model catalog, displaying semantic generations, tiers, capabilities, and pricing."""
    from catalog import ModelCatalog
    cat = ModelCatalog()
    if getattr(args, "refresh", False):
        print("[*] Refreshing live models from providers...")
        cat.refresh_from_providers(force=True)
    elif getattr(args, "rescore", False):
        print("[*] Re-scoring cached models via dynamic heuristic engine...")
        cat.rescore_all()

    models = cat.list_all()
    if not models:
        print("[-] Reflex Model Catalog is empty. Run with --refresh or discover models first.")
        return

    provider_filter = getattr(args, "provider", None)
    if provider_filter:
        models = [m for m in models if m.provider.lower() == provider_filter.lower()]
        if not models:
            print(f"[-] No models found for provider '{provider_filter}'.")
            return

    # Sort order
    sort_key = getattr(args, "sort", "r_cap")
    if sort_key == "r_cap":
        models.sort(
            key=lambda m: (
                m.reasoning_capability,
                m.generation if m.generation is not None else 0.0,
                m.architecture_score,
                m.coding_score,
            ),
            reverse=True,
        )
    elif sort_key == "a_score":
        models.sort(
            key=lambda m: (
                m.architecture_score,
                m.generation if m.generation is not None else 0.0,
                m.reasoning_capability,
            ),
            reverse=True,
        )
    elif sort_key == "c_score":
        models.sort(
            key=lambda m: (
                m.coding_score,
                m.generation if m.generation is not None else 0.0,
                m.reasoning_capability,
            ),
            reverse=True,
        )
    elif sort_key == "cost":
        models.sort(key=lambda m: (m.input_cost_per_m + m.output_cost_per_m))
    elif sort_key == "provider":
        models.sort(key=lambda m: (m.provider, m.id))
    elif sort_key == "name":
        models.sort(key=lambda m: m.id)

    limit = getattr(args, "limit", None)
    if limit and limit > 0:
        models = models[:limit]

    sub_count = sum(1 for m in models if m.billing_type == "subscription")
    metered_count = sum(1 for m in models if m.billing_type != "subscription")

    if getattr(args, "json", False):
        out = [{
            "id": m.id,
            "provider": m.provider,
            "generation": m.generation,
            "tier": m.tier,
            "context_window": m.context_window,
            "billing_type": m.billing_type,
            "input_cost_per_m": m.input_cost_per_m,
            "output_cost_per_m": m.output_cost_per_m,
            "reasoning_capability": m.reasoning_capability,
            "architecture_score": m.architecture_score,
            "coding_score": m.coding_score,
            "speed_score": m.speed_score
        } for m in models]
        print(json.dumps(out, indent=2))
        return

    # ANSI styling
    GREEN = "\033[32m"
    BOLD_GREEN = "\033[1;32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    # Dynamic column widths
    max_id_len = max(27, min(36, max(len(m.id) for m in models)))
    max_prov_len = max(10, min(14, max(len(m.provider) for m in models)))

    print("Reflex Dynamic Model Catalog Audit\n")
    header = (
        f"{BOLD}{'ID':<{max_id_len}}{RESET} | "
        f"{BOLD}{'Provider':<{max_prov_len}}{RESET} | "
        f"{BOLD}{'Gen':^4}{RESET} | "
        f"{BOLD}{'Tier':<8}{RESET} | "
        f"{BOLD}{'Ctx Window':>11}{RESET} | "
        f"{BOLD}{'Cost ($/M)':<11}{RESET} | "
        f"{BOLD}{'R-Cap':>5}{RESET} | "
        f"{BOLD}{'A-Score':>7}{RESET} | "
        f"{BOLD}{'C-Score':>7}{RESET}"
    )
    divider = "-" * (max_id_len + max_prov_len + 4 + 8 + 11 + 11 + 5 + 7 + 7 + 24)
    print(header)
    print(divider)

    for m in models:
        gen_str = f"{m.generation:.1f}" if m.generation is not None else "N/A"
        if m.billing_type == "subscription":
            cost_styled = f"{BOLD_GREEN}$0.00 (S){RESET}{' ' * 2}"
        else:
            avg_cost = (m.input_cost_per_m + m.output_cost_per_m) / 2.0
            if avg_cost < 0:
                cost_str = "$0.00 (M)"
            else:
                cost_str = f"${avg_cost:.2f} (M)"
            cost_styled = f"{YELLOW}{cost_str}{RESET}"
            raw_len = len(cost_str)
            if raw_len < 11:
                cost_styled += " " * (11 - raw_len)

        disp_id = m.id if len(m.id) <= max_id_len else m.id[:max_id_len - 2] + ".."

        row_str = (
            f"{CYAN}{disp_id:<{max_id_len}}{RESET} | "
            f"{m.provider:<{max_prov_len}} | "
            f"{YELLOW}{gen_str:^4}{RESET} | "
            f"{m.tier:<8} | "
            f"{m.context_window:>11,d} | "
            f"{cost_styled} | "
            f"{m.reasoning_capability:>5.2f} | "
            f"{m.architecture_score:>7.2f} | "
            f"{m.coding_score:>7.2f}"
        )
        print(row_str)

    print(f"\n{BOLD}Total Models: {len(models)}{RESET} | ({GREEN}S{RESET}) Subscription: {sub_count} / ({YELLOW}M{RESET}) Metered: {metered_count}")


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

    # Catalog command and subcommands
    cat_p = subparsers.add_parser("catalog", help="Model catalog management and capability inspection")
    cat_sub = cat_p.add_subparsers(dest="catalog_subcommand", help="Catalog subcommands")
    audit_p = cat_sub.add_parser("audit", help="Display formatted dynamic capability audit table")
    for p in (cat_p, audit_p):
        p.add_argument("--refresh", action="store_true", help="Force refresh models from live providers")
        p.add_argument("--rescore", action="store_true", help="Re-score cached models with dynamic heuristic engine")
        p.add_argument("--sort", choices=["r_cap", "a_score", "c_score", "cost", "provider", "name"], default="r_cap", help="Sort order (default: r_cap)")
        p.add_argument("--provider", default=None, help="Filter by provider name")
        p.add_argument("--limit", type=int, default=None, help="Limit number of displayed models")
        p.add_argument("--json", action="store_true", help="Output audit results in JSON format")
        p.set_defaults(func=cmd_catalog_audit)

    # Direct 'audit' alias
    alias_audit = subparsers.add_parser("audit", help="Alias for 'catalog audit'")
    alias_audit.add_argument("--refresh", action="store_true", help="Force refresh models from live providers")
    alias_audit.add_argument("--rescore", action="store_true", help="Re-score cached models with dynamic heuristic engine")
    alias_audit.add_argument("--sort", choices=["r_cap", "a_score", "c_score", "cost", "provider", "name"], default="r_cap", help="Sort order (default: r_cap)")
    alias_audit.add_argument("--provider", default=None, help="Filter by provider name")
    alias_audit.add_argument("--limit", type=int, default=None, help="Limit number of displayed models")
    alias_audit.add_argument("--json", action="store_true", help="Output audit results in JSON format")
    alias_audit.set_defaults(func=cmd_catalog_audit)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)

    args.func(args)

if __name__ == "__main__":
    main()
