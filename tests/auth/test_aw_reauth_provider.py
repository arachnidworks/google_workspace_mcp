"""
Enforcement tests for AwGoogleProvider on the OAuth refresh path.

This is where the re-auth policy actually bites: a denied refresh must raise
invalid_grant and must NOT fall through to the upstream refresh (which would
silently mint new tokens and let the client keep going without re-auth).
Clock-injected so the 5-day and 30-day windows are exercised without waiting.
"""

import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import TokenError
from key_value.aio.stores.memory import MemoryStore

from auth.aw_reauth import ReauthPolicyStore, set_reauth_store
from auth.aw_reauth_provider import AwGoogleProvider

UID = "upstream-token-xyz"


class FakeClock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)


class _StubProvider(AwGoogleProvider):
    """AwGoogleProvider with GoogleProvider.__init__ bypassed for unit testing."""

    def __init__(self):
        pass  # skip network/discovery-heavy base init

    async def _resolve_upstream_token_id(self, token, token_use):
        return UID


def _refresh_token():
    return types.SimpleNamespace(token="fastmcp.refresh.jwt")


def _install_super_stubs(monkeypatch, calls):
    async def fake_refresh(self, client, refresh_token, scopes):
        calls.append("refresh")
        return "NEW_TOKENS"

    async def fake_authcode(self, client, authorization_code):
        calls.append("authcode")
        return types.SimpleNamespace(access_token="fastmcp.access.jwt")

    monkeypatch.setattr(GoogleProvider, "exchange_refresh_token", fake_refresh)
    monkeypatch.setattr(GoogleProvider, "exchange_authorization_code", fake_authcode)


def test_active_refresh_passes_through(monkeypatch):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = ReauthPolicyStore(MemoryStore(), inactivity_days=5, max_days=30, clock=clock)
    set_reauth_store(store)
    calls = []
    _install_super_stubs(monkeypatch, calls)
    provider = _StubProvider()
    try:
        async def go():
            await store.start(UID)
            result = await provider.exchange_refresh_token(None, _refresh_token(), [])
            assert result == "NEW_TOKENS"
        asyncio.run(go())
        assert calls == ["refresh"]  # super WAS called
    finally:
        set_reauth_store(None)


def test_inactive_refresh_is_denied_and_does_not_call_super(monkeypatch):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = ReauthPolicyStore(MemoryStore(), inactivity_days=5, max_days=30, clock=clock)
    set_reauth_store(store)
    calls = []
    _install_super_stubs(monkeypatch, calls)
    provider = _StubProvider()
    try:
        async def go():
            await store.start(UID)
            clock.advance(days=6)  # exceed inactivity window
            with pytest.raises(TokenError) as exc:
                await provider.exchange_refresh_token(None, _refresh_token(), [])
            assert exc.value.error == "invalid_grant"
            # The denial is sticky: a retry is also denied, never silently OK.
            with pytest.raises(TokenError):
                await provider.exchange_refresh_token(None, _refresh_token(), [])
        asyncio.run(go())
        assert calls == []  # super (upstream refresh) NEVER called -> no new tokens
    finally:
        set_reauth_store(None)


def test_thirty_day_cap_denies_refresh_even_when_active(monkeypatch):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = ReauthPolicyStore(MemoryStore(), inactivity_days=5, max_days=30, clock=clock)
    set_reauth_store(store)
    calls = []
    _install_super_stubs(monkeypatch, calls)
    provider = _StubProvider()
    try:
        async def go():
            await store.start(UID)
            denied = False
            for _ in range(10):  # active every 4 days, past 30 total
                clock.advance(days=4)
                try:
                    await provider.exchange_refresh_token(None, _refresh_token(), [])
                except TokenError as e:
                    assert e.error == "invalid_grant"
                    denied = True
                    break
            assert denied, "30-day cap must deny refresh even for an active user"
        asyncio.run(go())
    finally:
        set_reauth_store(None)


def test_authorization_code_starts_fresh_window(monkeypatch):
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = ReauthPolicyStore(MemoryStore(), inactivity_days=5, max_days=30, clock=clock)
    set_reauth_store(store)
    calls = []
    _install_super_stubs(monkeypatch, calls)
    provider = _StubProvider()
    try:
        async def go():
            # Before auth there is no window -> refresh denied.
            assert (await store.check_and_slide(UID)) == ("reauth_required", "no_session")
            # A completed authorization opens a fresh window.
            token = await provider.exchange_authorization_code(None, None)
            assert token.access_token == "fastmcp.access.jwt"
            assert (await store.check_and_slide(UID)) == ("ok", "active")
        asyncio.run(go())
        assert calls == ["authcode"]
    finally:
        set_reauth_store(None)


def test_policy_disabled_is_passthrough(monkeypatch):
    set_reauth_store(None)  # no policy configured
    calls = []
    _install_super_stubs(monkeypatch, calls)
    provider = _StubProvider()

    async def go():
        result = await provider.exchange_refresh_token(None, _refresh_token(), [])
        assert result == "NEW_TOKENS"

    asyncio.run(go())
    assert calls == ["refresh"]
