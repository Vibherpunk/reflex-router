# Reflex Router — Client Contract (v2.3.0)

**Posture: advisory-only.** Reflex *suggests* a route; the caller decides.
Reflex is never a hard dependency of any build path. If the daemon is down,
slow, or unreachable, the caller falls back to its own local default and
continues — it MUST NOT raise a Reflex outage into its own build.

## Transport

- `HTTP POST` to `$REFLEX_URL` (default `http://127.0.0.1:8787`)
- Auth: `Authorization: Bearer $REFLEX_GATEWAY_TOKEN` on every `/v1/*` call.
  (`/health` and `/` are open; everything else 401s without the token.)
- Flag day: all local callers (MCP server, federation fallback, scripts,
  future VibeHard wiring) must send the Bearer <redacted>

## Timeouts

| Call | Connect | Read |
|---|---|---|
| `GET /health` | ≤ 2s | ≤ 5s |
| `POST /v1/route` | ≤ 2s | ≤ 10s |
| `POST /v1/chat/completions` | ≤ 2s | caller-defined (streaming) |

## Retry

- Exactly **ONE** retry on transport error, timeout, or HTTP 503, with a
  fixed 250ms backoff.
- Never retry 4xx (400/401/404/422 are caller errors — retrying is pointless).
- Honor `Retry-After` once on 429, then retry exactly once; a second 429 falls back.

## Fallback (fail-closed to local default)

After retry exhaustion the caller returns a local default suggestion and
continues:

```json
{
  "available": false,
  "tier": 0,
  "effort": "low",
  "model": "<caller's own default model>",
  "provider": "local-default",
  "reason": "reflex_unavailable: <cause: refused|timeout|503|auth>"
}
```

## Logging

- Emit one structured event per fallback: `reflex_unavailable` with fields
  `cause`, `latency_ms`, `retries`.
- NEVER log prompt text or the Bearer <redacted>

## Health caching (optional)

Callers MAY cache `GET /health` for 30s to avoid hammering a dead daemon;
not required.

## Reference implementation

`reflex_client.py` in this repo implements this contract (~120 lines,
`httpx` only). Use it or re-implement; the contract above is normative.
