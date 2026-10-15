"""Fixtures for the AW overlay tests.

The AW tests drive coroutines with ``asyncio.run()``, which closes its loop and
clears the thread-global one. Later suites (e.g. tests/gcontacts) still call
``asyncio.get_event_loop()``, so restore a usable loop after each AW test.
"""

import asyncio
import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_event_loop():
    yield
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def overlay():
    """Apply aw.overlay to core.server for one test, then undo it."""
    import core.server as s

    saved = (s.WorkspaceGoogleProvider, s.configure_server_for_http)
    saved_middleware = list(s.server.middleware)
    sys.modules.pop("aw.overlay", None)
    try:
        yield importlib.import_module("aw.overlay")
    finally:
        s.WorkspaceGoogleProvider, s.configure_server_for_http = saved
        s.server.middleware[:] = saved_middleware
        sys.modules.pop("aw.overlay", None)
