"""VT-406 — entity-match unit tests (injected fns; no network/creds/DB)."""

from __future__ import annotations

from typing import Any

from orchestrator.onboarding import entity_match

_VALID_GSTIN = "29ABCDE1234F1Z5"  # 2 state + PAN(ABCDE1234F) + entity '1' + 'Z' + checksum '5'


def test_fetch_candidates_extracts_gstin_from_web_results() -> None:
    def search_fn(query: str) -> list[dict[str, Any]]:
        assert "GST number" in query
        return [
            {"title": "Sundaram Book Store", "description": f"GSTIN {_VALID_GSTIN} active", "url": "x"},
            {"title": "No gstin here", "description": "nothing"},
        ]

    cands = entity_match.fetch_candidates("Sundaram Book Store", "Chennai", search_fn=search_fn, gbp_fetch_fn=lambda *_: [])
    assert len(cands) == 1
    assert cands[0].candidate_gstin == _VALID_GSTIN
    assert cands[0].source == "web"
    assert cands[0].trade_name == "Sundaram Book Store"


def _gbp_one(*_: Any) -> list[dict[str, Any]]:
    return [{"title": "Sundaram Multi Pap Limited", "categoryName": "Stationery", "city": "Chennai"}]


def test_fetch_candidates_gbp_leg_has_no_gstin() -> None:
    cands = entity_match.fetch_candidates("Sundaram", "Chennai", search_fn=lambda _q: [], gbp_fetch_fn=_gbp_one)
    assert len(cands) == 1
    assert cands[0].source == "gbp"
    assert cands[0].candidate_gstin is None
    assert "Stationery" in (cands[0].detail or "")


def test_fetch_candidates_empty_name_returns_empty() -> None:
    assert entity_match.fetch_candidates("", "Chennai") == []


def test_fetch_candidates_web_failure_degrades_to_gbp() -> None:
    def boom(_q: str) -> list[dict[str, Any]]:
        raise RuntimeError("search down")

    def gbp(*_: Any) -> list[dict[str, Any]]:
        return [{"title": "Sundaram Books", "city": "Chennai"}]

    cands = entity_match.fetch_candidates("Sundaram", "Chennai", search_fn=boom, gbp_fetch_fn=gbp)
    assert [c.source for c in cands] == ["gbp"]  # web degraded, gbp survived


def test_confirm_rejects_malformed_gstin_without_calling_sandbox() -> None:
    called = {"n": 0}

    def lookup(_t: Any, _g: str) -> dict[str, Any]:
        called["n"] += 1
        return {"ok": True}

    out = entity_match.confirm_and_verify("t1", "NOTAGSTIN", lookup_fn=lookup)
    assert out["reason"] == "invalid_gstin_format"
    assert called["n"] == 0  # never hit the vendor on a malformed id


def test_confirm_verified_persists_anchor_and_seeds_discovery(monkeypatch) -> None:
    anchor: dict[str, Any] = {}
    seeded: dict[str, Any] = {}
    monkeypatch.setattr(
        "orchestrator.knowledge.l1.upsert_business_profile",
        lambda tid, attrs: anchor.update(tid=tid, attrs=attrs) or __import__("uuid").uuid4(),
    )
    monkeypatch.setattr(entity_match, "_seed_discovery", lambda *a, **k: seeded.update(called=True))

    def lookup(tid: Any, g: str) -> dict[str, Any]:
        return {"ok": True, "status": "gstin_verified", "gstin": g, "name": "Sundaram Multi Pap Limited"}

    out = entity_match.confirm_and_verify("t1", _VALID_GSTIN, lookup_fn=lookup)
    assert out["status"] == "gstin_verified"
    a = anchor["attrs"]["business_entity_anchor"]
    assert a["gstin"] == _VALID_GSTIN
    assert a["source"] == "sandbox" and a["verified"] is True
    assert a["trade_name"] == "Sundaram Multi Pap Limited"
    assert seeded.get("called") is True


def test_confirm_vendor_down_returns_unverified_no_anchor(monkeypatch) -> None:
    persisted = {"n": 0}
    monkeypatch.setattr(entity_match, "_persist_anchor", lambda *a, **k: persisted.update(n=persisted["n"] + 1))
    out = entity_match.confirm_and_verify(
        "t1", _VALID_GSTIN, lookup_fn=lambda _t, _g: {"ok": False, "reason": "vendor_down", "status": "unverified"}
    )
    assert out["reason"] == "vendor_down"
    assert persisted["n"] == 0  # no anchor on a non-verified result
