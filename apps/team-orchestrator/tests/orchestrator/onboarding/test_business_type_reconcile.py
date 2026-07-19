"""VT-475 — unit tests for business-TYPE reconciliation (the RKeCom mis-classification fix).

No network, no key, no DB: the deterministic cross-check runs with NO LLM; the LLM leg is exercised
through the injectable ``reconcile_fn`` seam (a fake that returns a taxonomy key, and a fake that
raises). The ACCEPTANCE test is ``test_rkecom_adversarial`` — the mis-categorized GBP
'Telecommunications service provider' must NOT win against the rkecom.in domain + name.
"""

from __future__ import annotations

from orchestrator.onboarding.business_type_reconcile import (
    ReconciledType,
    is_valid_business_type,
    reconcile_business_type,
    taxonomy_label,
)

# --------------------------------------------------------------------------- taxonomy plumbing


def test_taxonomy_loads_real_yaml():
    # The coarse buckets from config/business_types.yaml are loadable + range-checkable.
    assert is_valid_business_type("services")
    assert is_valid_business_type("sweets")
    assert is_valid_business_type("other")  # the floor is always present
    assert not is_valid_business_type("telecommunications")  # not a Viabe bucket
    assert not is_valid_business_type(None)


def test_taxonomy_label_resolves_key_to_human_label():
    en, hi = taxonomy_label("sweets")
    assert "Sweets" in en or "sweets" in en.lower()
    assert hi  # bilingual label present
    # unknown key → falls back to the key itself (never raises)
    assert taxonomy_label("nonexistent_key") == ("nonexistent_key", "nonexistent_key")


# --------------------------------------------------------------------------- THE ACCEPTANCE TEST


def test_rkecom_adversarial_deterministic():
    """ADVERSARIAL (the VT-475 acceptance test): GBP mis-categorized RKeCom as a 'Telecommunications
    service provider'. With {GBP category, rkecom.in domain, 'RKeCom Services' name} and NO LLM, the
    reconciliation MUST NOT output telecom — the domain+name dominate the mis-categorized GBP field."""
    out = reconcile_business_type(
        business_name="RKeCom Services",
        gbp_category="Telecommunications service provider",
        website="https://rkecom.in",
        # no reconcile_fn → deterministic path only (no LLM, no key)
    )
    assert isinstance(out, ReconciledType)
    assert out.business_type != "telecommunications"
    assert is_valid_business_type(out.business_type)
    # rkecom.in / 'RKeCom Services' (ecom/commerce) → the e-commerce/online-retail bucket ('services'),
    # NEVER a telecom one. The raw GBP category is recorded but did NOT lead.
    assert out.business_type == "services"
    assert out.raw_gbp_category == "Telecommunications service provider"
    # the conflict is reflected: GBP didn't win, so confidence isn't 'high-because-everyone-agreed'
    assert out.confidence in {"medium", "low"}
    assert "domain" in out.signals_used


def test_rkecom_adversarial_with_llm_seam():
    """Same case through the injectable LLM seam (mocked, no real key): a reconcile_fn that returns a
    taxonomy key is honoured + range-checked — still NOT telecom."""
    out = reconcile_business_type(
        business_name="RKeCom Services",
        gbp_category="Telecommunications service provider",
        website="https://rkecom.in",
        reconcile_fn=lambda name, cat, domain, nature: "services",  # the LLM resolves it correctly
    )
    assert out.business_type == "services"
    assert out.business_type != "telecommunications"
    assert "llm" in out.signals_used
    assert out.confidence == "high"


# --------------------------------------------------------------------------- clean / agreeing case


def test_clean_case_gbp_and_domain_agree():
    """GBP + domain agree → that type, high confidence (the common happy path is undisturbed)."""
    out = reconcile_business_type(
        business_name="Sharma Sweets",
        gbp_category="Sweet shop",
        website="https://sharmasweets.example",  # name+category both say sweets
    )
    assert out.business_type == "sweets"
    assert out.confidence == "high"


def test_sane_gbp_category_with_no_other_signal_is_honoured():
    # A correct GBP category should still flow when there's nothing to contradict it.
    out = reconcile_business_type(gbp_category="Pharmacy")
    assert out.business_type == "pharmacy"


