"""
Re-auth policy tests (clock-injected, no real waiting).

Proves the policy state machine on ReauthPolicyStore:
  * start() opens a fresh window,
  * an actively-used session keeps sliding past the 5-day window,
  * inactivity beyond the window reports re-auth WITHOUT mutating the record
    (so the decision is sticky, not self-resetting),
  * the 30-day absolute cap reports re-auth even for a continuously active user,
  * slide() records activity but never creates a record or resets the cap.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from key_value.aio.stores.memory import MemoryStore

from auth.aw_reauth import ReauthPolicyStore

KEY = "upstream-token-abc"


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


def _store(clock):
    return ReauthPolicyStore(MemoryStore(), inactivity_days=5, max_days=30, clock=clock)


def test_start_then_active_session():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        await store.start(KEY)
        status, reason = await store.check_and_slide(KEY)
        assert (status, reason) == ("ok", "active")

    asyncio.run(go())


def test_check_without_start_fails_closed():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        # No start(): a refresh with no policy record must force re-auth.
        assert (await store.check_and_slide(KEY)) == ("reauth_required", "no_session")

    asyncio.run(go())


def test_active_session_slides_past_five_days():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        await store.start(KEY)
        # Refresh every 4 days for 20 days: each within the 5-day window.
        for _ in range(5):
            clock.advance(days=4)
            assert (await store.check_and_slide(KEY)) == ("ok", "active")

    asyncio.run(go())


def test_inactivity_forces_reauth_and_does_not_self_reset():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        await store.start(KEY)
        clock.advance(days=5, hours=1)  # just over the inactivity window
        assert (await store.check_and_slide(KEY)) == ("reauth_required", "inactivity")
        # Sticky: a retry does NOT silently succeed (no self-reset to new_session).
        assert (await store.check_and_slide(KEY)) == ("reauth_required", "inactivity")

    asyncio.run(go())


def test_thirty_day_cap_forces_reauth_even_when_active():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        await store.start(KEY)
        hit_cap = False
        for _ in range(10):  # stay active every 4 days, well past 30
            clock.advance(days=4)
            status, reason = await store.check_and_slide(KEY)
            if status == "reauth_required":
                assert reason == "max_age"
                hit_cap = True
                break
        assert hit_cap, "30-day cap should fire even for a continuously active user"
        # Sticky after the cap trips.
        assert (await store.check_and_slide(KEY))[0] == "reauth_required"

    asyncio.run(go())


def test_reauth_then_fresh_start_resets_window():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        await store.start(KEY)
        clock.advance(days=40)
        # Idle for 40 days: re-auth required (inactivity trips first).
        assert (await store.check_and_slide(KEY))[0] == "reauth_required"
        # A completed re-authorization starts a brand new window.
        await store.start(KEY)
        assert (await store.check_and_slide(KEY)) == ("ok", "active")

    asyncio.run(go())


def test_slide_never_creates_record_or_resets_cap():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        # slide() on an unknown session is a no-op (does not create a record).
        await store.slide(KEY)
        assert (await store.check_and_slide(KEY)) == ("reauth_required", "no_session")

        # After start, slide advances last_used but preserves session_start.
        await store.start(KEY)
        clock.advance(days=40)  # past the 30-day cap
        await store.slide(KEY)  # activity, but must NOT reset session_start
        assert (await store.check_and_slide(KEY)) == ("reauth_required", "max_age")

    asyncio.run(go())
