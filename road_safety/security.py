"""Shared request-auth helpers for sensitive operational endpoints.

This module centralises the authentication primitives used across both the
edge server (``road_safety.server``) and the cloud receiver (``cloud.receiver``).
Keeping the check in one place means every sensitive endpoint verifies the
token the same way — with constant-time comparison, the same error codes,
and the same "fail closed" behaviour when a token is not configured.

Token model — two distinct tiers:

1. **Bearer token (admin tier)** — standard ``Authorization: Bearer <token>``
   HTTP header, used by ``require_bearer_token`` below. The caller presents
   a long-lived shared secret that was provisioned into the deployment's
   environment (for example ``ROAD_ADMIN_TOKEN`` or ``ROAD_CLOUD_READ_TOKEN``).
   Bearer tokens guard operator/admin routes: audit log, LLM telemetry,
   retention controls, agent internals. Treat as root-equivalent.

2. **DSAR token (subject-access tier)** — a custom header
   ``X-DSAR-Token`` (configured via ``ROAD_DSAR_TOKEN``) granting access to
   *unredacted* thumbnails for Data Subject Access Request workflows. It is
   intentionally a separate tier from the admin token because legal / DPO
   teams should be able to fulfil DSARs without also getting operator
   superpowers. Validation for the DSAR tier is performed inline in
   ``road_safety/server.py`` (audit-logged on denial) because it uses a
   different header name; this module focuses on the bearer flow.

Both tiers "fail closed": if the corresponding environment variable is unset
on the server, the endpoint responds with HTTP 503 rather than silently
allowing unauthenticated access — accidentally deploying without a token
should break the route, not expose it.
"""

# ``from __future__ import annotations`` makes every type annotation in this
# file a forward-reference string, which is what allows ``str | None`` syntax
# to work on older Python versions as well as the ``Request`` type-hint below
# without triggering evaluation at import time.
from __future__ import annotations

# ``secrets`` is the stdlib module for security-sensitive primitives. We only
# need ``secrets.compare_digest`` here — a constant-time byte comparison that
# prevents timing-attack leaks of the configured token. A naive ``==`` check
# would return faster on early-mismatching bytes, leaking token length / prefix.
import secrets

# FastAPI ships these two symbols:
#   - ``HTTPException`` — raising it inside a route handler produces an HTTP
#     error response with the given status code and detail payload. Unlike a
#     normal exception, FastAPI catches it and renders a proper JSON response
#     instead of a 500.
#   - ``Request`` — the per-request object carrying headers, query params, and
#     the raw body. We only need it for the ``Authorization`` header here.
from fastapi import HTTPException, Request


def require_bearer_token(
    request: Request,
    token: str | None,
    *,
    realm: str,
    env_var: str,
) -> None:
    """Require an exact bearer token for a sensitive endpoint.

    Intended to be called at the top of any FastAPI route handler that should
    only be reachable by an operator holding the configured shared secret.
    Raises an ``HTTPException`` on any failure — FastAPI turns the exception
    into the corresponding HTTP response, so no explicit return value is
    needed for the happy path.

    Args:
        request: The current ``fastapi.Request`` — we read its
            ``Authorization`` header to extract the presented token.
        token: The expected secret, typically read from an environment
            variable at module import time. ``None``/empty means "this
            endpoint is disabled on this deployment".
        realm: Short human-readable label for error messages (e.g. "audit",
            "cloud read"). The realm shows up in the HTTP 401/403 detail so
            operators can tell which auth tier rejected them.
        env_var: Name of the environment variable that should hold the
            token. Included in the 503 detail so operators know exactly
            which variable is missing.

    Returns:
        ``None`` on success. The function is used purely for its side effect
        of raising on failure.

    Raises:
        HTTPException(503): The server is not configured for this tier at
            all (``token`` is falsy). Fail-closed stance: an unconfigured
            admin endpoint is treated as disabled, never as public.
        HTTPException(401): The ``Authorization`` header is missing, not a
            bearer scheme, or carries an empty value. Caller did not
            authenticate.
        HTTPException(403): A token was presented, but it did not match.
            Caller authenticated with the wrong secret.
    """
    # Fail closed: if the deployment never set the token env var, the
    # endpoint is treated as disabled rather than silently exposed. A
    # forgotten env var in a new deployment should surface as a clear 503,
    # not a wide-open route.
    if not token:
        raise HTTPException(
            503,
            f"{realm} access disabled — set {env_var} to enable this endpoint",
        )

    # ``request.headers.get("Authorization")`` returns ``None`` if the header
    # was not sent. The ``or ""`` collapses that to an empty string so the
    # subsequent ``.strip()`` and ``.startswith()`` checks don't need
    # separate None handling.
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        # Standard HTTP bearer scheme per RFC 6750. We reject anything else
        # (Basic, no scheme, different casing) to keep the authentication
        # surface narrow.
        raise HTTPException(401, f"missing bearer token for {realm} access")

    # Slice off the literal ``"Bearer "`` prefix (7 characters) to get the
    # raw token. ``.strip()`` tolerates benign trailing whitespace from
    # clients that pad the header.
    presented = auth[7:].strip()
    if not presented:
        raise HTTPException(401, f"missing bearer token for {realm} access")

    # ``secrets.compare_digest`` runs in time proportional to the length of
    # the shorter input — critically, it does *not* short-circuit on the
    # first mismatching byte the way ``==`` does. Using ``==`` here would
    # leak the correct prefix of the real token over many timed requests.
    if not secrets.compare_digest(presented, token):
        raise HTTPException(403, f"invalid bearer token for {realm} access")
