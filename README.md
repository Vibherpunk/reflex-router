# OpenCode-Go System 1 Intelligent Router Gateway

A high-performance local reverse-proxy router (`http://127.0.0.1:8787/v1`) designed specifically for **OpenCode Go** subscriptions. It intercepts requests from open agent harnesses (OpenCode, Goose, Aider, Cline) and dynamically routes prompts to the optimal model and reasoning tier in sub-10ms.

---

## 1. Verified Model Tiers (OpenCode-Go Live Catalog)

| Tier | Category | Primary Model | Fallback Model | Use Cases |
| :---: | :--- | :--- | :--- | :--- |
| **0** | **Fast / Tool Churn** | `deepseek-v4-flash` | `glm-5.3-flash` | File reading, grepping, formatting, commit messages, status checks. |
| **1** | **General Implementation** | `qwen3.7-plus` | `glm-5.3` | Writing features, adding endpoints, multi-file refactors, unit tests. |
| **2** | **Deep Reasoning & Bugs** | `deepseek-v4-pro` | `qwen3.7-max` | Race conditions, memory leaks, concurrency, test failures (`exit code 1`). |
| **3** | **Frontier Architecture** | `qwen3.7-max` | `kimi-k3` | System architecture, RFC specs, distributed consensus, `--deep` / `#hard`. |

---

## 2. Production Defensive Engineering

1. **Trojan Horse Detection:** Inspects prior tool executions in the conversation history. If any recent turn contains `exit code 1`, `AssertionError`, or `Traceback`, the request is automatically escalated to **Tier 2 (Deep Reasoning)**, even if the user's prompt is only 2 words long (`"Fix it"`).
2. **Sticky Session KV-Cache Latch:** Once a conversation exceeds 20,000 tokens, the router locks the model family to preserve provider-side KV prompt caching (avoiding the 10x cost penalty of provider thrashing).
3. **Speculative Preamble Buffer:** Buffers the first 512 bytes of upstream streaming chunks before committing headers downstream. If an upstream 429/503 occurs, it fails over to the tier's backup model transparently.
4. **SSE Heartbeat Generator:** Injects `: ping\n\n` SSE comment frames every 3 seconds during deep reasoning phases to prevent downstream client socket read timeouts.
5. **Client Disconnect Sentry:** Detects client disconnects (`Ctrl+C` or task cancellation) and immediately terminates upstream `httpx` streaming requests to prevent ghost/zombie token generation.
6. **OpenCode-Go Protocol Compliance:** Automatically injects required `x-opencode-session` headers and verified client user-agent strings.

---

## 3. How to Configure OpenCode to Use This Router

In your `~/.config/opencode/opencode.json` (or project `.opencode.json`):

```json
{
  "provider": {
    "system1-router": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "System 1 OpenCode-Go Router",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "local-key"
      },
      "models": {
        "auto": {
          "id": "auto",
          "name": "Intelligent Auto Router (System 1)"
        }
      }
    }
  },
  "model": "system1-router/auto"
}
```

---

## 4. Running & Managing the Service

### Start Server
```bash
cd ~/workspace/system1-router
./start.sh
```

### Health Check
```bash
curl http://127.0.0.1:8787/health
```

### Run Full Test Suite
```bash
pytest -v tests/test_router.py
```
