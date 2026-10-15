"""The AW overlay wires onto the upstream server without editing core/server.py."""

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from auth.aw_reauth_provider import AwGoogleProvider

REPO = Path(__file__).resolve().parents[2]


def test_overlay_patches_provider_and_configure(overlay):
    import core.server as s

    assert s.WorkspaceGoogleProvider is overlay._aw_provider
    assert s.configure_server_for_http is overlay.configure_server_for_http
    assert issubclass(AwGoogleProvider, overlay._server.GoogleProvider)


def _kwargs():
    return {"jwt_signing_key": b"k" * 32, "client_storage": None}


def test_firestore_backend_swaps_client_storage(overlay, monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "firestore")
    sentinel = object()
    monkeypatch.setattr(overlay, "_firestore_client_storage", lambda key: sentinel)
    seen = {}
    monkeypatch.setattr(
        overlay, "AwGoogleProvider", lambda **kw: seen.update(kw) or type("P", (), {})()
    )
    provider = overlay._aw_provider(**_kwargs())
    assert seen["client_storage"] is sentinel
    assert provider._aw_jwt_signing_key == b"k" * 32


def test_other_backends_keep_upstream_storage(overlay, monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "memory")
    seen = {}
    monkeypatch.setattr(
        overlay, "AwGoogleProvider", lambda **kw: seen.update(kw) or type("P", (), {})()
    )
    overlay._aw_provider(**_kwargs())
    assert seen["client_storage"] is None


def test_firestore_fails_closed(overlay, monkeypatch):
    import key_value.aio.stores as stores

    monkeypatch.setitem(sys.modules, "key_value.aio.stores.firestore", None)
    monkeypatch.delattr(stores, "firestore", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to"):
        overlay._firestore_client_storage(b"k" * 32)


def test_upstream_idle_timeout_is_61_minutes(monkeypatch):
    import core.server as s

    monkeypatch.delenv("WORKSPACE_MCP_SESSION_IDLE_TIMEOUT", raising=False)
    assert s.get_session_idle_timeout() == 61 * 60


def test_entrypoint_imports_and_routes_chat_to_overlay():
    # Separate process: aw_main patches module-global state for the whole run.
    code = (
        "import aw_main, main, core.server as s;"
        "assert main.SERVICE_MODULES['chat'] == 'aw.chat_tools';"
        "assert main.configure_server_for_http is s.configure_server_for_http;"
        "assert s.configure_server_for_http.__module__ == 'aw.overlay'"
    )
    subprocess.run([sys.executable, "-c", code], cwd=REPO, check=True)


def test_chat_override_registers_aw_tools():
    code = (
        "import asyncio, aw.chat_tools, core.server as s;"
        "t = asyncio.run(s.server.get_tool('list_spaces'));"
        "assert 'resolve_names' in t.parameters['properties'], t.parameters;"
        "assert asyncio.run(s.server.get_tool('find_direct_message'));"
        "assert asyncio.run(s.server.get_tool('get_messages'))"
    )
    subprocess.run([sys.executable, "-c", code], cwd=REPO, check=True)


def test_aw_list_spaces_signature():
    from aw.chat_tools import list_spaces

    fn = getattr(list_spaces, "fn", list_spaces)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    assert "resolve_names" in inspect.signature(fn).parameters
