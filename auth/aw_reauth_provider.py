"""
GoogleProvider subclass that enforces the AW re-auth policy on the OAuth
refresh path.

FastMCP access tokens are short-lived and refresh silently, so the refresh
exchange is the real re-auth surface: denying it with invalid_grant forces the
client to re-run Google authorization. This subclass hooks three seams of the
fork's own provider (no refactor onto aw_mcp_core, per-user credential behaviour
untouched):

  * exchange_authorization_code -> start a fresh policy window (the reset).
  * exchange_refresh_token       -> check the policy; deny (invalid_grant) when
                                    inactivity or the absolute cap is exceeded,
                                    otherwise slide the inactivity window.
  * slide_activity (called by the tool-call middleware) -> finer-grained sliding.

Sessions are identified by the provider's stable ``upstream_token_id``, resolved
from a FastMCP token's JTI exactly as the base provider does.
"""

import logging

from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import AuthorizationCode, OAuthClientInformationFull, RefreshToken, TokenError
from mcp.shared.auth import OAuthToken

from auth.aw_reauth import get_reauth_store

logger = logging.getLogger(__name__)


class AwGoogleProvider(GoogleProvider):
    """GoogleProvider + AW 5-day/30-day re-auth policy enforced on refresh."""

    async def _resolve_upstream_token_id(self, token: str, token_use: str):
        """Map a FastMCP access/refresh JWT to its stable upstream_token_id."""
        try:
            payload = self.jwt_issuer.verify_token(token, expected_token_use=token_use)
            jti = payload.get("jti")
            if not jti:
                return None
            mapping = await self._jti_mapping_store.get(key=jti)
            return mapping.upstream_token_id if mapping else None
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not resolve upstream_token_id (%s): %s", token_use, exc)
            return None

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        token = await super().exchange_authorization_code(client, authorization_code)
        store = get_reauth_store()
        if store is not None:
            uid = await self._resolve_upstream_token_id(token.access_token, "access")
            if uid:
                await store.start(uid)
                logger.info("AW re-auth: started fresh session window (%s)", uid[:8])
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        store = get_reauth_store()
        if store is not None:
            uid = await self._resolve_upstream_token_id(refresh_token.token, "refresh")
            if uid:
                status, reason = await store.check_and_slide(uid)
                if status == "reauth_required":
                    logger.info(
                        "AW re-auth: denying refresh for session %s (%s); "
                        "client must re-authenticate.",
                        uid[:8],
                        reason,
                    )
                    raise TokenError(
                        "invalid_grant",
                        f"Session re-authentication required ({reason}).",
                    )
        return await super().exchange_refresh_token(client, refresh_token, scopes)

    async def slide_activity(self, access_token: str) -> None:
        """Best-effort activity slide from an authenticated tool call."""
        store = get_reauth_store()
        if store is None:
            return
        uid = await self._resolve_upstream_token_id(access_token, "access")
        if uid:
            await store.slide(uid)
