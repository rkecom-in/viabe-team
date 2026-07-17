"""VT-676 (CD2) — customer-list export unit tests.

Proves the PII-egress rails without any live DB/storage/Twilio:
  - CSV shape (portal-parity columns, verbatim paise→INR, paged read to exhaustion);
  - recipient is SERVER-DERIVED only (tenants.owner_phone → whatsapp_number), never an argument;
  - the whole delivery FAILS SOFT → False (caller rides the honest VT-642 ack) on: empty list /
    no verified owner phone / signed-URL failure / transport error;
  - the signed URL travels ONLY as media_urls (never in the body, never logged);
  - the tm_audit egress event records rows + path + sid — never content, never the URL;
  - the triage-seam ride-along prefers the real delivery and falls back to LIST_SEND_ACK_PREAMBLE.

Dep discipline: customer_export itself is import-light, but the seam test pulls the manager stack →
importorskip("anthropic") mirrors test_triage_seam.py.
"""

from __future__ import annotations

import csv
import io
from typing import Any
from uuid import uuid4

import pytest

# The tests wire fakes onto orchestrator.db.wrappers (psycopg) and drive the twilio_send funnel —
# both absent in the dep-less smoke; the full suite runs everything (VT-337 discipline).
pytest.importorskip("psycopg")
pytest.importorskip("twilio")

from orchestrator.owner_surface import customer_export as ce  # noqa: E402


class _FakeStorage:
    """report_storage._StorageClient-shaped fake — records uploads, mints a fixed signed URL."""

    def __init__(self, *, url: str | None = "https://signed.example/abc?token=x") -> None:
        self.uploads: list[tuple[str, bytes, dict[str, Any]]] = []
        self._url = url

    def upload(self, path: str, file: bytes, file_options: dict[str, Any]) -> Any:
        self.uploads.append((path, file, file_options))
        return {"path": path}

    def create_signed_url(self, path: str, expires_in: int) -> Any:
        if self._url is None:
            raise RuntimeError("storage down")
        return {"signedURL": self._url}


def _customers_rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": str(uuid4()),
            "display_name": f"Cust {i}",
            "phone_e164": f"+91900000{i:04d}",
            "opt_out_status": "subscribed",
            "spend_paise": 12345,
        }
        for i in range(n)
    ]


