"""
Dynamic Capability-to-Provider Arbitration Solver for Reflex.
Executes sub-5ms deterministic matching between conversation requirements and
live model catalog. Prioritizes $0.00 marginal-cost subscriptions with automatic
metered fallback when needed or when circuit breakers trip.
"""
import re
import time
import threading
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass
from catalog import ReflexModelDefinition, ModelCatalog
from circuit_breaker import BREAKER_REGISTRY, ensure_breaker
from classifier import detect_tool_errors, classify_domain, TIER3_PATTERNS, TIER2_PATTERNS, TIER1_PATTERNS
from memory import check_memory, record_incident
import scoring_spec

@dataclass
class CapabilityRequestVector:
    token_count: int               # L: Estimated token context length
    reasoning_depth: float         # R: [0.0, 1.0] Required reasoning depth
    tool_calling: bool             # T: Whether function calling is required
    architecture_score: float      # A: [0.0, 1.0] Required architecture score
    modality: str                  # M: "text", "vision", etc.
    tier_num: int                  # Equivalent tier (0-3) for telemetry
    explanation: str               # Human-readable rationale
    domain: Optional[str] = None   # Domain specialization (legal, medical, finance, math, etc.)

class ArbitrationSolver:
    def __init__(self, catalog: ModelCatalog):
        self.catalog = catalog
        self.session_affinity: Dict[str, Dict[str, Any]] = {}
        self._affinity_lock = threading.Lock()

    def extract_vector(self, messages: List[Dict[str, Any]], requested_model: str = "auto") -> CapabilityRequestVector:
        """Extracts capability requirements from message context in <2ms."""
        p0 = scoring_spec.get_tier_threshold_profile(0)
        p1 = scoring_spec.get_tier_threshold_profile(1)
        p2 = scoring_spec.get_tier_threshold_profile(2)
        p3 = scoring_spec.get_tier_threshold_profile(3)

        if not messages:
            return CapabilityRequestVector(
                token_count=0,
                reasoning_depth=p0.get("reasoning_depth", 0.10),
                tool_calling=False,
                architecture_score=p0.get("architecture_score", 0.20),
                modality="text",
                tier_num=0,
                explanation="Empty request default",
                domain=None
            )

        detected_domain = classify_domain(messages)
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        estimated_tokens = max(1, total_chars // 4)

        last_user_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_content = str(msg.get("content", ""))
                break

        # Check for tool calling requests or outputs
        history_turns = int(scoring_spec.get_classification_param("history_check_turns", 4))
        has_tools = False
        for msg in reversed(messages[-history_turns:]):
            if "tools" in msg or "tool_calls" in msg or msg.get("role") == "tool":
                has_tools = True
                break

        # 1. Trojan Horse Check: Preceding tool failures
        has_execution_error = detect_tool_errors(messages)
        if has_execution_error:
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
            return CapabilityRequestVector(
                token_count=estimated_tokens,
                reasoning_depth=p2.get("reasoning_depth", 0.90),
                tool_calling=has_tools,
                architecture_score=p2.get("architecture_score", 0.75),
                modality="text",
                tier_num=2,
                explanation="Escalated to Tier 2: Detected test/compiler error in prior execution",
                domain=detected_domain
            )

        # 2. System 1 Memory Check: Learned incidents
        if last_user_content:
            try:
                mem_threshold = float(scoring_spec.get_classification_param("memory_threshold", 0.55))
                mem_hit = check_memory(last_user_content, threshold=mem_threshold)
                if mem_hit:
                    esc_tier = mem_hit["escalated_tier"]
                    inc_id = mem_hit["incident_id"]
                    sim = mem_hit["similarity"]
                    sample_snippet = mem_hit["sample"][:35]
                    esc_defaults = scoring_spec.get_memory_escalation_defaults(esc_tier)
                    r_depth = esc_defaults.get("reasoning_depth", 0.95 if esc_tier >= 2 else 0.60)
                    a_score = esc_defaults.get("architecture_score", 0.95 if esc_tier == 3 else 0.70)
                    return CapabilityRequestVector(
                        token_count=estimated_tokens,
                        reasoning_depth=r_depth,
                        tool_calling=has_tools,
                        architecture_score=a_score,
                        modality="text",
                        tier_num=esc_tier,
                        explanation=f"Memory Auto-Escalation (Incident #{inc_id}, sim={sim}): learned from prior failure in '{sample_snippet}'",
                        domain=detected_domain
                    )
            except Exception:
                pass

        # 3. Frontier Architecture patterns (Tier 3)
        for pattern in TIER3_PATTERNS:
            if re.search(pattern, last_user_content):
                return CapabilityRequestVector(
                    token_count=estimated_tokens,
                    reasoning_depth=p3.get("reasoning_depth", 0.95),
                    tool_calling=has_tools,
                    architecture_score=p3.get("architecture_score", 0.95),
                    modality="text",
                    tier_num=3,
                    explanation=f"Matched Tier 3 Architecture pattern: {pattern}",
                    domain=detected_domain
                )

        # 4. Deep Reasoning / Concurrency patterns (Tier 2)
        for pattern in TIER2_PATTERNS:
            if re.search(pattern, last_user_content):
                return CapabilityRequestVector(
                    token_count=estimated_tokens,
                    reasoning_depth=p2.get("reasoning_depth", 0.90),
                    tool_calling=has_tools,
                    architecture_score=p2.get("architecture_score", 0.75),
                    modality="text",
                    tier_num=2,
                    explanation=f"Matched Tier 2 Concurrency pattern: {pattern}",
                    domain=detected_domain
                )

        # 5. General Implementation / Feature patterns (Tier 1)
        for pattern in TIER1_PATTERNS:
            if re.search(pattern, last_user_content):
                return CapabilityRequestVector(
                    token_count=estimated_tokens,
                    reasoning_depth=p1.get("reasoning_depth", 0.45),
                    tool_calling=has_tools,
                    architecture_score=p1.get("architecture_score", 0.50),
                    modality="text",
                    tier_num=1,
                    explanation=f"Matched Tier 1 Implementation pattern: {pattern}",
                    domain=detected_domain
                )

        # 6. Length heuristic: substantive implementation threshold
        word_threshold = int(scoring_spec.get_classification_param("word_count_tier1_threshold", 80))
        words = len(last_user_content.split())
        if words > word_threshold:
            return CapabilityRequestVector(
                token_count=estimated_tokens,
                reasoning_depth=p1.get("reasoning_depth", 0.45),
                tool_calling=has_tools,
                architecture_score=p1.get("architecture_score", 0.50),
                modality="text",
                tier_num=1,
                explanation=f"Assigned Tier 1 by length ({words} words)",
                domain=detected_domain
            )

        # 7. Default routine query / tool churn (Tier 0)
        return CapabilityRequestVector(
            token_count=estimated_tokens,
            reasoning_depth=p0.get("reasoning_depth", 0.10),
            tool_calling=has_tools,
            architecture_score=p0.get("architecture_score", 0.20),
            modality="text",
            tier_num=0,
            explanation="Default Tier 0: Routine query or tool-churn step",
            domain=detected_domain
        )

    def arbitrate(
        self,
        vector: CapabilityRequestVector,
        session_id: str,
        requested_model: str = "auto",
        preferred_access_method: Optional[str] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        """
        Arbitrates requests across dynamically discovered models:
        1. Exact requested_model match if explicitly specified.
        2. KV-cache latching if tokens > 20,000 and previous primary is healthy.
        3. Prioritizes zero-marginal-cost subscriptions matching required capability.
        4. Selects optimal metered fallback (with circuit breaker check).
        Returns: (primary_route, metered_fallback_route, explanation)
        """
        models = self.catalog.list_all()
        if not models:
            raise RuntimeError("Reflex Model Catalog is empty. Please run discovery.")

        # 1. Handle explicit model request (Exact match or canonical alias)
        if requested_model and requested_model != "auto":
            # Match exact model ID
            matches = [m for m in models if m.id == requested_model]
            if not matches:
                # Try matching by display_name or provider/model syntax
                matches = [m for m in models if f"{m.provider}/{m.id}" == requested_model or m.display_name.lower() == requested_model.lower()]
            if matches:
                chosen = matches[0]
                route = {
                    "provider": chosen.provider,
                    "model": chosen.id,
                    "access_method": chosen.access_method,
                    "billing_type": chosen.billing_type
                }
                return route, route, f"Explicit model override: {chosen.id} ({chosen.provider})"
            else:
                raise ValueError(f"Requested model '{requested_model}' not found in active model catalog")

        # 2. KV-Cache Context Latching (> 20,000 tokens)
        if vector.token_count > 20_000:
            with self._affinity_lock:
                if session_id in self.session_affinity:
                    latched = self.session_affinity[session_id]
                    breaker = ensure_breaker(latched["primary"]["provider"])
                    if breaker.is_healthy():
                        return (
                            latched["primary"],
                            latched["fallback"],
                            f"KV-Cache Latch locked to {latched['primary']['model']} ({vector.token_count} tokens)"
                        )

        # 3. Filter candidates by context length, tool support, access method, and circuit health
        eligible_sub: List[Tuple[float, ReflexModelDefinition]] = []
        eligible_metered: List[Tuple[float, ReflexModelDefinition]] = []

        for m in models:
            if m.context_window < vector.token_count:
                continue
            if vector.tool_calling and not m.tool_calling:
                continue
            if preferred_access_method and m.access_method != preferred_access_method:
                continue

            breaker = ensure_breaker(m.provider)
            is_healthy = breaker.is_healthy()

            # Dynamic fitness score tailored to compute tier via declarative spec
            weights = scoring_spec.get_arbitration_weights(vector.tier_num)
            fitness = (
                (m.speed_score * weights.get("speed", 0.0)) +
                (m.coding_score * weights.get("coding", 0.0)) +
                (m.reasoning_capability * weights.get("reasoning", 0.0)) +
                (m.architecture_score * weights.get("architecture", 0.0))
            )

            # Domain Specialization Boost (declarative via scoring_spec.yaml)
            if vector.domain and m.domain_specialization == vector.domain:
                fitness += scoring_spec.get_domain_boost(vector.domain)

            if m.billing_type == "subscription":
                if is_healthy:
                    eligible_sub.append((fitness, m))
            else:
                # Metered providers (e.g. OpenRouter)
                if is_healthy:
                    # Penalize cost slightly to prefer cost-effective metered options
                    cost_divisor = scoring_spec.get_cost_penalty_divisor()
                    cost_penalty = (m.input_cost_per_m + m.output_cost_per_m) / cost_divisor
                    eligible_metered.append((fitness - cost_penalty, m))

        # Sort descending by fitness, breaking ties with generation and reasoning capability
        eligible_sub.sort(key=lambda x: (x[0], x[1].generation if x[1].generation is not None else 0.0, x[1].reasoning_capability), reverse=True)
        eligible_metered.sort(key=lambda x: (x[0], x[1].generation if x[1].generation is not None else 0.0, x[1].reasoning_capability), reverse=True)

        # 4. Emergency Fallback: If no providers met constraints, find healthy emergency models
        if not eligible_sub and not eligible_metered:
            healthy_emergency = [
                m for m in models
                if m.context_window >= vector.token_count and ensure_breaker(m.provider).is_healthy()
            ]
            if healthy_emergency:
                best = max(healthy_emergency, key=lambda x: x.reasoning_capability)
            else:
                best = max(models, key=lambda x: x.context_window)

            route = {
                "provider": best.provider,
                "model": best.id,
                "access_method": best.access_method,
                "billing_type": best.billing_type
            }
            return route, route, f"Emergency fallback: selected {best.id} ({best.provider})"

        # 5. Route selection with Dynamic Metered Override
        best_sub = eligible_sub[0] if eligible_sub else None
        best_metered = eligible_metered[0] if eligible_metered else None

        # Dynamic Metered Override Threshold (Fixing Subscription Monopoly)
        # Subscriptions win for routine tasks (Tier 0/1). For complex reasoning / architecture (Tier 2/3),
        # a metered frontier model overrides subscription if fitness delta > override_threshold.
        # Purpose-driven domain models override generalists if fitness delta > domain_override_threshold.
        override_threshold = scoring_spec.get_fitness_override_threshold()
        domain_override_threshold = scoring_spec.get_domain_override_threshold()

        metered_override = False
        domain_override = False

        if best_sub and best_metered:
            is_domain_specialist_metered = bool(
                vector.domain and
                best_metered[1].domain_specialization == vector.domain and
                best_sub[1].domain_specialization != vector.domain
            )
            if is_domain_specialist_metered and (best_metered[0] - best_sub[0]) > domain_override_threshold:
                primary_model = best_metered[1]
                metered_override = True
                domain_override = True
            elif vector.tier_num >= 2 and (best_metered[0] - best_sub[0]) > override_threshold:
                primary_model = best_metered[1]
                metered_override = True
            else:
                primary_model = best_sub[1]
        elif best_sub:
            primary_model = best_sub[1]
        elif best_metered:
            primary_model = best_metered[1]
        else:
            primary_model = eligible_metered[0][1] if eligible_metered else eligible_sub[0][1]

        fallback_model = eligible_metered[0][1] if eligible_metered else primary_model

        primary_route = {
            "provider": primary_model.provider,
            "model": primary_model.id,
            "access_method": primary_model.access_method,
            "billing_type": primary_model.billing_type
        }
        metered_route = {
            "provider": fallback_model.provider,
            "model": fallback_model.id,
            "access_method": fallback_model.access_method,
            "billing_type": fallback_model.billing_type
        }

        # Store session affinity safely
        with self._affinity_lock:
            self.session_affinity[session_id] = {
                "primary": primary_route,
                "fallback": metered_route,
                "last_used": time.time()
            }

        if domain_override:
            primary_label = f"Primary Metered: {primary_model.id} ({primary_model.provider})"
            explanation_suffix = f" (Domain specialist override for '{vector.domain}')"
        elif metered_override:
            primary_label = f"Primary Metered: {primary_model.id} ({primary_model.provider})"
            explanation_suffix = " (Metered override due to high capability delta)"
        elif primary_model.billing_type == "subscription":
            primary_label = f"Primary Sub: {primary_model.id} ({primary_model.provider})"
            explanation_suffix = ""
        else:
            primary_label = f"Primary Metered: {primary_model.id} ({primary_model.provider})"
            explanation_suffix = ""

        domain_info = f", Domain={vector.domain}" if vector.domain else ""
        explanation = (
            f"Dynamic Match (Req: R={vector.reasoning_depth:.2f}, A={vector.architecture_score:.2f}{domain_info}) -> "
            f"{primary_label}, "
            f"Fallback: {fallback_model.id} ({fallback_model.provider}) [{vector.explanation}]{explanation_suffix}"
        )
        return primary_route, metered_route, explanation
