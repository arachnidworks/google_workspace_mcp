"""
ArachnidWorks re-auth policy for google_workspace_mcp.

Enforces the fleet-wide rule on top of the fork's own session handling:

  * 5-day sliding inactivity window - a session stays valid as long as it is
    used at least once every REAUTH_INACTIVITY_DAYS days.
  * 30-day hard absolute cap - a session must be re-established at least every
    REAUTH_MAX_DAYS days, even for a continuously active user.

Both windows are checked on every tool call (the same seam the audit +
allowlist gate use), so a session that violates either window forces the user
back through Google OAuth. Per-user metadata (session_start, last_used) lives
in a persistent store so the policy survives Cloud Run cold starts.

The clock is injectable so the windows can be tested without waiting real days.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_INACTIVITY_DAYS = 5
DEFAULT_MAX_DAYS = 30


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default


def get_inactivity_days() -> int:
    return _env_int("REAUTH_INACTIVITY_DAYS", DEFAULT_INACTIVITY_DAYS)


def get_max_days() -> int:
    return _env_int("REAUTH_MAX_DAYS", DEFAULT_MAX_DAYS)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class ReauthPolicyStore:
    """Track per-user session age/activity and decide when re-auth is required.

    ``store`` is any py-key-value-aio async store (typically the Fernet-encrypted
    Firestore store from aw_persistence). ``clock`` returns the current aware UTC
    time and is injectable for tests.
    """

    def __init__(
        self,
        store,
        *,
        collection: str = "aw_reauth_policy",
        inactivity_days: Optional[int] = None,
        max_days: Optional[int] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self._store = store
        self._collection = collection
        self._inactivity_days = inactivity_days if inactivity_days is not None else get_inactivity_days()
        self._max_days = max_days if max_days is not None else get_max_days()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    async def touch(self, user_email: str) -> Tuple[str, str]:
        """Record activity for ``user_email`` and evaluate the re-auth policy.

        Returns ``("ok", reason)`` if the session may proceed, or
        ``("reauth_required", reason)`` if the user must re-authenticate. When
        re-auth is required the stored record is cleared so the next successful
        auth starts a fresh window.
        """
        if not user_email:
            # Fail closed: no identity means no valid session.
            return ("reauth_required", "no_identity")

        now = self._now()
        record = await self._store.get(user_email, collection=self._collection)

        if not record:
            await self._write(user_email, now, now)
            return ("ok", "new_session")

        session_start = _parse(record.get("session_start")) or now
        last_used = _parse(record.get("last_used")) or session_start

        if now - last_used > timedelta(days=self._inactivity_days):
            await self._store.delete(user_email, collection=self._collection)
            return ("reauth_required", "inactivity")

        if now - session_start > timedelta(days=self._max_days):
            await self._store.delete(user_email, collection=self._collection)
            return ("reauth_required", "max_age")

        # Active session within both windows: slide the inactivity window,
        # keep the original session_start so the 30-day cap stays absolute.
        await self._write(user_email, session_start, now)
        return ("ok", "active")

    async def _write(self, user_email: str, session_start: datetime, last_used: datetime) -> None:
        # TTL to the inactivity window so abandoned records self-expire; an
        # active session keeps re-writing (and re-extending) it.
        ttl_seconds = self._inactivity_days * 24 * 60 * 60
        await self._store.put(
            user_email,
            {
                "session_start": session_start.isoformat(),
                "last_used": last_used.isoformat(),
            },
            collection=self._collection,
            ttl=ttl_seconds,
        )

    async def reset(self, user_email: str) -> None:
        """Clear a user's policy record (used on explicit sign-out / new auth)."""
        try:
            await self._store.delete(user_email, collection=self._collection)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to reset re-auth record for %s: %s", user_email, exc)


# Module-global policy store, configured at server start (parallels
# set_auth_provider in the fork). None until configured; the middleware treats
# "not configured" as "policy disabled" so stdio / OAuth 2.0 mode is unaffected.
_policy_store: Optional[ReauthPolicyStore] = None


def set_reauth_store(store: Optional[ReauthPolicyStore]) -> None:
    global _policy_store
    _policy_store = store


def get_reauth_store() -> Optional[ReauthPolicyStore]:
    return _policy_store