def _patch_customers(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    import orchestrator.db.wrappers as wrappers_mod

    class _FakeCustomers:
        def list_customers_page(self, tenant_id: Any, *, limit: int, offset: int, **kw: Any):
            return rows[offset : offset + limit]

    monkeypatch.setattr(wrappers_mod, "CustomersWrapper", _FakeCustomers)


# --- CSV builder --------------------------------------------------------------------------------


def test_csv_shape_and_verbatim_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_customers(monkeypatch, _customers_rows(3))
    data, count = ce.build_customer_list_csv(uuid4())
    assert count == 3
    parsed = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    assert parsed[0] == ["name", "phone", "status", "total_spend_inr"]
    assert parsed[1] == ["Cust 0", "+919000000000", "subscribed", "123.45"]  # 12345 paise verbatim
    assert len(parsed) == 4


def test_csv_pages_to_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """More rows than one page → the builder iterates pages until exhausted."""
    rows = _customers_rows(ce._PAGE_SIZE + 7)
    _patch_customers(monkeypatch, rows)
    _, count = ce.build_customer_list_csv(uuid4())
    assert count == ce._PAGE_SIZE + 7


# --- the full delivery (fail-soft rails) --------------------------------------------------------


def _patch_owner_phone(monkeypatch: pytest.MonkeyPatch, phone: str | None) -> None:
    monkeypatch.setattr(ce, "_resolve_owner_phone", lambda tid: phone)


def _patch_send(monkeypatch: pytest.MonkeyPatch, sends: list[dict[str, Any]], *, boom: bool = False):
    import orchestrator.utils.twilio_send as tw

    def _fake_send(body: str, recipient: str, **kwargs: Any) -> str:
        if boom:
            raise RuntimeError("twilio down")
        sends.append({"body": body, "recipient": recipient, **kwargs})
        return "SMmedia123"

    monkeypatch.setattr(tw, "send_freeform_message", _fake_send)


def _patch_audit(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> None:
    import orchestrator.observability.tm_audit as tma

    monkeypatch.setattr(tma, "emit_tm_audit", lambda **kw: events.append(kw))


def test_delivery_happy_path_all_rails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Success: CSV built → stored → signed → media send to the SERVER-derived owner → audit with
    rows/path/sid and NO url/content."""
    _patch_customers(monkeypatch, _customers_rows(2))
    _patch_owner_phone(monkeypatch, "+919321553267")
    storage = _FakeStorage()
    sends: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    _patch_send(monkeypatch, sends)
    _patch_audit(monkeypatch, events)

    tid = uuid4()
    assert ce.send_customer_list_to_owner(tid, storage_client=storage) is True

    # Stored privately, CSV content-type, upsert.
    assert len(storage.uploads) == 1
    path, blob, opts = storage.uploads[0]
    assert path.startswith(str(tid))
    assert opts["content-type"] == "text/csv"

    # Media send: URL ONLY in media_urls, never in the body; recipient = server-derived owner.
    assert len(sends) == 1
    send = sends[0]
    assert send["recipient"] == "+919321553267"
    assert send["media_urls"] == ["https://signed.example/abc?token=x"]
    assert "signed.example" not in send["body"]
    assert send["body"] == ce.CUSTOMER_LIST_CAPTION

    # Audit: rows + path + sid; NEVER the url or content.
    assert len(events) == 1
    ev = events[0]
    assert ev["event_kind"] == "customer_list_exported"
    assert ev["decision"] == {"rows": 2, "object_path": path, "message_sid": "SMmedia123"}
    assert "signed.example" not in str(ev)


@pytest.mark.parametrize(
    ("rows", "phone", "url", "send_boom"),
    [
        (0, "+919321553267", "https://x/y", False),  # empty list
        (2, None, "https://x/y", False),  # no verified owner phone
        (2, "+919321553267", None, False),  # signed-URL mint failure
        (2, "+919321553267", "https://x/y", True),  # transport failure
    ],
)
def test_delivery_fails_soft_to_false(
    monkeypatch: pytest.MonkeyPatch,
    rows: int,
    phone: str | None,
    url: str | None,
    send_boom: bool,
) -> None:
    """EVERY failure mode → False, never a raise (the caller rides the honest VT-642 ack)."""
    _patch_customers(monkeypatch, _customers_rows(rows))
    _patch_owner_phone(monkeypatch, phone)
    sends: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    _patch_send(monkeypatch, sends, boom=send_boom)
    _patch_audit(monkeypatch, events)
    storage = _FakeStorage(url=url)

    assert ce.send_customer_list_to_owner(uuid4(), storage_client=storage) is False
    assert events == []  # no egress audit without an actual egress


# --- media passthrough on the freeform funnel ---------------------------------------------------


def test_freeform_media_url_passthrough_and_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """send_freeform_message passes media_urls to the transport as media_url and NEVER logs the
    URL (mock-mode would-send included)."""
    import logging as _logging

    import orchestrator.utils.twilio_send as tw

    monkeypatch.setenv("TEAM_TWILIO_MOCK_MODE", "1")
    monkeypatch.setenv("TEAM_TWILIO_FROM_NUMBER", "+15550000000")
    secret_url = "https://signed.example/secret-token-abc"
    with caplog.at_level(_logging.DEBUG):
        sid = tw.send_freeform_message(
            "here is your file",
            "+919321553267",
            media_urls=[secret_url],
        )
    assert sid  # mocked transport returned a sid (no network)
    assert secret_url not in caplog.text  # the PII-document URL never reaches a log line
    assert "media" in caplog.text  # presence IS logged (count)


# --- the triage-seam ride-along (prefer delivery, fall back to the honest ack) ------------------


def test_seam_prefers_real_delivery_over_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    import orchestrator.manager.plan_store as plan_store_mod
    import orchestrator.manager.task_store as task_store_mod
    import orchestrator.manager.workflow as workflow_mod
    import orchestrator.onboarding.campaign_first_contact as cfc
    from orchestrator.manager import triage_seam as ts

    task_id = uuid4()
    monkeypatch.setattr(cfc, "campaign_cohort_is_empty", lambda t: False)
    monkeypatch.setattr(cfc, "mentions_customer_list_request", lambda t: True)
    monkeypatch.setattr(ts, "_recent_sent_campaign_guard", lambda *a, **k: None)
    monkeypatch.setattr(ts, "emit_tm_audit", lambda **kw: None)
    monkeypatch.setattr(plan_store_mod, "create_plan", lambda *a, **k: task_id)
    monkeypatch.setattr(
        task_store_mod, "get_task", lambda t, i: {"id": str(i), "status": "planned"}
    )
    monkeypatch.setattr(workflow_mod, "start_manager_task_workflow", lambda t, i: None)
    monkeypatch.setattr(ce, "send_customer_list_to_owner", lambda tid: True)

    out = ts._dispatch_campaign_first_contact(uuid4(), "winback + send me the list", "SMl1")
    assert out is not None
    assert out.direct_reply_text is None  # the attachment (with caption) IS the list reply


def test_seam_falls_back_to_honest_ack_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    import orchestrator.manager.plan_store as plan_store_mod
    import orchestrator.manager.task_store as task_store_mod
    import orchestrator.manager.workflow as workflow_mod
    import orchestrator.onboarding.campaign_first_contact as cfc
    from orchestrator.manager import triage_seam as ts

    task_id = uuid4()
    monkeypatch.setattr(cfc, "campaign_cohort_is_empty", lambda t: False)
    monkeypatch.setattr(cfc, "mentions_customer_list_request", lambda t: True)
    monkeypatch.setattr(ts, "_recent_sent_campaign_guard", lambda *a, **k: None)
    monkeypatch.setattr(ts, "emit_tm_audit", lambda **kw: None)
    monkeypatch.setattr(plan_store_mod, "create_plan", lambda *a, **k: task_id)
    monkeypatch.setattr(
        task_store_mod, "get_task", lambda t, i: {"id": str(i), "status": "planned"}
    )
    monkeypatch.setattr(workflow_mod, "start_manager_task_workflow", lambda t, i: None)
    monkeypatch.setattr(ce, "send_customer_list_to_owner", lambda tid: False)

    out = ts._dispatch_campaign_first_contact(uuid4(), "winback + send me the list", "SMl2")
    assert out is not None
    assert out.direct_reply_text == cfc.LIST_SEND_ACK_PREAMBLE  # VT-642 honest fallback preserved
