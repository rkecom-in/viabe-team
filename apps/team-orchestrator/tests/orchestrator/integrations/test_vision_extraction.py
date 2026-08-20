"""VT-52 — unit tests for the shared Vision-LLM extraction primitive.

No network: the Anthropic client is injected (fake) and the consent predicate is
injected. Pillow is required for the preprocessing tests.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

pytest.importorskip("PIL")
pytest.importorskip("anthropic")
pytest.importorskip("pydantic")

from orchestrator.integrations.vision_extraction import (  # noqa: E402
    ConsentRejectedError,
    ExtractedField,
    ImagePreprocessError,
    VisionExtractionError,
    _preprocess_image,
    _resolve_vision_model,
    extract_from_image,
    route_field,
)

_TENANT = UUID("11111111-1111-4111-8111-111111111111")
_ALLOW = lambda _tid: True  # noqa: E731 — test consent predicate
_DENY = lambda _tid: False  # noqa: E731


class _FakeCall:
    """VT-732 transport double: vision goes through the multi-provider seam, so the injected object
    is a ``messages_call``-shaped callable returning a response with ``.content``."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict] = []

    def __call__(self, tier: str, **kw):
        self.calls.append({"tier": tier, **kw})
        return SimpleNamespace(content=self._text)


def _exploding_call(tier: str, **kw):  # noqa: ARG001
    """Any transmission attempt fails the test (proves fail-closed)."""
    raise AssertionError("transmitted to the vision model despite no consent")


def _png_bytes(w: int = 32, h: int = 32, color=(200, 180, 160)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# --- consent gate (fail-closed) ------------------------------------------------

def test_consent_absent_fails_closed_no_transmission():
    with pytest.raises(ConsentRejectedError):
        extract_from_image(
            _png_bytes(),
            tenant_id=_TENANT,
            target_fields=["customer_name"],
            acquired_via="paper_book",
            media_type="image/png",
            call=_exploding_call,  # must NOT be called
            consent_check=_DENY,
        )


# --- happy path: per-field confidence -----------------------------------------

def test_extract_returns_per_field_confidence():
    payload = json.dumps(
        {
            "fields": [
                {"name": "customer_name", "value": "Asha", "confidence": 0.92},
                {"name": "phone", "value": "9000000001", "confidence": 0.71},
            ]
        }
    )
    client = _FakeCall(payload)
    out = extract_from_image(
        _png_bytes(),
        tenant_id=_TENANT,
        target_fields=["customer_name", "phone"],
        acquired_via="paper_book",
        media_type="image/png",
        call=client,
        consent_check=_ALLOW,
    )
    assert out.acquired_via == "paper_book"
    assert [f.name for f in out.fields] == ["customer_name", "phone"]
    assert out.fields[0].confidence == 0.92
    assert out.fields[1].value == "9000000001"
    # transmitted exactly once, on the env-governed vision tier, with an image + a text block.
    assert len(client.calls) == 1
    assert client.calls[0]["tier"] == "specialist"
    content = client.calls[0]["messages"][0].content
    assert any(b["type"] == "image" for b in content)
    assert any(b["type"] == "text" for b in content)


def test_empty_value_becomes_none_never_guessed():
    # P4: an unreadable field is null, not invented.
    payload = json.dumps(
        {"fields": [{"name": "email", "value": "", "confidence": 0.3}]}
    )
    out = extract_from_image(
        _png_bytes(),
        tenant_id=_TENANT,
        target_fields=["email"],
        acquired_via="contacts",
        media_type="image/png",
        call=_FakeCall(payload),
        consent_check=_ALLOW,
    )
    assert out.fields[0].value is None


# --- Pillar 8: malformed output raises (no regex repair) ----------------------

def test_non_json_output_raises():
    with pytest.raises(VisionExtractionError):
        extract_from_image(
            _png_bytes(),
            tenant_id=_TENANT,
            target_fields=["x"],
            acquired_via="paper_book",
            media_type="image/png",
            call=_FakeCall("sorry, I can't read that"),
            consent_check=_ALLOW,
        )


def test_missing_fields_key_raises():
    with pytest.raises(VisionExtractionError):
        extract_from_image(
            _png_bytes(),
            tenant_id=_TENANT,
            target_fields=["x"],
            acquired_via="paper_book",
            media_type="image/png",
            call=_FakeCall(json.dumps({"result": "nope"})),
            consent_check=_ALLOW,
        )


def test_json_fence_is_tolerated():
    payload = "```json\n" + json.dumps(
        {"fields": [{"name": "x", "value": "v", "confidence": 0.9}]}
    ) + "\n```"
    out = extract_from_image(
        _png_bytes(),
        tenant_id=_TENANT,
        target_fields=["x"],
        acquired_via="paper_book",
        media_type="image/png",
        call=_FakeCall(payload),
        consent_check=_ALLOW,
    )
    assert out.fields[0].value == "v"


# --- thresholds single-sourced from field_mapping -----------------------------

@pytest.mark.parametrize(
    "conf,expected",
    [
        (0.60, "ask_owner"),
        (0.70, "commit_with_notification"),
        (0.84, "commit_with_notification"),
        (0.85, "commit_silently"),
        (0.99, "commit_silently"),
    ],
)
def test_route_field_uses_shared_thresholds(conf, expected):
    assert route_field(ExtractedField(name="x", value="v", confidence=conf)) == expected


# --- model resolution (VT-732: the env tier, not a VIABE_ENV yaml slot) -------

def test_resolve_model_follows_the_specialist_tier_var(monkeypatch):
    """The reported model id comes from TEAM_MODEL_SPECIALIST — the SAME switch that decides which
    model actually runs. The old config/models.yaml VIABE_ENV slot no longer participates, so a
    deployment can no longer 'change the model' in one place while the other keeps picking."""
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "gpt-5.6-luna")
    assert _resolve_vision_model() == "gpt-5.6-luna"
    monkeypatch.setenv("TEAM_MODEL_SPECIALIST", "claude-sonnet-5")
    assert _resolve_vision_model() == "claude-sonnet-5"


