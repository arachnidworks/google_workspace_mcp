"""
ArachnidWorks-branded OAuth consent page for google_workspace_mcp.

FastMCP's OAuthProxy renders its consent screen through the module-level
``create_consent_html`` symbol in ``fastmcp.server.auth.oauth_proxy.consent``.
We swap that symbol for a drop-in replacement (identical signature) that renders
the AW-branded ``consent_assets/auth.html`` instead. All of FastMCP's CSRF,
transaction, and cookie machinery is left untouched: only the HTML changes, and
the branded form posts the exact field names (txn_id, csrf_token, submit,
action) that OAuthProxy._submit_consent expects, so the OAuth flow is preserved.

The Mona Sans font is served same-origin from ``/aw-assets/mona-sans.ttf``; the
logo is inlined as a data URI (16 KB) to avoid an extra request and CSP hoops.
"""

import base64
import html as html_module
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "consent_assets"
_AUTH_HTML = _ASSETS_DIR / "auth.html"
_LOGO_PNG = _ASSETS_DIR / "assets" / "aw-logo-stack-red.png"
_FONT_TTF = _ASSETS_DIR / "fonts" / "MonaSans-VariableFont_wdth_wght.ttf"


def brand_enabled() -> bool:
    """Branding is on by default; set WORKSPACE_MCP_BRAND=off to disable."""
    if not _AUTH_HTML.exists():
        return False
    return os.getenv("WORKSPACE_MCP_BRAND", "on").strip().lower() not in ("off", "false", "0", "")


@lru_cache(maxsize=1)
def _logo_data_uri() -> str:
    try:
        data = _LOGO_PNG.read_bytes()
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("AW consent logo missing (%s)", exc)
        return ""


@lru_cache(maxsize=1)
def _template() -> str:
    return _AUTH_HTML.read_text(encoding="utf-8")


def _esc(value: Optional[str]) -> str:
    return html_module.escape(value or "", quote=True)


def aw_create_consent_html(
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    txn_id: str,
    csrf_token: str,
    client_name: Optional[str] = None,
    title: str = "Application Access Request",
    server_name: Optional[str] = None,
    server_icon_url: Optional[str] = None,
    server_website_url: Optional[str] = None,
    client_website_url: Optional[str] = None,
    csp_policy: Optional[str] = None,
    is_cimd_client: bool = False,
    cimd_domain: Optional[str] = None,
) -> str:
    """Drop-in replacement for FastMCP's create_consent_html, AW-branded.

    Signature mirrors fastmcp.server.auth.oauth_proxy.ui.create_consent_html so
    it can be swapped in transparently. A test asserts the swap target still
    exists so an upstream rename fails loudly on upgrade.
    """
    verified_domain = (
        cimd_domain
        or os.getenv("WORKSPACE_MCP_BRAND_VERIFIED_DOMAIN", "").strip()
        or (urlparse(redirect_uri).hostname or "")
    )
    help_url = os.getenv("WORKSPACE_MCP_BRAND_HELP_URL", "").strip() or "#"
    resolved_server_name = (
        server_name
        or os.getenv("WORKSPACE_MCP_BRAND_SERVER_NAME", "").strip()
        or "ArachnidWorks Google Workspace"
    )

    tokens = {
        "{{APP_NAME}}": _esc(client_name or client_id),
        "{{APP_URL}}": _esc(client_website_url or "#"),
        "{{SERVER_NAME}}": _esc(resolved_server_name),
        "{{SERVER_URL}}": _esc(server_website_url or "#"),
        "{{VERIFIED_DOMAIN}}": _esc(verified_domain),
        "{{CALLBACK_URL}}": _esc(redirect_uri),
        "{{RESPONSE_TYPE}}": "code",
        "{{SCOPE}}": _esc(" ".join(scopes) if scopes else "None"),
        # Empty action posts back to the current consent URL, exactly like
        # FastMCP's own form, so _submit_consent handles the POST.
        "{{CONSENT_ACTION}}": "",
        "{{HELP_URL}}": _esc(help_url),
        "{{TXN_ID}}": _esc(txn_id),
        "{{CSRF_TOKEN}}": _esc(csrf_token),
        "{{LOGO_SRC}}": _logo_data_uri(),
    }

    html = _template()
    for token, value in tokens.items():
        html = html.replace(token, value)
    return html


def install_branded_consent() -> bool:
    """Swap FastMCP's consent HTML generator for the AW-branded one.

    Returns True if the swap was applied. Idempotent and safe to call more than
    once. No-op (returns False) when branding is disabled or assets are missing.
    """
    if not brand_enabled():
        logger.info("AW consent branding disabled or assets missing; using default consent page.")
        return False
    try:
        from fastmcp.server.auth.oauth_proxy import consent as _consent_mod

        if not hasattr(_consent_mod, "create_consent_html"):
            # Upstream renamed the symbol: fail loud rather than silently ship
            # the default page.
            raise AttributeError(
                "fastmcp.server.auth.oauth_proxy.consent.create_consent_html not found; "
                "FastMCP internals changed. Update auth/aw_consent.py."
            )
        _consent_mod.create_consent_html = aw_create_consent_html
        logger.info("AW-branded consent page installed.")
        return True
    except Exception as exc:
        logger.error("Failed to install AW-branded consent page: %s", exc)
        raise


def register_consent_assets(server) -> None:
    """Register the same-origin font route the branded consent page references."""
    if not brand_enabled():
        return

    from starlette.requests import Request
    from starlette.responses import FileResponse, Response

    @server.custom_route("/aw-assets/mona-sans.ttf", methods=["GET"])
    async def _mona_sans(request: Request):  # noqa: ANN202 - starlette handler
        if not _FONT_TTF.exists():
            return Response(status_code=404)
        return FileResponse(
            path=str(_FONT_TTF),
            media_type="font/ttf",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    logger.info("Registered AW consent font route: /aw-assets/mona-sans.ttf")
