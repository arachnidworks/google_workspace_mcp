"""
Wiring + auth-flow-preservation tests.

Confirms the AW additions are registered without disturbing the fork's existing
auth flow, and that the consent branding hooks into a FastMCP symbol that still
exists (so an upstream rename fails loudly on upgrade rather than silently
serving the default page).
"""

import importlib

import auth.aw_consent as aw_consent


def test_both_middlewares_registered_in_order():
    import core.server as s

    names = [type(m).__name__ for m in s.server.middleware]
    assert "AuthInfoMiddleware" in names
    assert "AwPolicyMiddleware" in names
    # Policy middleware runs after auth-info so identity is already resolved.
    assert names.index("AwPolicyMiddleware") > names.index("AuthInfoMiddleware")


def test_fastmcp_consent_swap_target_still_exists():
    # Upgrade canary: if FastMCP renames/removes this symbol, install fails loud.
    consent = importlib.import_module("fastmcp.server.auth.oauth_proxy.consent")
    assert hasattr(consent, "create_consent_html")


def test_branded_consent_signature_matches_stock():
    # Signature parity: our drop-in must accept exactly the params FastMCP's
    # generator does, so a signature change (not just a rename) fails here.
    import inspect

    ui = importlib.import_module("fastmcp.server.auth.oauth_proxy.ui")
    stock = inspect.signature(ui.create_consent_html)
    ours = inspect.signature(aw_consent.aw_create_consent_html)
    assert list(ours.parameters) == list(stock.parameters)


def test_install_branded_consent_swaps_symbol(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_BRAND", "on")
    consent = importlib.import_module("fastmcp.server.auth.oauth_proxy.consent")
    original = consent.create_consent_html
    try:
        assert aw_consent.install_branded_consent() is True
        assert consent.create_consent_html is aw_consent.aw_create_consent_html
    finally:
        consent.create_consent_html = original


def test_branding_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WORKSPACE_MCP_BRAND", "off")
    assert aw_consent.brand_enabled() is False
    assert aw_consent.install_branded_consent() is False


def test_branded_form_fields_match_fastmcp_submit_handler():
    # The AW consent form must post the exact fields OAuthProxy._submit_consent
    # reads, or the OAuth flow breaks. This ties the branding to the flow.
    html = aw_consent.aw_create_consent_html(
        client_id="c",
        redirect_uri="https://claude.ai/cb",
        scopes=["openid"],
        txn_id="TXN",
        csrf_token="CSRF",
        client_name="Claude",
    )
    assert 'name="txn_id" value="TXN"' in html
    assert 'name="csrf_token" value="CSRF"' in html
    assert 'name="submit" value="true"' in html
    assert 'name="action" value="approve"' in html
    assert 'name="action" value="deny"' in html
    # font served same-origin; logo inlined
    assert "/aw-assets/mona-sans.ttf" in html
    assert "data:image/png;base64," in html
