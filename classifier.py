"""
Contextual System 1 Classifier (<10ms execution).
Evaluates prompts and previous conversation turns to assign compute tiers.
"""
import re
from typing import List, Dict, Any, Tuple, Optional
from memory import check_memory, record_incident

# Regex triggers for explicit and semantic overrides
TIER3_PATTERNS = [
    r"(?i)(?:^|\s)(--deep|#hard|#architect|#spec)(?:\s|$)",
    r"(?i)\b(system architecture|rfc spec|database migration schema|distributed consensus|split-brain)\b",
    r"(?i)\b(zero-downtime|formal verification|cryptographic audit|security vulnerability)\b"
]

TIER2_PATTERNS = [
    r"(?i)(?:^|\s)(#think|#reason)(?:\s|$)",
    r"(?i)\b(race condition|deadlock|concurrency bug|goroutine leak|memory leak)\b",
    r"(?i)\b(segmentation fault|panic: runtime error|null pointer dereference)\b",
    r"(?i)\b(dynamic programming|backtracking|np-hard|algorithmic optimization)\b"
]

TIER1_PATTERNS = [
    r"(?i)\b(refactor|implement|create feature|add endpoint|unit test|write tests)\b",
    r"(?i)\b(multi-file|component|module|service layer|controller)\b"
]

# Error indicators in previous tool outputs that detect "Trojan Horse" bugs
TOOL_ERROR_PATTERNS = [
    r"(?i)(exit code [1-9]|exit status [1-9])",
    r"(?i)(assertionerror|traceback \(most recent call last\)|panic:)",
    r"(?i)(failed [1-9]\d* (test|spec)|failures?: [1-9])",
    r"(?i)(npm err!|error: command failed|syntaxerror:)"
]

def detect_tool_errors(messages: List[Dict[str, Any]]) -> bool:
    """Inspects recent tool or system outputs for failure signatures."""
    # Check the last 3 messages for execution errors
    for msg in reversed(messages[-4:]):
        content = str(msg.get("content", ""))
        for pattern in TOOL_ERROR_PATTERNS:
            if re.search(pattern, content):
                return True
    return False

def classify_request(messages: List[Dict[str, Any]]) -> Tuple[int, str]:
    """
    Classifies a conversation request into a compute tier (0-3).
    Returns (tier, explanation).
    """
    if not messages:
        return 0, "Empty request default"

    # Find last user prompt
    last_user_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_content = str(msg.get("content", ""))
            break

    # 1. Trojan Horse Check: Did the previous command or test fail?
    if detect_tool_errors(messages):
        # Autonomously record incident into System 1 memory
        if last_user_content:
            try:
                record_incident(
                    prompt=last_user_content,
                    failed_tier=0,
                    escalated_tier=2,
                    error_signature="Compiler/test error in preceding tool outputs"
                )
            except Exception:
                pass
        return 2, "Escalated to Tier 2: Detected test/compiler error in prior execution"

    # 2. System 1 Memory Check: Has a similar prompt or operational nuance failed before?
    if last_user_content:
        try:
            mem_hit = check_memory(last_user_content, threshold=0.55)
            if mem_hit:
                esc_tier = mem_hit["escalated_tier"]
                inc_id = mem_hit["incident_id"]
                sim = mem_hit["similarity"]
                sample_snippet = mem_hit["sample"][:35]
                return esc_tier, f"Memory Auto-Escalation (Incident #{inc_id}, sim={sim}): learned from prior failure in '{sample_snippet}'"
        except Exception:
            pass

    # 3. Check Tier 3 triggers (Frontier Architecture)
    for pattern in TIER3_PATTERNS:
        if re.search(pattern, last_user_content):
            return 3, f"Matched Tier 3 pattern: {pattern}"

    # 3. Check Tier 2 triggers (Deep Reasoning / Concurrency)
    for pattern in TIER2_PATTERNS:
        if re.search(pattern, last_user_content):
            return 2, f"Matched Tier 2 pattern: {pattern}"

    # 4. Check Tier 1 triggers (General Implementation / Feature writing)
    for pattern in TIER1_PATTERNS:
        if re.search(pattern, last_user_content):
            return 1, f"Matched Tier 1 pattern: {pattern}"

    # 5. Length heuristic: prompts with > 80 words are usually substantive implementation
    words = len(last_user_content.split())
    if words > 80:
        return 1, f"Assigned Tier 1 by length ({words} words)"

    # 6. Default to Tier 0 (Fast / Cheap tool churn)
    return 0, "Default Tier 0: Routine query or tool-churn step"
