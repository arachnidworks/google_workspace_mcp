"""
ArachnidWorks policy middleware for google_workspace_mcp.

One choke point on the tool-call hook (FastMCP ``on_call_tool``), using the
fork's own identity source (the validated access token / auth context):

  1. Audit trail   - one structured JSON line to stderr per tool call, with the
                     same schema as the rest of the AW fleet.
  2. Email allowlist gate (ALLOWED_EMAILS) - fail-closed: unset/empty list, or
                     an identity we cannot verify, denies the call.
  3. Re-auth activity sliding - records tool-call activity so the 5-day
                     inactivity window slides at tool-call granularity. The
                     re-auth policy is ENFORCED on the OAuth refresh path
                     (AwGoogleProvider), not here.

The allowlist is enforced in HTTP transport only (stdio is single-user and
unauthenticated by design, matching the rest of the fork). The audit line is
emitted for every tool call regardless of transport.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_access_token

logger = logging.getLogger(__name__)


def _service_name() -> str:
    return os.getenv("SERVICE_NAME", "").strip() or "google_workspace"


def _transport_mode() -> str:
    try:
        from core.config import get_transport_mode

        return get_transport_mode()
    except Exception:
        # Fail closed for enforcement decisions: unknown transport is treated
        # as HTTP so the allowlist/re-auth gate still runs.
        return "streamable-http"


def _actor_email(context: MiddlewareContext) -> Optional[str]:
    """Resolve the acting user's email from the validated token / auth context."""
    try:
        token = get_access_token()
        if token is not None:
            email = getattr(token, "email", None)
            if not email and getattr(token, "claims", None):
                email = token.claims.get("email")
            if email:
                return email
    except Exception:
        pass
    # Fall back to the state AuthInfoMiddleware populated for this request.
    try:
        fmcp = getattr(context, "fastmcp_context", None)
        if fmcp is not None:
            getter = getattr(fmcp, "get_state", None)
            if getter is not None:
                email = getter("authenticated_user_email")
                if email is not None and not hasattr(email, "__await__"):
                    return email
    except Exception:
        pass
    return None


def _check_allowlist(email: Optional[str]) -> Optional[str]:
    """Return None if allowed, else an error message. Fail-closed (mirrors access.py)."""
    allowed_raw = os.environ.get("ALLOWED_EMAILS", "").strip()
    if not allowed_raw:
        return (
            "Error: ALLOWED_EMAILS is not configured on this server. Access denied. "
            "Ask the admin to set ALLOWED_EMAILS."
        )
    if not email:
        return "Error: Could not determine your identity. Please reconnect the MCP integration."
    entries = {e.strip().lower() for e in allowed_raw.split(",") if e.strip()}
    email_lower = email.lower()
    # Domain of the email = part after the LAST "@" (only if an "@" is present).
    domain = email_lower.rsplit("@", 1)[-1] if "@" in email_lower else None
    for entry in entries:
        if entry.startswith("@"):
            # Domain wildcard: true domain equality, never a substring match.
            if domain and domain == entry[1:]:
                return None
        elif entry == email_lower:
            return None
    return f"Error: Access denied. {email} is not authorized to use this MCP. Contact your admin."


async def _slide_activity() -> None:
    """Record tool-call activity against the re-auth policy (best-effort).

    Delegates to AwGoogleProvider, which resolves the stable upstream_token_id
    from the current access token and slides last_used. Never enforces, never
    raises into the call path.
    """
    try:
        from core.server import get_auth_provider
        from auth.aw_reauth_provider import AwGoogleProvider

        provider = get_auth_provider()
        if not isinstance(provider, AwGoogleProvider):
            return
        token = get_access_token()
        token_str = getattr(token, "token", None) if token else None
        if token_str:
            await provider.slide_activity(token_str)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Activity slide skipped: %s", exc)


class AwPolicyMiddleware(Middleware):
    """Audit + email allowlist on every tool call, plus re-auth activity sliding.

    Re-auth is ENFORCED on the OAuth refresh path (see AwGoogleProvider), not
    here: denying a single tool call does not force re-authentication (the client
    retries and the still-valid provider token rebuilds credentials). This
    middleware only records activity so the 5-day inactivity window slides at
    tool-call granularity.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = getattr(context, "message", None)
        tool_name = getattr(message, "name", None) or "unknown"
        arguments = getattr(message, "arguments", None) or {}
        arg_keys: List[str] = (
            sorted(arguments.keys()) if isinstance(arguments, dict) else []
        )

        email = _actor_email(context)
        enforce = _transport_mode() == "streamable-http"
        outcome = "ok"
        start = time.monotonic()

        try:
            if enforce:
                allow_error = _check_allowlist(email)
                if allow_error:
                    raise PermissionError(allow_error)

                # Record activity for the sliding inactivity window (best-effort;
                # never enforces or fails the call).
                await _slide_activity()

            return await call_next(context)
        except Exception:
            outcome = "error"
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            self._emit_audit(tool_name, email, arg_keys, outcome, duration_ms)

    @staticmethod
    def _emit_audit(
        action: str,
        actor_email: Optional[str],
        arg_keys: List[str],
        outcome: str,
        duration_ms: int,
    ) -> None:
        entry = {
            "audit": True,
            "ts": datetime.now(timezone.utc).isoformat(),
            "service": _service_name(),
            "actor_email": actor_email,
            "action": action,
            "arg_keys": arg_keys,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        try:
            print(json.dumps(entry), file=sys.stderr, flush=True)
        except Exception:  # pragma: no cover - never let auditing break a call
            logger.debug("Failed to emit audit line")
