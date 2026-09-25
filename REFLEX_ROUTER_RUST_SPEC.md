# Reflex Router Rust Architecture Specification (`reflex-router-rs`)

## 1. Audit Report of Python Architecture (`system1-router`)

### 1.1. Bottlenecks & Failure Modes
- **Synchronous GIL Locks:** The `ModelCatalog` (`catalog.py`) uses `threading.Lock()` (`self._refresh_lock`) and `sqlite3` without true asynchronous support. Under high concurrency, these locks block the entire event loop. `solver.py` uses `threading.Lock()` for `_affinity_lock`.
- **Regex Fragility (Version Guessing):** `catalog.py` uses massive, fragile regex patterns (e.g., `re.search(r"(?:^|[-_a-z])(?:v|version)?(\d+)[._-](\d+)(?![bB\d])", clean_base)`) to guess model generation, family, and tiers from strings. This breaks instantly when new nomenclature is introduced.
- **Starlette Streaming Response Bugs:** `server.py` attempts a speculative failover by holding the first chunk of a stream. However, Starlette's `StreamingResponse` can leak connections or prematurely commit `200 OK` headers if exceptions occur inside the generator, breaking SSE compliance.
- **Memory Bloat:** The Python FastAPI + SQLite + regex engine combination maintains a heavy memory footprint (~200MB+ RSS), completely missing the $<15\text{MB}$ invariant.
- **Routing Overhead:** Python execution time (5-15ms) violates the $<300\mu s$ budget.

---

## 2. Authoritative Rust Specification

### 2.1. Crate Structure & Dependencies (`Cargo.toml`)
```toml
[package]
name = "reflex-router-rs"
version = "3.0.0"
edition = "2021"

[dependencies]
axum = "0.7"
tokio = { version = "1", features = ["full"] }
hyper = { version = "1.0", features = ["full"] }
reqwest = { version = "0.12", features = ["json", "stream"] }
serde = { version = "1.0", features = ["derive"] }
serde_json = "1.0"
tracing = "0.1"
tracing-subscriber = "0.3"
clap = { version = "4.0", features = ["derive"] }
futures = "0.3"
dashmap = "5.5" # Zero-allocation concurrent maps
bytes = "1.5"
```

### 2.2. Port Constraints
- **Strict Network Bind:** The server MUST bind to `0.0.0.0:8787` or `127.0.0.1:8787`. Port 8000 is strictly forbidden (reserved for Reachy Mini hardware).

### 2.3. Core Data Structures (Zero Regex Parsing)
Instead of regex parsing, the new architecture consumes structured capabilities directly from upstream metadata (e.g., `/v1/models`).

```rust
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelMetadata {
    pub id: String,
    pub provider: String,
    pub created: u64, // Descending epoch timestamps for recency
    pub context_length: u32,
    pub tools_supported: bool,
    pub reasoning_supported: bool,
    pub tier: ComputeTier,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
pub enum ComputeTier {
    Base = 0,
    Implementation = 1,
    Reasoning = 2,
    Architecture = 3,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RoutingDecision {
    pub primary_model: String,
    pub primary_provider: String,
    pub fallback_model: Option<String>,
    pub fallback_provider: Option<String>,
    pub explanation: String,
}
```

### 2.4. Live Provider Catalog Engine
A background `tokio` task periodically polls providers. The catalog uses `std::sync::RwLock` or `DashMap` for zero-overhead concurrent reads.

```rust
use std::sync::Arc;
use tokio::sync::RwLock;
use std::collections::HashMap;

pub struct LiveCatalog {
    pub models: Arc<RwLock<HashMap<String, ModelMetadata>>>,
}

impl LiveCatalog {
    pub async fn background_poll(&self) {
        // Poll /v1/models from OpenRouter, Gemini, Anthropic
        // Update HashMap atomically
        // Sort by 'created' epoch integer timestamp for newest models
    }
}
```

### 2.5. Zero-Allocation Routing Decision Matcher
The arbitration solver must match prompt intent against capability filters instead of regex heuristics.

```rust
pub fn resolve_route(
    catalog: &LiveCatalog, 
    tools_required: bool, 
    reasoning_required: bool, 
    tier: ComputeTier
) -> RoutingDecision {
    // 1. Filter models by tools_required and reasoning_required
    // 2. Select subscription (zero-marginal-cost) models first
    // 3. Fallback to metered OpenRouter models
    // Allocation-free matching using iterators.
}
```

### 2.6. Streaming SSE Proxy Pipeline
Using `axum` and `reqwest` streaming, properly decoupled from downstream until the first chunk validates HTTP 200.

```rust
use axum::response::sse::{Event, Sse};
use futures::stream::Stream;

pub async fn proxy_stream(req: RequestPayload) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, StatusCode> {
    // 1. Issue Reqwest request to Upstream Provider
    // 2. Await Response Headers. If 429/5xx -> Failover immediately to metered route
    // 3. If 200 OK -> Convert to Axum SSE Stream
    // Prevents premature 200 OK headers from being sent to client before upstream verifies.
}
```

### 2.7. Configuration Schema (`scoring_spec.yaml` parity)
Load configuration into static `once_cell` or strictly typed `struct` structs at boot.

### 2.8. Test Plan
- **Unit Tests:** Direct execution of `resolve_route` against synthetic catalog states.
- **Integration Tests:** Mock HTTP upstream servers using `wiremock` to verify speculative failover and SSE formatting.
- **Load Tests:** `oha` or `wrk` targeting $10,000$ RPS against the router, verifying $<300\mu s$ latency and $<15\text{MB}$ memory overhead.
