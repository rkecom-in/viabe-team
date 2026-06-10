"""VT-366 — SSRF guard on the Auto-Discovery website source. The fetched URL comes from a GBP
listing (attacker-influenceable) or owner input, fetched server-side → must be public http(s)."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator.onboarding.auto_discovery_sources import (  # noqa: E402
    UnsafeUrlError,
    _assert_public_url,
    discover_website,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud-metadata (the classic SSRF target)
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "http://user:pw@example.com/",
        "http:///nohost",
    ],
)
def test_assert_public_url_rejects_unsafe(url):
    with pytest.raises(UnsafeUrlError):
        _assert_public_url(url)


def test_assert_public_url_accepts_public():
    _assert_public_url("https://example.com/")  # resolves to a public address → no raise


def test_discover_website_unsafe_url_is_fail_soft(monkeypatch):
    """An internal/unsafe URL must NOT be fetched and must degrade (status 'error'), never crash the
    engine — _fetch_website's _assert_public_url raises before any socket connect."""
    monkeypatch.setattr(
        "orchestrator.onboarding.auto_discovery_sources.write_draft",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write on an unsafe URL")),
    )
    r = discover_website("t", {"website": "http://169.254.169.254/"})
    assert r.status == "error" and r.fields == {}
