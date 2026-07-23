"""
ArachnidWorks policy middleware for google_workspace_mcp.

One choke point on the tool-call hook (FastMCP ``on_call_tool``) that provides
the three fleet-wide guarantees the fork lacked, using the fork's own identity
source (the validated access token / auth context):

  1. Audit trail   - one structured JSON line to stderr per tool call, with the
                     same schema as the rest of the AW fleet.
  2. Email allowlist gate (ALLOWED_EMAILS) - fail-closed: unset/empty list, or
                     an identity we cannot verify, denies the call.
  3. Re-auth policy - 5-day sliding inactivity + 30-day absolute cap, enforced
                     against the persistent ReauthPolicyStore.

Allowlist + re-auth are enforced in HTTP transport only (stdio is single-user
and unauthenticated by design, matching the rest of the fork). The audit line
is emitted for every tool call regardless of transport.
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

from auth.aw_reauth import get_reauth_store

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
    allowed = {e.strip().lower() for e in allowed_raw.split(",") if e.strip()}
    if email.lower() not in allowed:
        return f"Error: Access denied. {email} is not authorized to use this MCP. Contact your admin."
    return None


def _evict_session(email: str) -> None:
    """Drop the cached Google session so a re-auth is actually required."""
    try:
        from auth.oauth21_session_store import get_oauth21_session_store

        get_oauth21_session_store().remove_session(email)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to evict session for %s: %s", email, exc)


class AwPolicyMiddleware(Middleware):
    """Audit + allowlist + re-auth enforcement on every tool call."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = getattr(context, "message", None)
        tool_name = getattr(message, "name", None) or "unknown"
        arguments = getattr(message, "arguments", None) or {}
        arg_keys: List[str] = sorted(arguments.keys()) if isinstance(arguments, dict) else []

        email = _actor_email(context)
        enforce = _transport_mode() == "streamable-http"
        outcome = "ok"
        start = time.monotonic()

        try:
            if enforce:
                allow_error = _check_allowlist(email)
                if allow_error:
                    raise PermissionError(allow_error)

                store = get_reauth_store()
                if store is not None:
                    status, reason = await store.touch(email)
                    if status == "reauth_required":
                        if email:
                            _evict_session(email)
                        raise PermissionError(
                            "Error: Your session has expired under the re-auth policy "
                            f"({reason}). Please reconnect the MCP integration to continue."
                        )

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
