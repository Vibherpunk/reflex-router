# Reflex Router (Rust Edition)

## Architecture Overview
Reflex Router is implemented in native Rust using **Axum 0.7 + Tokio**, binding to **Port 8787** (Port 8000 is strictly reserved for Reachy Mini hardware). 

### 1. High-Performance Concurrency
By moving to native Rust, we completely eliminate **Python GIL contention** which previously bottlenecked concurrent routing evaluations. We also eliminate **Starlette SSE premature 200 OK bugs**, where broken HTTP 200 frames or partial JSON deltas were sent to the agent before verifying upstream subscription health. Finally, moving to a strict typing model eliminates **regex version guessing** for provider payloads.

### 2. Dynamic Live Provider Catalog
The router serves a live, dynamically updated catalog of models at `/v1/models`. Models are sorted dynamically by an **epoch integer timestamp descending** and filtered by requested capabilities, ensuring the router consistently assigns the freshest, most capable model version to incoming agent requests.

### 3. The Metered Override Delta Rule
Reflex prevents Tier 2 (Deep Reasoning) and Tier 3 (Frontier Architecture) tasks from being artificially monopolized by "free" subscription models when a metered model is demonstrably better. 
If the metered model's fitness score exceeds the subscription model's fitness by **`Delta_fitness >= 0.15`**, Reflex will trigger a **Metered Override** and route to the paid API (e.g., DeepSeek-R1, Claude 3.7 Sonnet) to guarantee task success over local cost-saving constraints.

### 4. Shared Skidnir Host Gateway Topology
Reflex operates as a unified, process-level gateway on the host macOS daemon. Docker containers, specialized Sons of Anton subagents (Friday, Leo, PostBot), and VibeHard all share this single host daemon via `host.docker.internal:8787` (or the Docker bridge mesh). 

This unified topology allows:
- Global unified rate-limiting across all agents.
- Centralized 3-state circuit breakers.
- Subscription pooling across harnesses.
- Complete elimination of multi-container router bloat, avoiding duplicating API keys or token limits inside every client container.
