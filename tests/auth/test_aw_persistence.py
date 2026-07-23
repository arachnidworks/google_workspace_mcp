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
        assert (await policy_a.touch("user@aw.com"))[0] == "ok"

        # New objects, same disk: the session is still known (an active slide,
        # not a fresh new_session), so the user is NOT forced to reconnect.
        policy_b = ReauthPolicyStore(
            _encrypted_disk_store(directory, key), clock=lambda: now
        )
        status, reason = await policy_b.touch("user@aw.com")
        assert status == "ok"
        assert reason == "active"

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(go(directory))


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
