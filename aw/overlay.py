"""
AW overlay on the upstream server (core/server.py stays upstream-owned).

Importing this module patches core.server in place, so it must run before
main.py binds configure_server_for_http (aw_main.py does that). It adds:

  * AwPolicyMiddleware: audit line, ALLOWED_EMAILS gate, re-auth activity slide.
  * AwGoogleProvider in place of WorkspaceGoogleProvider: 5-day/30-day re-auth
    enforced on the refresh path.
  * Firestore OAuth proxy client_storage when
    WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=firestore (upstream knows only
    valkey/disk/memory and leaves client_storage None for anything else).
  * The persistent re-auth policy store and the AW-branded consent page.
"""

import logging
import os

import core.server as _server
from auth.aw_policy_middleware import AwPolicyMiddleware
from auth.aw_reauth_provider import AwGoogleProvider

logger = logging.getLogger(__name__)

_upstream_configure_server_for_http = _server.configure_server_for_http
_aw_consent_ready = False


def _firestore_client_storage(jwt_signing_key: bytes):
    """Firestore-backed, hash-keyed, Fernet-encrypted OAuth proxy client_storage.

    Fails closed: Firestore was explicitly selected, so a silent in-memory
    fallback would bring back the cold-start session loss it exists to fix.
    """
    try:
        from cryptography.fernet import Fernet
        from fastmcp.server.auth.jwt_issuer import derive_jwt_key
        from key_value.aio.stores.firestore import FirestoreStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        from auth.aw_persistence import HashKeysWrapper

        collection = (
            os.getenv("WORKSPACE_MCP_OAUTH_PROXY_FIRESTORE_COLLECTION", "").strip()
            or "workspace_mcp_oauth_proxy"
        )
        storage_encryption_key = derive_jwt_key(
            high_entropy_material=jwt_signing_key.decode(),
            salt="fastmcp-storage-encryption-key",
        )
        storage = FernetEncryptionWrapper(
            key_value=HashKeysWrapper(FirestoreStore(default_collection=collection)),
            fernet=Fernet(key=storage_encryption_key),
        )
        logger.info(
            "OAuth 2.1: Using FirestoreStore for FastMCP OAuth proxy client_storage (collection=%s)",
            collection,
        )
        return storage
    except Exception as exc:
        raise RuntimeError(
            "OAuth 2.1: Firestore client_storage was explicitly requested "
            "(WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND=firestore) but could not "
            f"be initialized: {exc}. Install 'py-key-value-aio[firestore]' and "
            "ensure GCP Firestore access, or choose another backend. Refusing to "
            "start with a silent in-memory fallback."
        ) from exc


def _aw_provider(**kwargs):
    """Stands in for WorkspaceGoogleProvider inside configure_server_for_http."""
    backend = os.getenv("WORKSPACE_MCP_OAUTH_PROXY_STORAGE_BACKEND", "").strip().lower()
    if backend == "firestore":
        kwargs["client_storage"] = _firestore_client_storage(kwargs["jwt_signing_key"])
    provider = AwGoogleProvider(**kwargs)
    provider._aw_jwt_signing_key = kwargs["jwt_signing_key"]
    return provider


def _configure_aw_native_parity(jwt_signing_key: bytes) -> None:
    """Set up the persistent re-auth policy store and AW-branded consent page.

    Failures are logged but never abort startup: the server keeps working with
    the re-auth policy disabled rather than refusing to serve.
    """
    global _aw_consent_ready

    try:
        from fastmcp.server.auth.jwt_issuer import derive_jwt_key

        from auth.aw_persistence import build_encrypted_store
        from auth.aw_reauth import ReauthPolicyStore, set_reauth_store

        reauth_collection = (
            os.getenv("WORKSPACE_MCP_AW_REAUTH_COLLECTION", "").strip()
            or "aw_reauth_policy"
        )
        reauth_key = derive_jwt_key(
            high_entropy_material=jwt_signing_key.decode(),
            salt="aw-reauth-policy-encryption-key",
        )
        backing = build_encrypted_store(
            collection=reauth_collection, storage_encryption_key=reauth_key
        )
        set_reauth_store(ReauthPolicyStore(backing, collection=reauth_collection))
        logger.info(
            "AW re-auth policy store configured (collection=%s)", reauth_collection
        )
    except Exception as exc:
        logger.error("Failed to configure AW re-auth policy store: %s", exc)

    try:
        from auth.aw_consent import install_branded_consent, register_consent_assets

        install_branded_consent()
        if not _aw_consent_ready:
            register_consent_assets(_server.server)
            _aw_consent_ready = True
    except Exception as exc:
        logger.error("Failed to set up AW branded consent: %s", exc)


def configure_server_for_http():
    _upstream_configure_server_for_http()
    provider = _server.get_auth_provider()
    if isinstance(provider, AwGoogleProvider):
        _configure_aw_native_parity(provider._aw_jwt_signing_key)


# Runs after AuthInfoMiddleware (added at core.server import), so identity is resolved.
_server.server.add_middleware(AwPolicyMiddleware())
_server.WorkspaceGoogleProvider = _aw_provider
_server.configure_server_for_http = configure_server_for_http
