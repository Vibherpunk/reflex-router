# Reflex Multi-Provider System 1 Intelligent Gateway (v2.0.0)

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

---

## 3. Verified Compute Tiers

| Tier | Category | Subscription Model ($0.00) | Metered Fallback | Primary Use Cases |
| :---: | :--- | :--- | :--- | :--- |
| **0** | **Fast / Tool Churn** | `deepseek-v4-flash` | `deepseek/deepseek-chat` | Reading files, grepping, status checks, formatters. |
| **1** | **General Implementation** | `qwen3.7-plus` | `deepseek/deepseek-chat` | Writing features, adding endpoints, multi-file refactors. |
| **2** | **Deep Reasoning & Bugfix** | `deepseek-v4-pro` | `deepseek/deepseek-r1` | Concurrency bugs, memory leaks, test failures (`exit code 1`). |
| **3** | **Frontier Architecture** | `qwen3.7-max` | `anthropic/claude-3.7-sonnet` | System architecture, RFC specs, security audits, `#hard`. |

---

## 4. API Endpoints

| Endpoint | Method | Description |
| :--- | :---: | :--- |
| `/health` | `GET` | Reports provider configuration status, API key health, and circuit breakers. |
| `/v1/models` | `GET` | OpenAI-compatible model listing (`reflex/auto` + catalog). |
| `/v1/chat/completions` | `POST` | Core OpenAI-compatible streaming & non-streaming completions endpoint. |
| `/v1/feedback` | `POST` | Ingests execution feedback to train Skidnir System 1 incident memory. |
| `/v1/stats` | `GET` | Returns incident counts, tier request distributions, and circuit breaker states. |

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