def test_resolve_model_ignores_viabe_env(monkeypatch):
    monkeypatch.delenv("TEAM_MODEL_SPECIALIST", raising=False)
    monkeypatch.setenv("VIABE_ENV", "production")
    assert _resolve_vision_model() == "claude-sonnet-5"  # the tier default, not a yaml prod slot


# --- preprocessing ------------------------------------------------------------

def test_corrupt_image_raises_preprocess_error():
    with pytest.raises(ImagePreprocessError):
        _preprocess_image(b"not an image at all", "image/jpeg")


def test_small_png_passthrough_keeps_media_type():
    raw = _png_bytes(40, 40)
    out_bytes, mt = _preprocess_image(raw, "image/png")
    assert mt == "image/png"
    assert out_bytes == raw


def test_oversized_image_downscaled_and_reencoded():
    raw = _png_bytes(4000, 3000)  # long edge 4000 > 1568
    out_bytes, mt = _preprocess_image(raw, "image/png")
    assert mt == "image/jpeg"
    from PIL import Image

    img = Image.open(io.BytesIO(out_bytes))
    assert max(img.size) <= 1568


# --- VT-55 multi-entry extraction ---------------------------------------------

def test_extract_entries_returns_one_result_per_entry():
    from orchestrator.integrations.vision_extraction import extract_entries_from_image

    payload = json.dumps({"entries": [
        {"fields": [{"name": "customer_name", "value": "Asha", "confidence": 0.9},
                    {"name": "phone", "value": "9000000001", "confidence": 0.92}]},
        {"fields": [{"name": "customer_name", "value": "Ravi", "confidence": 0.88}]},
    ]})
    out = extract_entries_from_image(
        _png_bytes(), tenant_id=_TENANT, target_fields=["customer_name", "phone"],
        acquired_via="paper_book", media_type="image/png",
        call=_FakeCall(payload), consent_check=_ALLOW,
    )
    assert len(out) == 2
    assert out[0].fields[0].value == "Asha" and out[1].fields[0].value == "Ravi"


def test_extract_entries_consent_fail_closed():
    from orchestrator.integrations.vision_extraction import extract_entries_from_image

    with pytest.raises(ConsentRejectedError):
        extract_entries_from_image(
            _png_bytes(), tenant_id=_TENANT, target_fields=["customer_name"],
            acquired_via="paper_book", media_type="image/png",
            call=_exploding_call, consent_check=_DENY,
        )


def test_extract_entries_missing_entries_key_raises():
    from orchestrator.integrations.vision_extraction import extract_entries_from_image

    with pytest.raises(VisionExtractionError):
        extract_entries_from_image(
            _png_bytes(), tenant_id=_TENANT, target_fields=["x"],
            acquired_via="paper_book", media_type="image/png",
            call=_FakeCall(json.dumps({"fields": []})), consent_check=_ALLOW,
        )
