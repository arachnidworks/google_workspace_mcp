"""
Re-auth policy tests (clock-injected, no real waiting).

Proves the three fleet rules on ReauthPolicyStore:
  * a fresh session is accepted and recorded,
  * an actively-used session keeps sliding past the 5-day window,
  * inactivity beyond the window forces re-auth,
  * the 30-day absolute cap forces re-auth even for a continuously active user.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from key_value.aio.stores.memory import MemoryStore

from auth.aw_reauth import ReauthPolicyStore


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


def _store(clock, **kwargs):
    return ReauthPolicyStore(
        MemoryStore(),
        inactivity_days=5,
        max_days=30,
        clock=clock,
        **kwargs,
    )


def test_new_session_is_accepted_and_recorded():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        status, reason = await store.touch("user@aw.com")
        assert (status, reason) == ("ok", "new_session")
        # A subsequent immediate call is an active session, not a new one.
        status2, reason2 = await store.touch("user@aw.com")
        assert status2 == "ok"
        assert reason2 == "active"

    asyncio.run(go())


def test_active_session_slides_past_five_days():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        assert (await store.touch("u@aw.com"))[0] == "ok"
        # Use it every 4 days for 20 days: each use is within the 5-day window
        # of the previous use, so it must stay valid the whole time.
        for _ in range(5):
            clock.advance(days=4)
            status, reason = await store.touch("u@aw.com")
            assert status == "ok", reason
            assert reason == "active"

    asyncio.run(go())


def test_inactivity_beyond_window_forces_reauth():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        await store.touch("idle@aw.com")
        clock.advance(days=5, hours=1)  # just over 5 days, no activity between
        status, reason = await store.touch("idle@aw.com")
        assert (status, reason) == ("reauth_required", "inactivity")
        # After re-auth-required the record is cleared, so the next touch is a
        # brand new session.
        status2, reason2 = await store.touch("idle@aw.com")
        assert (status2, reason2) == ("ok", "new_session")

    asyncio.run(go())


def test_thirty_day_cap_forces_reauth_even_when_active():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        await store.touch("busy@aw.com")
        # Stay active (every 4 days) well past 30 days.
        hit_cap = False
        for _ in range(10):
            clock.advance(days=4)
            status, reason = await store.touch("busy@aw.com")
            if status == "reauth_required":
                assert reason == "max_age"
                hit_cap = True
                break
        assert hit_cap, "30-day absolute cap should have fired for an active user"

    asyncio.run(go())


def test_missing_identity_fails_closed():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = _store(clock)

    async def go():
        assert (await store.touch(""))[0] == "reauth_required"
        assert (await store.touch(None))[0] == "reauth_required"

    asyncio.run(go())
