"""
30-Prompt Adversarial Tier Classification & Routing Benchmark.
Re-audit validation for Reflex Router v2.2.0.
Verifies 0% False Negative Rate for Tier 3 prompts.
"""
import time
from classifier import classify_request, classify_full_request
from solver import ArbitrationSolver
from catalog import ModelCatalog

BENCHMARK_PROMPTS = [
    # Tier 0 Routine Prompts
    {"id": "T0-01", "prompt": "git status", "expected_tier": 0},
    {"id": "T0-02", "prompt": "ls -la /tmp", "expected_tier": 0},
    {"id": "T0-03", "prompt": "what is the current time in UTC?", "expected_tier": 0},
    {"id": "T0-04", "prompt": "curl -I https://example.com", "expected_tier": 0},
    {"id": "T0-05", "prompt": "echo $PATH", "expected_tier": 0},
    {"id": "T0-06", "prompt": "cat package.json grep version", "expected_tier": 0},
    {"id": "T0-07", "prompt": "whoami", "expected_tier": 0},

    # Tier 1 Implementation Prompts
    {"id": "T1-01", "prompt": "Implement a React button component with secondary styles and disabled state", "expected_tier": 1},
    {"id": "T1-02", "prompt": "Refactor the user profile settings form across multiple files and clean up hooks", "expected_tier": 1},
    {"id": "T1-03", "prompt": "Create feature for exporting invoices to CSV and add controller endpoint", "expected_tier": 1},
    {"id": "T1-04", "prompt": "Write unit tests for the email notification service and verify mock calls", "expected_tier": 1},
    {"id": "T1-05", "prompt": "Add endpoint /v1/webhooks/stripe to Express router with signature validation", "expected_tier": 1},
    {"id": "T1-06", "prompt": "Build component for file upload preview with progress bar and cancel button", "expected_tier": 1},
    {"id": "T1-07", "prompt": "Refactor database model for user preferences and update schema relationships", "expected_tier": 1},
    {"id": "T1-08", "prompt": "We need to expand our backend API to support paginated search across user directory records with filtering by organization, department, and role status.", "expected_tier": 1},

    # Tier 2 Deep Reasoning Prompts
    {"id": "T2-01", "prompt": "Debug this subtle race condition where two threads acquire locks in inconsistent order", "expected_tier": 2},
    {"id": "T2-02", "prompt": "Fix goroutine leak in the worker pool when channels close unexpectedly during cancellation", "expected_tier": 2},
    {"id": "T2-03", "prompt": "Investigate segmentation fault in the C foreign function interface when passing raw pointers", "expected_tier": 2},
    {"id": "T2-04", "prompt": "Implement dynamic programming solution for bounded knapsack with 2D memoization table", "expected_tier": 2},
    {"id": "T2-05", "prompt": "Solve backtracking graph coloring with NP-hard constraints and branch-and-bound pruning", "expected_tier": 2},
    {"id": "T2-06", "prompt": "Fix null pointer dereference panic under high thread concurrency in session cache", "expected_tier": 2},
    {"id": "T2-07", "prompt": "Optimize Big-O complexity of the nearest neighbor search from quadratic to logarithmic", "expected_tier": 2},
    {"id": "T2-08", "prompt": "#reason Analyze this core dump from an out-of-memory crash in production", "expected_tier": 2},

    # Tier 3 Frontier Architecture Prompts (Target of P0-1 Audit)
    {"id": "T3-01", "prompt": "Compile formal system architecture and distributed consensus specification for Multi-Paxos cluster", "expected_tier": 3},
    {"id": "T3-02", "prompt": "Design zero-downtime database migration strategy for 50M records with dual-write shadow verification", "expected_tier": 3},
    {"id": "T3-03", "prompt": "Perform cryptographic audit and threat model on MPC threshold signature protocol", "expected_tier": 3},
    {"id": "T3-04", "prompt": "Review master services agreement indemnification clause under Delaware law for liability cap exclusions", "expected_tier": 3},
    {"id": "T3-05", "prompt": "Reconcile Oregon DELC ERDC subsidy double-entry general ledger records for child care billing audit", "expected_tier": 3},
    {"id": "T3-06", "prompt": "Execute blast radius level 3 statutory compliance filing and purge user records under subpoena", "expected_tier": 3},
    {"id": "T3-07", "prompt": "#architect Formulate RFC specification for Byzantine consensus engine with formal safety invariants", "expected_tier": 3},
]

def run_benchmark():
    cat = ModelCatalog()
    solver = ArbitrationSolver(cat)

    print("=" * 115)
    print(f"{'ID':<6} | {'Prompt Preview':<40} | {'Exp':<4} | {'Act':<4} | {'Match':<6} | {'Latency':<8} | {'Selected Model':<28} | {'Reason'}")
    print("=" * 115)

    tier3_total = 0
    tier3_false_negatives = 0
    total_matches = 0

    for item in BENCHMARK_PROMPTS:
        pid = item["id"]
        prompt = item["prompt"]
        exp_tier = item["expected_tier"]
        messages = [{"role": "user", "content": prompt}]

        t0 = time.perf_counter()
        act_tier, reason, domain = classify_full_request(messages)
        vec = solver.extract_vector(messages)
        primary_route, _, arbitration_reason = solver.arbitrate(vec, session_id="benchmark")
        latency_ms = (time.perf_counter() - t0) * 1000

        matched = (act_tier == exp_tier)
        match_str = "PASS" if matched else "FAIL"
        if matched:
            total_matches += 1

        if exp_tier == 3:
            tier3_total += 1
            if act_tier != 3:
                tier3_false_negatives += 1
                match_str = "CRIT_FAIL"

        preview = (prompt[:38] + "..") if len(prompt) > 40 else prompt
        selected_model = f"{primary_route['provider']}/{primary_route['model']}"
        if len(selected_model) > 28:
            selected_model = selected_model[:26] + ".."

        print(f"{pid:<6} | {preview:<40} | T{exp_tier:<3} | T{act_tier:<3} | {match_str:<6} | {latency_ms:6.2f}ms | {selected_model:<28} | {reason[:30]}")

    print("=" * 115)
    t3_fn_rate = (tier3_false_negatives / tier3_total) * 100 if tier3_total else 0.0
    accuracy = (total_matches / len(BENCHMARK_PROMPTS)) * 100

    print(f"\nBENCHMARK RESULTS SUMMARY:")
    print(f"Total Prompts Tested         : {len(BENCHMARK_PROMPTS)}")
    print(f"Exact Tier Matches           : {total_matches} / {len(BENCHMARK_PROMPTS)} ({accuracy:.1f}%)")
    print(f"Tier 3 Total Prompts         : {tier3_total}")
    print(f"Tier 3 False Negatives       : {tier3_false_negatives}")
    print(f"Tier 3 False Negative Rate   : {t3_fn_rate:.1f}% (TARGET: 0.0%)")

    assert tier3_false_negatives == 0, f"CRITICAL: Tier 3 false negative rate is {t3_fn_rate:.1f}% (expected 0.0%)"
    print("\nVERIFICATION PASSED: Tier 3 False Negative Rate is strictly 0.0%!")

if __name__ == "__main__":
    run_benchmark()
