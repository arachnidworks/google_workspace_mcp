"""
Persistent store round-trip across a simulated process restart.

Uses a FileTreeStore in a temp directory so two independent store objects
(standing in for before/after a Cloud Run cold start) share the same durable
backend. Proves the Fernet-encrypted, SHA-256-hashed store the fork will use for
the re-auth policy actually persists and decrypts across instances, and that the
re-auth policy state survives with it.
"""

import asyncio
import tempfile
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from key_value.aio.stores.filetree import FileTreeStore

from auth.aw_persistence import build_encrypted_store
from auth.aw_reauth import ReauthPolicyStore


def _encrypted_disk_store(directory, key):
    return build_encrypted_store(
        collection="aw_reauth_policy",
        storage_encryption_key=key,
        backend=FileTreeStore(data_directory=directory),
    )


def test_encrypted_store_round_trips_across_restart():
    key = Fernet.generate_key()

    async def go(directory):
        # "Before restart": write a value.
        store_a = _encrypted_disk_store(directory, key)
        await store_a.put(
            "user@aw.com", {"hello": "world"}, collection="aw_reauth_policy"
        )

        # "After restart": a fresh store object over the same directory reads it.
        store_b = _encrypted_disk_store(directory, key)
        got = await store_b.get("user@aw.com", collection="aw_reauth_policy")
        assert got == {"hello": "world"}

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(go(directory))


def test_reauth_state_survives_restart():
    key = Fernet.generate_key()
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)

    async def go(directory):
        policy_a = ReauthPolicyStore(
            _encrypted_disk_store(directory, key), clock=lambda: now
        )
        await policy_a.start("upstream-token-1")

        # New objects, same disk: the session is still known (an active slide,
        # not a forced re-auth), so the user is NOT bounced on a cold start.
        policy_b = ReauthPolicyStore(
            _encrypted_disk_store(directory, key), clock=lambda: now
        )
        status, reason = await policy_b.check_and_slide("upstream-token-1")
        assert status == "ok"
        assert reason == "active"

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(go(directory))


def test_explicit_firestore_fails_closed_when_unavailable(monkeypatch):
    # Explicit backend request that cannot be honoured must raise, not silently
    # drop to in-memory (which would reintroduce cold-start session loss).
    import auth.aw_persistence as persistence

    monkeypatch.setenv("TEST_AW_BACKEND", "firestore")
    monkeypatch.setattr(
        persistence, "_build_firestore_backend", lambda collection: None
    )
    try:
        persistence.build_encrypted_store(
            collection="c",
            storage_encryption_key=Fernet.generate_key(),
            backend_env="TEST_AW_BACKEND",
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "explicit firestore selection must fail closed when unavailable"


def test_unset_backend_falls_back_to_memory(monkeypatch):
    # No explicit backend: silent in-memory fallback is acceptable (dev/stdio).
    import auth.aw_persistence as persistence

    monkeypatch.delenv("TEST_AW_BACKEND", raising=False)
    monkeypatch.setattr(
        persistence, "_build_firestore_backend", lambda collection: None
    )
    store = persistence.build_encrypted_store(
        collection="c",
        storage_encryption_key=Fernet.generate_key(),
        backend_env="TEST_AW_BACKEND",
    )
    assert store is not None  # built with a memory fallback, no exception


def test_wrong_key_cannot_read():
    async def go(directory):
        store_a = _encrypted_disk_store(directory, Fernet.generate_key())
        await store_a.put("u@aw.com", {"secret": "x"}, collection="aw_reauth_policy")

        store_b = _encrypted_disk_store(directory, Fernet.generate_key())
        # Different Fernet key: the ciphertext cannot be decrypted, so nothing
        # usable comes back (None or an error swallowed to None by the wrapper).
        try:
            got = await store_b.get("u@aw.com", collection="aw_reauth_policy")
        except Exception:
            got = None
        assert got != {"secret": "x"}

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(go(directory))
