# Reflex Router Rust Specification

## BYOK Tenant Isolation Protocol
1. **Upstream Mapping**: First-class support for `opencode-go` models mapping to `https://opencode.ai/zen/go/v1/chat/completions`.
2. **Client Key Preservation**: Client containers supply `Authorization: Bearer <key>` and `X-Tenant-Id: <tenant>`. The router captures and transparently proxies these credentials upstream without persisting them to disk (zero disk cleartext secrets).
3. **Billing Independence**: Ensures true $0 agency pass-through liability by allowing clients to directly incur AI usage costs.
4. **Fallback Resilience**: If no client key is provided, gracefully falls back to the host default `OPENCODE_GO_API_KEY` or `OPENROUTER_API_KEY`.
