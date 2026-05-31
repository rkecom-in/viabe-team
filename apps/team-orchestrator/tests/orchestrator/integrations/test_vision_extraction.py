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


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.content = [SimpleNamespace(type="text", text=text)]


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        return _FakeResp(self._text)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


class _ExplodingClient:
    """Any transmission attempt fails the test (proves fail-closed)."""

    class _M:
        def create(self, **kw):  # noqa: ARG002
            raise AssertionError("transmitted to Anthropic despite no consent")

    def __init__(self) -> None:
        self.messages = _ExplodingClient._M()


def _png_bytes(w: int = 32, h: int = 32, color=(200, 180, 160)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


# --- consent gate (fail-closed) ------------------------------------------------

def test_consent_absent_fails_closed_no_transmission():
    client = _ExplodingClient()
    with pytest.raises(ConsentRejectedError):
        extract_from_image(
            _png_bytes(),
            tenant_id=_TENANT,
            target_fields=["customer_name"],
            acquired_via="paper_book",
            media_type="image/png",
            client=client,  # must NOT be called
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
    client = _FakeClient(payload)
    out = extract_from_image(
        _png_bytes(),
        tenant_id=_TENANT,
        target_fields=["customer_name", "phone"],
        acquired_via="paper_book",
        media_type="image/png",
        client=client,
        consent_check=_ALLOW,
    )
    assert out.acquired_via == "paper_book"
    assert [f.name for f in out.fields] == ["customer_name", "phone"]
    assert out.fields[0].confidence == 0.92
    assert out.fields[1].value == "9000000001"
    # transmitted exactly once, with an image block + a text block.
    assert len(client.messages.calls) == 1
    content = client.messages.calls[0]["messages"][0]["content"]
    assert any(b["type"] == "image" for b in content)


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
        client=_FakeClient(payload),
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
            client=_FakeClient("sorry, I can't read that"),
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
            client=_FakeClient(json.dumps({"result": "nope"})),
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
        client=_FakeClient(payload),
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


# --- model split --------------------------------------------------------------

def test_resolve_model_production_is_sonnet(monkeypatch):
    monkeypatch.setenv("VIABE_ENV", "production")
    assert _resolve_vision_model() == "claude-sonnet-4-6"


def test_resolve_model_default_is_haiku(monkeypatch):
    monkeypatch.delenv("VIABE_ENV", raising=False)
    assert _resolve_vision_model() == "claude-haiku-4-5"


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
