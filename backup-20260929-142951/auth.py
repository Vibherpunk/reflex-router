"""Bearer-token gateway authentication for the Reflex router. Fail-closed by design.

The expected token is read lazily on every request via ``config.get_key``:
``os.environ`` -> ``$HOME/.env`` -> ``$HOME/.hermes/.env``.

Rotation semantics:
- Setting/changing ``REFLEX_GATEWAY_TOKEN`` in ``os.environ`` takes effect
  immediately (no restart).
- Editing ``~/.hermes/.env`` (or the launchd plist ``EnvironmentVariables``)
  post-start requires a daemon restart (import-time snapshot / plist reload).
"""
import hmac
from fastapi import Request, HTTPException

from config import get_key

TOKEN_ENV_VAR = "REFLEX_GATEWAY_TOKEN"


def get_expected_token() -> str:
    """Return the configured gateway token, or '' when not configured."""
    return get_key(TOKEN_ENV_VAR)


def token_configured() -> bool:
    return bool(get_expected_token())


async def verify_gateway_token(request: Request) -> None:
    """FastAPI dependency guarding protected routes.

    - 503 when the server itself is misconfigured (no token set): fail closed,
      never serve unauthenticated.
    - 401 when the client presents no token or a wrong token. Wrong-length and
      wrong-value tokens both yield 401 with no distinguishing detail (no oracle).
    """
    expected = get_expected_token()
    if not expected:
        raise HTTPException(status_code=503, detail="Gateway auth not configured")
    auth = request.headers.get("authorization", "")
    scheme, _, presented = auth.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Constant-time comparison: no length/oracle leakage.
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
