"""
Contextual System 1 Classifier (<10ms execution).
Evaluates prompts and previous conversation turns to assign compute tiers.
"""
import re
from typing import List, Dict, Any, Tuple, Optional
from memory import check_memory, record_incident
import scoring_spec

# Regex triggers for explicit and semantic overrides
TIER3_PATTERNS = [
    r"(?i)(?:^|\s)(--deep|#hard|#architect|#spec)(?:\s|$)",
    r"(?i)\b(system architecture|architecture|rfc|specification|database migration|distributed|consensus|byzantine|split-brain)\b",
    r"(?i)\b(zero[- ]downtime|formal verification|cryptographic audit|security vulnerability|threat model)\b"
]

TIER2_PATTERNS = [
    r"(?i)(?:^|\s)(#think|#reason)(?:\s|$)",
    r"(?i)\b(race condition|deadlock|concurrency|goroutine leak|memory leak|thread safety)\b",
    r"(?i)\b(segmentation fault|panic|null pointer|nullpointer|core dump|stack overflow)\b",
    r"(?i)\b(dynamic programming|backtracking|np-hard|algorithmic optimization|big-o)\b"
]

TIER1_PATTERNS = [
    r"(?i)\b(refactor|implement|create feature|add endpoint|unit test|write tests|build component)\b",
    r"(?i)\b(multi-file|component|module|service layer|controller|database model)\b"
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
    history_turns = int(scoring_spec.get_classification_param("history_check_turns", 4))
    for msg in reversed(messages[-history_turns:]):
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
            mem_threshold = float(scoring_spec.get_classification_param("memory_threshold", 0.55))
            mem_hit = check_memory(last_user_content, threshold=mem_threshold)
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

    # 5. Length heuristic: substantive implementation threshold
    word_threshold = int(scoring_spec.get_classification_param("word_count_tier1_threshold", 80))
    words = len(last_user_content.split())
    if words > word_threshold:
        return 1, f"Assigned Tier 1 by length ({words} words)"

    # 6. Default to Tier 0 (Fast / Cheap tool churn)
    return 0, "Default Tier 0: Routine query or tool-churn step"


def classify_domain(messages: List[Dict[str, Any]]) -> Optional[str]:
    """
    Classifies a conversation request into a domain specialization (legal, medical, finance, math).
    Matches against declarative keywords loaded from scoring_spec.yaml.
    Returns domain string or None if general.
    """
    if not messages:
        return None

    last_user_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_content = str(msg.get("content", ""))
            break

    if not last_user_content:
        return None

    domain_specs = scoring_spec.get_domain_specializations()
    text = last_user_content.lower()

    for domain_name, domain_info in domain_specs.items():
        keywords = domain_info.get("keywords", [])
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw.lower()) + r"\b", text):
                return domain_name

    return None


def classify_full_request(messages: List[Dict[str, Any]]) -> Tuple[int, str, Optional[str]]:
    """
    Classifies both compute tier and domain specialization.
    Returns (tier, explanation, domain).
    """
    tier, explanation = classify_request(messages)
    domain = classify_domain(messages)
    return tier, explanation, domain

