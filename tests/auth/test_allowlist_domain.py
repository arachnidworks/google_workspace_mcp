"""
Domain-wildcard allowlist tests for _check_allowlist.

An ALLOWED_EMAILS entry starting with "@" matches any email in that domain,
by true domain equality (never a substring/endswith on the raw address).
Exact-email entries continue to work alongside domain entries.
"""

from auth import aw_policy_middleware as pm


def test_domain_entry_allows_in_domain_mixed_case(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "@arachnidworks.com")
    assert pm._check_allowlist("x@arachnidworks.com") is None
    assert pm._check_allowlist("X@Arachnidworks.COM") is None  # case-insensitive


def test_domain_entry_denies_suffix_bypass(monkeypatch):
    # Domain is arachnidworks.com.evil.com, NOT arachnidworks.com.
    monkeypatch.setenv("ALLOWED_EMAILS", "@arachnidworks.com")
    assert pm._check_allowlist("attacker@arachnidworks.com.evil.com") is not None


def test_domain_entry_denies_prefix_bypass(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "@arachnidworks.com")
    assert pm._check_allowlist("x@evil-arachnidworks.com") is not None


def test_domain_entry_denies_no_or_empty_domain(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "@arachnidworks.com")
    assert pm._check_allowlist("noatsign") is not None
    assert pm._check_allowlist("x@") is not None


def test_domain_entry_denies_when_unset(monkeypatch):
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    assert pm._check_allowlist("x@arachnidworks.com") is not None


def test_exact_entry_works_alongside_domain_entry(monkeypatch):
    monkeypatch.setenv("ALLOWED_EMAILS", "@arachnidworks.com, alice@other.com")
    # In-domain via wildcard.
    assert pm._check_allowlist("bob@arachnidworks.com") is None
    # Exact entry from a different domain.
    assert pm._check_allowlist("alice@other.com") is None
    assert pm._check_allowlist("ALICE@OTHER.COM") is None  # case-insensitive exact
    # Neither wildcard nor exact.
    assert pm._check_allowlist("mallory@other.com") is not None
