"""
Policy middleware tests: email allowlist (fail-closed) and audit emission.

Re-auth is enforced on the OAuth refresh path (see test_aw_reauth_provider.py),
NOT in this middleware. Here we only confirm the middleware gates on the
allowlist and always emits a well-formed audit line, and that stdio mode does
not enforce.
"""

import asyncio
import json
import types
from datetime import datetime

import pytest

from auth import aw_policy_middleware as pm
from auth.aw_reauth import set_reauth_store


def _ctx(tool="do_thing", args=None):
    message = types.SimpleNamespace(name=tool, arguments=args if args is not None else {"a": 1, "b": 2})
    return types.SimpleNamespace(message=message, fastmcp_context=None)


def _run(coro):
    return asyncio.run(coro)


def _last_audit(capsys):
    err = capsys.readouterr().err
    entries = []
    for line in err.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("audit") is True:
            entries.append(obj)
    assert entries, f"no audit line found in stderr:\n{err}"
    return entries[-1]


# ---- allowlist (pure helper) ------------------------------------------------

def test_allowlist_fail_closed_when_unset(monkeypatch):
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    assert pm._check_allowlist("x@aw.com") is not None


def test_allowlist_denies_unknown_and_missing_identity(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "ok@aw.com, other@aw.com")
    assert pm._check_allowlist(None) is not None
    assert pm._check_allowlist("nope@aw.com") is not None
    assert pm._check_allowlist("ok@aw.com") is None
    assert pm._check_allowlist("OK@AW.COM") is None  # case-insensitive


# ---- middleware -------------------------------------------------------------

def test_middleware_allows_listed_user_and_audits_ok(monkeypatch, capsys):
    monkeypatch.setenv("ALLOWED_EMAILS", "user@aw.com")
    monkeypatch.setattr(pm, "_transport_mode", lambda: "streamable-http")
    monkeypatch.setattr(
        pm, "get_access_token",
        lambda: types.SimpleNamespace(email="user@aw.com", claims={"email": "user@aw.com"}),
    )
    set_reauth_store(None)
    mw = pm.AwPolicyMiddleware()
    called = {}

    async def call_next(ctx):
        called["yes"] = True
        return "RESULT"

    result = _run(mw.on_call_tool(_ctx(), call_next))
    assert result == "RESULT"
    assert called.get("yes")

    entry = _last_audit(capsys)
    assert entry["actor_email"] == "user@aw.com"
    assert entry["action"] == "do_thing"
    assert entry["arg_keys"] == ["a", "b"]
    assert entry["outcome"] == "ok"
    assert entry["service"]
    assert isinstance(entry["duration_ms"], int)
    # ts is ISO-8601 UTC parseable
    datetime.fromisoformat(entry["ts"])


def test_middleware_denies_unlisted_user_and_audits_error(monkeypatch, capsys):
    monkeypatch.setenv("ALLOWED_EMAILS", "user@aw.com")
    monkeypatch.setattr(pm, "_transport_mode", lambda: "streamable-http")
    monkeypatch.setattr(
        pm, "get_access_token",
        lambda: types.SimpleNamespace(email="intruder@aw.com", claims={}),
    )
    set_reauth_store(None)
    mw = pm.AwPolicyMiddleware()
    called = {}

    async def call_next(ctx):
        called["yes"] = True
        return "R"

    with pytest.raises(PermissionError):
        _run(mw.on_call_tool(_ctx(), call_next))
    assert "yes" not in called  # tool never ran

    entry = _last_audit(capsys)
    assert entry["outcome"] == "error"
    assert entry["actor_email"] == "intruder@aw.com"
    assert entry["arg_keys"] == ["a", "b"]  # names only, never values


def test_middleware_does_not_enforce_reauth_only_slides(monkeypatch, capsys):
    # Even with no auth provider set (so activity sliding is a no-op), an
    # allowlisted call must succeed: the middleware must NOT deny on re-auth.
    monkeypatch.setenv("ALLOWED_EMAILS", "user@aw.com")
    monkeypatch.setattr(pm, "_transport_mode", lambda: "streamable-http")
    monkeypatch.setattr(
        pm, "get_access_token",
        lambda: types.SimpleNamespace(email="user@aw.com", claims={}),
    )
    set_reauth_store(None)
    mw = pm.AwPolicyMiddleware()
    called = {}

    async def call_next(ctx):
        called["yes"] = True
        return "OK"

    result = _run(mw.on_call_tool(_ctx(), call_next))
    assert result == "OK"
    assert called.get("yes")
    assert _last_audit(capsys)["outcome"] == "ok"


def test_stdio_mode_skips_enforcement_but_still_audits(monkeypatch, capsys):
    # No allowlist set: in HTTP this would deny, but stdio must not enforce.
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    monkeypatch.setattr(pm, "_transport_mode", lambda: "stdio")
    monkeypatch.setattr(pm, "get_access_token", lambda: None)
    set_reauth_store(None)
    mw = pm.AwPolicyMiddleware()
    called = {}

    async def call_next(ctx):
        called["yes"] = True
        return "OK"

    result = _run(mw.on_call_tool(_ctx(), call_next))
    assert result == "OK"
    assert called.get("yes")
    entry = _last_audit(capsys)
    assert entry["outcome"] == "ok"
