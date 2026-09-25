# Reflex Multi-Provider System 1 Intelligent Gateway (v2.1.0)

A high-performance local reverse-proxy router (`http://127.0.0.1:8787/v1`) designed specifically for **agentic coding harnesses** (Goose, OpenCode, Antigravity, Aider) operating across **flat-rate developer subscriptions** and **metered pay-as-you-go APIs**.

Reflex evaluates incoming requests in sub-millisecond System 1 time, enforces **Zero-Marginal-Cost Subscriptions First**, protects multi-turn KV-caches, prevents socket disconnects on long-thinking reasoning models, and learns from prior execution errors using local incident memory.

---

## 1. Architectural Economics: The Marginal Cost Waterfall

Every LLM backend on your machine falls into one of two economic tiers:

```
                            Incoming Agent Request
                         (e.g., reflex/auto from Goose / OpenCode)
                                     │
                                     ▼
                      ┌─────────────────────────────┐
                      │  Reflex System 1 Classifier │
                      │  - Intent / Complexity      │
                      │  - Skidnir Incident Memory  │
                      └──────────────┬──────────────┘
                                     │ Identifies Required Tier & Capability
                                     ▼
                      ┌─────────────────────────────┐
                      │  Cost & Capability Router   │
                      └──────────────┬──────────────┘
                                     │
            ┌────────────────────────┴────────────────────────┐
            ▼                                                 ▼
┌─────────────────────────────────┐               ┌─────────────────────────────────┐
│     ACTIVE SUBSCRIPTIONS        │               │       PAY-AS-YOU-GO POOL        │
│   (Marginal Cost: $0.00/token)  │               │   (Marginal Cost: > $0.00/token)│
│ - OpenCode-Go (zen/go/v1)       │               │ - OpenRouter (api/v1)           │
│ - Google Gemini (v1beta/openai) │               │ - Anthropic Direct              │
│ - Claude Web/Team Bridge        │               │ - OpenAI Direct                 │
└────────────────┬────────────────┘               └─────────────────▲───────────────┘
                 │                                                  │
                 ├── 200 OK & First Chunk Verified ─────────────────┤
                 │   Stream response smoothly to client             │
                 │                                                  │
                 └── 429 Rate Limit / 503 Outage? ──────────────────┘
                     Zero-Byte Speculative Pre-Flight catches it.
                     Trips 3-State Canary Circuit Breaker.
                     Seamlessly retries on Metered backstop.
                     ZERO bytes lost. ZERO errors seen by agent.
```

### The Invariants:
1. **Capability Matches First:** If a task specifically requires a model exclusive to metered APIs (e.g. `anthropic/claude-3.7-sonnet` or `openai/o3-mini`), Reflex routes directly to the metered API. It never downgrades an agent to an inadequate model.
2. **Zero-Marginal-Cost Subscriptions First:** If an equivalent model or tier capability is available on an active subscription, Reflex **always defaults to the subscription ($0.00 marginal cost)**.
3. **Seamless Metered Overflow:** Metered APIs serve as warm failover backstops. If a subscription backend returns HTTP `429` (Rate Limited) or `503` (Outage), Reflex fails over instantly to metered without corrupting the downstream stream.

---

## 2. Core Architectural Pillars

### A. True Zero-Byte Speculative Pre-Flight
In Python ASGI servers (Starlette / FastAPI), returning a `StreamingResponse` commits `HTTP 200 OK` downstream **before** the generator yield loop begins. If an upstream subscription returns 429 after stream initialization, the gateway cannot change the HTTP status code.

**Reflex's Defense:**
Reflex executes a pre-flight probe: it opens the upstream HTTP stream, inspects the status code, and reads the first data chunk into memory **before committing downstream headers**. If upstream returns 429/503:
- Zero bytes have been sent to the agent.
- Reflex cleanly closes the subscription connection.
- It trips the circuit breaker and opens a connection to the metered backend.
- The agent harness never receives broken HTTP 200 frames or partial JSON deltas.

### B. 3-State Canary Circuit Breaker (Anti-Stampede)
Static 60-second cooldowns create a "recovery cliff" where all concurrent requests wake up simultaneously and re-trip the rate limiter.

