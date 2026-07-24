"""
ArachnidWorks native-parity persistent storage helpers.

Mirrors the AW fleet pattern (aw_mcp_core/oauth.py) without depending on it: a
py-key-value-aio store backed by Firestore, wrapped with SHA-256 key hashing
(Firestore document IDs cannot contain '/') and Fernet encryption at rest.
Falls back to Valkey, then in-memory, matching the storage-backend selection
style already used in core/server.py. Persistent storage is what lets the
re-auth policy metadata survive Cloud Run cold starts.

The fork already depends on py-key-value-aio and cryptography, so nothing new
is added to the dependency set here.
"""

import hashlib
import logging
import os

logger = logging.getLogger(__name__)


class HashKeysWrapper:
    """SHA-256 hash keys before delegating to the wrapped store.

    Firestore document IDs cannot contain '/', and OAuth session keys can, so
    every key is hashed first. This is the same wrapper the AW fleet uses in
    aw_mcp_core/oauth.py, reproduced here to keep native parity (no shared
    import) with the rest of the fork.
    """

    def __init__(self, key_value):
        self.key_value = key_value

    def _hash(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    async def get(self, key, *, collection=None):
        return await self.key_value.get(key=self._hash(key), collection=collection)

    async def get_many(self, keys, *, collection=None):
        return await self.key_value.get_many(
            keys=[self._hash(k) for k in keys], collection=collection
        )

    async def put(self, key, value, *, collection=None, ttl=None):
        return await self.key_value.put(
            key=self._hash(key), value=value, collection=collection, ttl=ttl
        )

    async def put_many(self, keys, values, *, collection=None, ttl=None):
        return await self.key_value.put_many(
            keys=[self._hash(k) for k in keys],
            values=values,
            collection=collection,
            ttl=ttl,
        )

    async def delete(self, key, *, collection=None):
        return await self.key_value.delete(key=self._hash(key), collection=collection)

    async def delete_many(self, keys, *, collection=None):
        return await self.key_value.delete_many(
            keys=[self._hash(k) for k in keys], collection=collection
        )

    async def ttl(self, key, *, collection=None):
        return await self.key_value.ttl(key=self._hash(key), collection=collection)

    async def ttl_many(self, keys, *, collection=None):
        return await self.key_value.ttl_many(
            keys=[self._hash(k) for k in keys], collection=collection
        )


def _build_firestore_backend(collection: str):
    """Return a raw FirestoreStore, or None if the backend is unavailable."""
    try:
        from key_value.aio.stores.firestore import FirestoreStore

        return FirestoreStore(default_collection=collection)
    except Exception as exc:  # pragma: no cover - depends on optional extra + GCP env
        logger.warning("Firestore backend unavailable (%s)", exc)
        return None


def _build_memory_backend():
    from key_value.aio.stores.memory import MemoryStore

    return MemoryStore()


def build_encrypted_store(
    *,
    collection: str,
    storage_encryption_key: bytes,
    backend=None,
    backend_env: str = "WORKSPACE_MCP_AW_STORE_BACKEND",
):
    """Build a Fernet-encrypted, hash-keyed persistent store.

    Backend selection (env ``backend_env``):
      * "firestore" (EXPLICIT) -> FirestoreStore; if it cannot be constructed,
        raise RuntimeError. Fail closed: silently dropping to memory would
        reintroduce cold-start session loss, so an explicit request that cannot
        be honoured is a hard startup error, not a warning.
      * "memory"               -> in-memory.
      * unset / anything else (AUTO) -> Firestore if available, else in-memory
        with a warning. Silent fallback is acceptable only when no explicit
        backend was requested (local / stdio / dev).
    An explicit ``backend`` object overrides env selection (used by tests to
    inject a FileTreeStore for a real process-restart round-trip).
    """
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from cryptography.fernet import Fernet

    raw = backend
    if raw is None:
        choice = os.getenv(backend_env, "").strip().lower()
        if choice == "memory":
            raw = _build_memory_backend()
        elif choice == "firestore":
            raw = _build_firestore_backend(collection)
            if raw is None:
                raise RuntimeError(
                    f"{backend_env}=firestore was explicitly requested but the Firestore "
                    f"store for collection '{collection}' could not be created. Install "
                    "'py-key-value-aio[firestore]' and ensure GCP Firestore access. "
                    "Refusing to start with a silent in-memory fallback."
                )
        else:
            raw = _build_firestore_backend(collection)
            if raw is None:
                logger.warning(
                    "No persistent backend requested (%s unset); using in-memory AW store "
                    "for collection '%s'. Data will NOT survive a restart.",
                    backend_env,
                    collection,
                )
                raw = _build_memory_backend()

    return FernetEncryptionWrapper(
        key_value=HashKeysWrapper(raw),
        fernet=Fernet(key=storage_encryption_key),
    )