def test_domain_keyword_overrides_conflicting_gbp_category():
    """Even a GBP category that DOES map to a bucket loses to a contradicting domain (generalised
    RKeCom: the business's own domain is the trustworthy signal)."""
    out = reconcile_business_type(
        business_name="Acme",
        gbp_category="Restaurant",  # maps to 'restaurant'…
        website="https://acmepharmacy.in",  # …but the domain says pharmacy
    )
    assert out.business_type == "pharmacy"
    assert out.confidence == "medium"  # conflict → not 'high'


# --------------------------------------------------------------------------- fail-soft


def test_llm_failure_falls_back_to_domain_preferred_deterministic():
    """LLM down (reconcile_fn raises) → deterministic fallback, NO crash, and it STILL prefers the
    domain over the conflicting GBP category (the RKeCom guarantee survives an LLM outage)."""

    def boom(name, cat, domain, nature):
        raise RuntimeError("llm down")

    out = reconcile_business_type(
        business_name="RKeCom Services",
        gbp_category="Telecommunications service provider",
        website="https://rkecom.in",
        reconcile_fn=boom,
    )
    assert out.business_type == "services"  # domain-preferred fallback
    assert out.business_type != "telecommunications"


def test_llm_out_of_taxonomy_output_is_discarded():
    """An LLM that returns junk / an out-of-taxonomy string is range-checked OUT → deterministic
    cross-check stands (never trust a raw out-of-range LLM key)."""
    out = reconcile_business_type(
        business_name="RKeCom Services",
        gbp_category="Telecommunications service provider",
        website="https://rkecom.in",
        reconcile_fn=lambda *a: "telecommunications",  # not a Viabe bucket
    )
    assert out.business_type == "services"  # fell back, did NOT echo the junk
    assert out.business_type != "telecommunications"


def test_no_signal_floors_to_other():
    out = reconcile_business_type()  # nothing at all
    assert out.business_type == "other"
    assert out.confidence == "low"


def test_maps_url_is_not_treated_as_a_domain():
    """A GBP fallback maps.google url is the LISTING, not the business's own site → carries no
    business-type signal, so it doesn't spuriously drive the type."""
    out = reconcile_business_type(
        business_name="Mystery Shop",
        gbp_category="Restaurant",
        website="https://maps.google/place/123",  # a maps url, not a real domain
    )
    # the maps url contributes nothing; GBP 'Restaurant' (no contradiction) wins.
    assert out.business_type == "restaurant"


def test_never_raises_on_garbage_inputs():
    # Defensive: weird inputs must degrade, never raise (fail-soft into discovery).
    for kwargs in (
        {"website": "not a url"},
        {"website": "ftp://x"},
        {"gbp_category": ""},
        {"business_name": "   "},
    ):
        out = reconcile_business_type(**kwargs)
        assert isinstance(out, ReconciledType)
        assert is_valid_business_type(out.business_type)


def test_gst_nature_as_list_does_not_no_op_the_reconcile():
    """REGRESSION (the 63211ce5 silent no-op): the GST verify writes ``nature_of_business`` as a
    ``list[str]`` (``sandbox_kyc.GstinLookup.nature_of_business``), and the VT-478 recompose passes
    that list verbatim as ``gst_nature``. The keyword matcher's ``t.lower()`` raised
    ``AttributeError`` on a list → the reconcile failed-soft to a no-op → the raw mis-categorized GBP
    'Telecommunications service provider' confirm re-surfaced. The reconciler MUST accept the list
    shape, use its keywords, and still beat the mis-category."""
    out = reconcile_business_type(
        business_name="Reecomps teleservices pvt. ltd",
        gbp_category="Telecommunications service provider",
        website="http://reecomps.in/",
        gst_nature=["Supplier of Services", "Others", "Warehouse / Depot"],
    )
    assert isinstance(out, ReconciledType)
    assert is_valid_business_type(out.business_type)
    assert out.business_type != "telecommunications"  # the mis-category must not win
    assert out.business_type == "services"  # domain+name+gst all point at services
    assert "gst_nature" in out.signals_used  # the list signal was actually consumed


def test_gst_nature_empty_list_is_no_signal():
    # An empty list carries no GST signal — must not raise, must not invent one.
    out = reconcile_business_type(
        business_name="RKeCom Services",
        gbp_category="Telecommunications service provider",
        website="https://rkecom.in",
        gst_nature=[],
    )
    assert isinstance(out, ReconciledType)
    assert out.business_type == "services"
    assert "gst_nature" not in out.signals_used
