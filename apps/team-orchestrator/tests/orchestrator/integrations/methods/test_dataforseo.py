"""VT-778 — DataForSEO AI Mode discovery leg.

The two things worth pinning are the ones that were actually wrong in production:
the query SHAPE (a natural-language question retrieved almost nothing) and the CITY
placement (in the keyword it restricts; as geo targeting it prefers). Plus the safety
property: GSTINs come from CITED REFERENCES only, never the model's synthesised prose.
"""
from __future__ import annotations

from typing import Any

import pytest

# The dep-less smoke gate runs this suite WITHOUT the heavy deps. Importing the module pulls
# orchestrator.integrations.__init__ -> registry -> schemas -> pydantic, so guard the import or
# collection dies for the whole file (not just these tests). Verify with:
#   uv run --no-project --isolated --with pytest --with pyyaml pytest <this file>
pytest.importorskip("pydantic")

from orchestrator.integrations.methods import dataforseo as d4s  # noqa: E402

_REAL = "27AADCS7829K1ZT"          # Sundaram Multi Pap Ltd — "Sundaram" is that company's brand
_OTHER = "33ADPPA8636E1ZN"         # a genuinely different company


def _payload(refs: list[dict[str, Any]], prose_gstin: str = "") -> dict[str, Any]:
    """An AI Mode response: `references` are cited sources, everything else is prose."""
    item: dict[str, Any] = {"type": "ai_overview", "references": refs}
    if prose_gstin:
        item["markdown"] = f"You can verify their GSTIN, for example {prose_gstin}, on the portal."
    return {"tasks": [{"result": [{"items": [item]}]}]}


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    monkeypatch.setattr(d4s, "_cache", {})
    monkeypatch.setattr(d4s, "_locations", {"mumbai": 1007785, "rajkot": 1007759})
    monkeypatch.setenv(d4s._KEY_ENV, "x")


def test_gstin_in_prose_only_is_never_returned():
    """The whole safety property. AI Mode was measured emitting a well-formed GSTIN for a
    business it had cited no source for — an illustrative number in synthesised prose."""
    rows = d4s.search_gstins(
        "Sundaram Book Store", "Mumbai",
        fetch_fn=lambda q: _payload(
            [{"title": "Sundaram Book Store - Justdial", "domain": "justdial.com"}],
            prose_gstin=_REAL,
        ),
    )
    assert rows == [], "a GSTIN that only appeared in prose was surfaced as a candidate"


def test_gstin_from_a_cited_reference_is_returned():
    rows = d4s.search_gstins(
        "Sundaram Book Store", "Mumbai",
        fetch_fn=lambda q: _payload(
            [{"title": f"Sundaram Multi Pap Ltd - {_REAL} - Maharashtra",
              "domain": "knowyourgst.com"}]
        ),
    )
    assert [r["gst_number"] for r in rows] == [_REAL]


def test_cited_gstin_for_an_unrelated_business_is_dropped():
    """AI Mode cites competitors, suppliers and generic "GST registration" pages. A cited
    GSTIN must ALSO sit in text echoing a distinctive token of the queried name."""
    rows = d4s.search_gstins(
        "Balaji Wafers", "Rajkot",
        fetch_fn=lambda q: _payload(
            [{"title": f"Sundaram Super Store - {_OTHER} - Tamil Nadu",
              "domain": "knowyourgst.com"}]
        ),
    )
    assert rows == []


def test_query_is_keyword_shaped_and_carries_no_city():
    """Both production defects in one assertion.

    A natural-language question retrieved 2 references where the keyword form retrieved 6
    (with the correct GSTIN cited twice), and extra qualifier words lost the answer entirely.
    The city must NOT appear in the keyword: there it restricts the result set instead of
    preferring local results (Fazal 2026-08-22) — it belongs in the geo targeting."""
    seen: list[str] = []
    d4s.search_gstins("Sundaram Book Store", "Mumbai",
                      fetch_fn=lambda q: (seen.append(q), _payload([]))[1])
    assert seen == ["Sundaram Book Store GSTIN"]
    assert "mumbai" not in seen[0].lower(), "city leaked into the keyword — that restricts"
    assert "?" not in seen[0], "question form starves retrieval"


def test_city_becomes_a_numeric_location_code():
    """Geo is not cosmetic: India-wide scored 0/4 on Sundaram, Mumbai 4/4."""
    assert d4s._location_code_for("Mumbai") == 1007785
    assert d4s._location_code_for("mumbai") == 1007785, "resolution must be case-insensitive"


def test_unknown_city_falls_back_instead_of_failing():
    """An unresolvable IP city must degrade discovery to India-wide, never break signup."""
    assert d4s._location_code_for("Zzznowhere") is None
    assert d4s._location_code_for("") is None


def test_short_name_skips_the_billed_call():
    called: list[str] = []
    assert d4s.search_gstins("ab", "Mumbai",
                             fetch_fn=lambda q: (called.append(q), _payload([]))[1]) == []
    assert called == []


def test_vendor_error_degrades_to_empty():
    """Discovery is best-effort and must never raise into signup."""
    def _boom(_q: str) -> dict[str, Any]:
        raise RuntimeError("dataforseo: AI Mode returned HTTP 402 (body withheld)")

    assert d4s.search_gstins("Sundaram Book Store", "Mumbai", fetch_fn=_boom) == []