Reflex implements a formal 3-State Finite State Machine (`CLOSED`, `OPEN`, `HALF_OPEN`):
- **`CLOSED`:** Normal operation. Requests route to subscription.
- **`OPEN`:** Subscription rate-limited. Requests skip the subscription entirely and route directly to metered.
- **`HALF_OPEN`:** When cooldown expires, Reflex allows **exactly ONE single canary probe request** to test the subscription. All other concurrent requests continue routing to metered.
  - If the canary succeeds $\rightarrow$ Breaker resets to `CLOSED`.
  - If the canary fails $\rightarrow$ Breaker re-opens with exponential backoff and randomized jitter:
    $$T_{\text{cooldown}} = \min(T_{\text{max}}, T_{\text{base}} \times 2^{\text{failures}}) + \text{Uniform}(0, \text{jitter})$$

### C. Contextual "Trojan Horse" Bug Escalation
Standard routers inspect prompts in isolation. A prompt like `"Fix it."` (2 words) is naively routed to a fast, cheap model that fails.

Reflex inspects previous turns in the conversation history. If any recent turn contains compiler or test failures (`exit code 1`, `AssertionError`, `Traceback`, `panic:`), Reflex automatically **escalates the 2-word prompt to Tier 2 (Deep Reasoning)**.

### D. Skidnir Recursive Incident Memory (`memory.py`)
Reflex maintains an embedded, zero-latency (< 1ms) SQLite incident database (`~/.reflex/memory.sqlite3`).
- When a tool execution error occurs or feedback is submitted via `/v1/feedback`, Reflex records the prompt shingles (unigrams & bigrams).
- On future requests, Reflex runs an online Jaccard similarity check.
- If a prompt matches a prior failure domain ($\ge 0.55$ similarity), Reflex **auto-escalates the tier** before heuristics fire.

### E. Reasoning Socket Keep-Alive (SSE Heartbeats)
Frontier reasoning models (DeepSeek-R1, o3-mini, Kimi-k3) often think for 30–60 seconds before generating their first visible token. Coding harnesses enforce strict 30s read timeouts and drop the connection.

Reflex's streaming engine injects synthetic `: ping\n\n` SSE comment frames every 3 seconds during thinking pauses, keeping the client TCP socket alive.

### F. Sticky-Session KV-Cache Latching
When a multi-turn conversation exceeds 20,000 tokens, Reflex locks the model family to preserve provider-side KV prompt caching, avoiding the 10x cost penalty of cache thrashing.

### G. CLI Harness Federation (Process-Level Subagent Arbitrage)
When an agent in one harness (e.g. Claude Code) needs a subagent specialized in another model (e.g. Gemini Ultra or DeepSeek-V4), Reflex eliminates the need for reverse-engineered web session cookies or separate paid API keys.

Reflex auto-discovers and delegates directly to the native CLIs installed on your Mac:
* **`claude` (Claude Code):** Uses your native Claude Pro/Teams subscription.
* **`agy` (Antigravity):** Uses your native Google Gemini Ultra/Advanced subscription.
* **`opencode` (OpenCode):** Uses your flat OpenCode-Go subscription.
* **`goose` (Goose):** Uses your local Goose agent profiles.
* **`codex` (Codex CLI):** Uses your native OpenAI subscription.

#### Defensive Protections in Federation:
1. **Headless Execution Matrix:** Enforces strict non-interactive flags (`--dangerously-skip-permissions`, `--auto`, `--no-session`, `TERM=dumb`) so child processes never freeze waiting for human TTY approvals.
2. **Process Group Containment (`os.setsid`):** Spawns child harnesses in detached process groups and terminates all descendants with `os.killpg` on timeout, preventing zombie compiler tasks.
3. **Recursion Limit & Cycle Guard:** Enforces max depth of 2 hops (`REFLEX_DELEGATION_DEPTH <= 2`) and prevents ping-pong loops (`A -> B -> A`) via execution chain tracking.

---

## 3. Verified Compute Tiers

| Tier | Category | Subscription Model ($0.00) | Metered Fallback | Primary Use Cases |
| :---: | :--- | :--- | :--- | :--- |
| **0** | **Fast / Tool Churn** | `deepseek-v4-flash` | `deepseek/deepseek-chat` | Reading files, grepping, status checks, formatters. |
| **1** | **General Implementation** | `qwen3.7-plus` | `deepseek/deepseek-chat` | Writing features, adding endpoints, multi-file refactors. |
| **2** | **Deep Reasoning & Bugfix** | `deepseek-v4-pro` | `deepseek/deepseek-r1` | Concurrency bugs, memory leaks, test failures (`exit code 1`). |
| **3** | **Frontier Architecture** | `qwen3.7-max` | `anthropic/claude-3.7-sonnet` | System architecture, RFC specs, security audits, `#hard`. |

---

