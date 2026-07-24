"""
ArachnidWorks re-auth policy for google_workspace_mcp.

Enforces the fleet-wide rule on top of the fork's own OAuth session handling:

  * 5-day sliding inactivity window - a session stays valid as long as it is
    used at least once every REAUTH_INACTIVITY_DAYS days.
  * 30-day hard absolute cap - a session must be re-established at least every
    REAUTH_MAX_DAYS days, even for a continuously active user.

Enforcement lives on the OAuth *refresh* path (see aw_reauth_provider.py): FastMCP
access tokens are short-lived and refresh silently, so denying a refresh with
invalid_grant is what actually forces the Google authorization prompt. Denying a
single tool call does not (the client just retries and the still-valid provider
token rebuilds credentials). Activity is recorded on both the refresh path and
every authenticated tool call (finer-grained sliding); a completed
authorization starts a fresh window.

Per-session metadata (session_start, last_used) is keyed by the provider's
stable upstream_token_id and lives in a persistent store, so the policy survives
Cloud Run cold starts. The clock is injectable so the windows are testable
without waiting real days.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

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
    """Track per-session age/activity and decide when re-auth is required.

    ``store`` is any py-key-value-aio async store (typically the Fernet-encrypted
    Firestore store from aw_persistence). Records are keyed by the provider's
    stable upstream_token_id. ``clock`` returns the current aware UTC time and is
    injectable for tests.
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
        self._inactivity_days = (
            inactivity_days if inactivity_days is not None else get_inactivity_days()
        )
        self._max_days = max_days if max_days is not None else get_max_days()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now

    async def start(self, key: str) -> None:
        """Begin a fresh session window (called on a completed authorization).

        Resets both windows: session_start = last_used = now. This is the real
        re-auth reset, so a completed re-authorization starts a new 30-day cap
        and clears any prior inactivity.
        """
        if not key:
            return
        now = self._now()
        await self._write(key, now, now)

    async def check_and_slide(self, key: str) -> Tuple[str, str]:
        """Refresh-path gate. Return ("ok", reason) or ("reauth_required", reason).

        Does NOT mutate the record when re-auth is required (session_start is
        preserved so the 30-day cap keeps counting). On success, slides the
        inactivity window (updates last_used, keeps session_start).
        """
        if not key:
            return ("reauth_required", "no_session")

        now = self._now()
        record = await self._store.get(key, collection=self._collection)
        if not record:
            # No policy record: either the session predates this feature or it
            # aged past the record TTL. Fail closed and force a clean re-auth.
            return ("reauth_required", "no_session")

        session_start = _parse(record.get("session_start")) or now
        last_used = _parse(record.get("last_used")) or session_start

        if now - last_used > timedelta(days=self._inactivity_days):
            return ("reauth_required", "inactivity")
        if now - session_start > timedelta(days=self._max_days):
            return ("reauth_required", "max_age")

        await self._write(key, session_start, now)
        return ("ok", "active")

    async def slide(self, key: str) -> None:
        """Record tool-call activity: update last_used only if a session exists.

        Never creates a record (that is start()'s job) so it can never reset the
        30-day cap, and never enforces (that is the refresh path's job).
        """
        if not key:
            return
        record = await self._store.get(key, collection=self._collection)
        if not record:
            return
        session_start = _parse(record.get("session_start")) or self._now()
        await self._write(key, session_start, self._now())

    async def _write(
        self, key: str, session_start: datetime, last_used: datetime
    ) -> None:
        # TTL to the absolute cap so the record outlives the inactivity window:
        # inactivity is enforced by the explicit last_used check while the record
        # is alive, and a missing record means the session is older than the cap
        # (or predates the feature) and must re-auth anyway.
        ttl_seconds = self._max_days * 24 * 60 * 60
        await self._store.put(
            key,
            {
                "session_start": session_start.isoformat(),
                "last_used": last_used.isoformat(),
            },
            collection=self._collection,
            ttl=ttl_seconds,
        )


# Module-global policy store, configured at server start (parallels
# set_auth_provider in the fork). None until configured; the provider and
# middleware treat "not configured" as "policy disabled" so stdio / OAuth 2.0
# mode is unaffected.
_policy_store: Optional[ReauthPolicyStore] = None


def set_reauth_store(store: Optional[ReauthPolicyStore]) -> None:
    global _policy_store
    _policy_store = store


def get_reauth_store() -> Optional[ReauthPolicyStore]:
    return _policy_store
