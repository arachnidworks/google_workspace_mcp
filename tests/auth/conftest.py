"""Test fixtures scoped to the auth test suite.

The AW native-parity tests drive their coroutines with ``asyncio.run()``, which
closes its event loop and clears the thread-global loop on exit. Later suites
(e.g. tests/gcontacts) still use the deprecated ``asyncio.get_event_loop()``,
which then raises "There is no current event loop". Restore a usable loop after
each auth test so collection order stays harmless.
"""

import asyncio

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