## 4. API & CLI Endpoints

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/health` | `GET` | Reports provider status, API key health, and circuit breakers. |
| `/v1/models` | `GET` | OpenAI-compatible model listing (`reflex/auto` + catalog). |
| `/v1/chat/completions` | `POST` | Core OpenAI-compatible streaming & non-streaming completions endpoint. |
| `/v1/feedback` | `POST` | Ingests execution feedback to train Skidnir System 1 incident memory. |
| `/v1/stats` | `GET` | Returns incident counts, tier request distributions, and circuit breaker states. |
| `/v1/harnesses` | `GET` | Auto-discovers all installed & authenticated CLI agent harnesses on host. |
| `/v1/delegate` | `POST` | Spawns a subagent in an installed CLI harness (e.g. `agy`, `claude`, `opencode`). |

### CLI Tools
```bash
# Scan installed agent harnesses and subscriptions
./reflex.py scan

# Delegate a subagent task to an auto-matched harness
./reflex.py delegate "Refactor authentication flow in auth.py" --model claude
```

---

## 5. Quickstart & Harness Setup

### Auto-Installation
Run the automated installer to wire Reflex into OpenCode and Goose:
```bash
python3 install.py
```

### Manual Configuration

#### OpenCode (`~/.config/opencode/opencode.json`)
```json
{
  "provider": {
    "reflex": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Reflex System 1 Router",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "local-key"
      },
      "models": {
        "auto": {
          "id": "auto",
          "name": "Reflex Intelligent Auto"
        }
      }
    }
  },
  "model": "reflex/auto"
}
```

#### Goose (`~/.config/goose/custom_providers/reflex.json`)
```json
{
  "name": "reflex",
  "engine": "openai",
  "display_name": "Reflex Router",
  "base_url": "http://127.0.0.1:8787/v1",
  "models": [
    { "name": "auto", "context_limit": 128000, "reasoning": true }
  ]
}
```

---

## 6. Running as a macOS Daemon (`launchd`)

Reflex is installed as a native macOS user daemon at:
`~/Library/LaunchAgents/com.reflex.router.plist`

- **Check status:**
  ```bash
  curl -s http://127.0.0.1:8787/health | jq .
  ```
- **Restart service:**
  ```bash
  launchctl kickstart -k gui/$(id -u)/com.reflex.router
  ```
- **Inspect live logs:**
  ```bash
  tail -f ~/workspace/system1-router/reflex.log
  ```

---

## 7. Automated Test Suite

Reflex includes an automated test suite covering classification, memory escalation, 3-state circuit breaking, and protocol safety:
```bash
python3 -m pytest tests/ -v
```
All tests run locally in under 0.5s with zero external network dependencies.

## Reflex Native Rust Architecture (reflex-router-rs)

The core routing engine has been ported to native Rust using **Axum 0.7 + Tokio** and listens on **Port 8787** (Note: Port 8000 is strictly reserved for Reachy Mini hardware).

### Key Architectural Upgrades
- **Zero GIL Contention:** Moving to Rust eliminates Python Global Interpreter Lock (GIL) contention.
- **SSE Stream Integrity:** Solves the Starlette SSE premature 200 OK bug. The Rust gateway guarantees that HTTP headers are not committed downstream until upstream provider health and stream initiation are confirmed.
- **Strict Parsing:** Replaces fragile regex version guessing with strict Serde-driven type parsing.

### Dynamic Live Provider Catalog
The `/v1/models` endpoint leverages a Dynamic Live Provider Catalog. Models are sorted by **epoch integer timestamp descending** and dynamically filtered by requested capabilities, ensuring agents always receive the most up-to-date inference endpoints.

### The Metered Override Delta Rule
To ensure optimal performance for complex tasks (Tier 2/3), Reflex prevents "free" flat-rate subscriptions from monopolizing difficult queries. If a metered provider's fitness score outperforms the subscription provider by **`Delta_fitness >= 0.15`**, Reflex executes a **Metered Override**, routing to the metered frontier model to ensure success.

### Shared Skidnir Host Gateway Topology
Reflex is designed as the centralized host gateway for the Skidnir enclave. 
Docker containers, Sons of Anton subagents (Friday, Leo, PostBot), and VibeHard all connect to the shared host daemon via `host.docker.internal:8787` or the Docker bridge mesh.
This topology provides:
- Unified host-level rate-limiting and circuit breakers.
- Global subscription pooling.
- Elimination of sidecar container bloat, avoiding fragmented API key distribution across separate containers.
