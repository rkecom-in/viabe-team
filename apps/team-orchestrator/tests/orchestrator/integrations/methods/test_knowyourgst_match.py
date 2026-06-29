"""VT-495 — knowyourgst name-matching layer unit tests (FAKE GSTSearcher; no network/creds).

Pins Fazal's matching-layer semantics: stopword normalization, the 0.72 similarity gate,
dedup-by-GSTIN, longest-token fallback, and stop-after-first-hit."""

from __future__ import annotations

import pytest

# knowyourgst_match is stdlib-only, but importing it executes orchestrator.integrations.__init__
# which pulls pydantic (absent in the dep-less smoke / lean CI test job). Skip there.
pytest.importorskip("pydantic")

from orchestrator.integrations.methods import knowyourgst_match as m  # noqa: E402

_RKECOM_GSTIN = "27AAKCR3738B1ZE"  # the real RKECOM GSTIN (public record) — valid 15-char shape


class FakeScraper:
    """Deterministic GSTSearcher: a {query_lower -> rows} map + a call recorder (order matters for
    the stop-after-first / fallback assertions)."""

    def __init__(self, mapping: dict[str, list[dict[str, str]]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def search(self, query: str) -> list[dict[str, str]]:
        self.calls.append(query)
        return self.mapping.get(query.lower(), [])


# ---------------------------------------------------------------------------
# Normalization helpers (the documented examples)
# ---------------------------------------------------------------------------

def test_normalized_key_collapses_legal_and_generic_words() -> None:
    # Both the typed and the registered forms reduce to the SAME distinctive key.
    assert m.normalized_company_key("RKECOM Services Pvt Ltd") == "rkecom"
    assert m.normalized_company_key("RKECOM SERVICES OPC PRIVATE LIMITED") == "rkecom"


def test_tokenize_strips_opc_expanded_form() -> None:
    toks = m.tokenize_company_name("Foo One Person Company")
    assert "one" not in toks and "person" not in toks and "company" not in toks
    assert toks == ["foo"]


def test_build_queries_phrase_then_longest_tokens() -> None:
    queries = m.build_search_queries("Digital Prodigy India")
    assert queries[0] == "digital prodigy india"  # most-specific phrase first
    # then individual distinctive tokens, longest first (ties keep input order)
    assert queries[1:] == ["digital", "prodigy", "india"]


def test_build_queries_raises_on_all_stopword_name() -> None:
    # An all-legal/generic name has no distinctive token — the matching layer raises (the wiring
    # leg catches this and degrades to the manual path).
    with pytest.raises(ValueError):
        m.build_search_queries("Services Pvt Ltd")


# ---------------------------------------------------------------------------
# search_company_by_similar_name semantics
# ---------------------------------------------------------------------------

def test_rkecom_example_matches_registered_name() -> None:
    rows = [{
        "company_name": "RKECOM SERVICES OPC PRIVATE LIMITED",
        "state": "Maharashtra",
        "gst_number": _RKECOM_GSTIN,
    }]
    scraper = FakeScraper({"rkecom": rows})
    out = m.search_company_by_similar_name(scraper, "RKECOM Services Pvt Ltd")
    assert out == [{
        "company_name": "RKECOM SERVICES OPC PRIVATE LIMITED",
        "state": "Maharashtra",
        "gst_number": _RKECOM_GSTIN,
    }]
    assert "_similarity" not in out[0]  # the internal sort score is stripped from the JSON shape
    assert scraper.calls == ["rkecom"]  # "services/pvt/ltd" dropped → single distinctive query


def test_below_threshold_candidate_filtered() -> None:
    # A valid GSTIN belonging to a DIFFERENT business (no distinctive-token overlap) scores < 0.72.
    rows = [{"company_name": "Sundaram Multi Pap Limited", "state": "Tamil Nadu", "gst_number": "33AAAAA0000A1Z5"}]
    out = m.search_company_by_similar_name(FakeScraper({"rkecom": rows}), "RKECOM Services Pvt Ltd")
    assert out == []


def test_dedup_by_gstin_keeps_one() -> None:
    rows = [
        {"company_name": "RKECOM SERVICES OPC PRIVATE LIMITED", "state": "MH", "gst_number": _RKECOM_GSTIN},
        {"company_name": "RKECOM SERVICES (OPC) PRIVATE LIMITED", "state": "MH", "gst_number": _RKECOM_GSTIN},
    ]
    out = m.search_company_by_similar_name(FakeScraper({"rkecom": rows}), "RKECOM Services Pvt Ltd")
    assert len(out) == 1 and out[0]["gst_number"] == _RKECOM_GSTIN


def test_longest_token_fallback_and_stop_after_first() -> None:
    # The phrase + the first individual token yield nothing; the next token ('prodigy') hits → STOP
    # (the remaining 'india' query is never issued — stop-after-first-hit avoids extra ScrapingBee calls).
    hit = [{"company_name": "DIGITAL-PRODIGY INDIA", "state": "Karnataka", "gst_number": "29AAACD1234A1Z5"}]
    scraper = FakeScraper({"prodigy": hit})
    out = m.search_company_by_similar_name(scraper, "Digital Prodigy India")
    assert len(out) == 1 and out[0]["gst_number"] == "29AAACD1234A1Z5"
    assert scraper.calls == ["digital prodigy india", "digital", "prodigy"]  # 'india' not reached


def test_drops_rows_missing_required_fields() -> None:
    # A row missing state/gstin/company_name is skipped (the JSON contract needs all three).
    rows = [
        {"company_name": "RKECOM SERVICES OPC PRIVATE LIMITED", "state": "", "gst_number": _RKECOM_GSTIN},
        {"company_name": "", "state": "MH", "gst_number": _RKECOM_GSTIN},
        {"company_name": "RKECOM SERVICES OPC PRIVATE LIMITED", "state": "MH", "gst_number": ""},
    ]
    out = m.search_company_by_similar_name(FakeScraper({"rkecom": rows}), "RKECOM Services Pvt Ltd")
    assert out == []


def test_empty_results_returns_empty() -> None:
    assert m.search_company_by_similar_name(FakeScraper({}), "RKECOM Services Pvt Ltd") == []
