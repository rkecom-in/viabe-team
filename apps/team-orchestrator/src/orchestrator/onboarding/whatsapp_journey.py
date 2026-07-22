"""VT-692 — WhatsApp-journey discovery kick + canonical promotion + completion belt.

The Fazal ruling (2026-07-22, first-customer screenshot): when the WhatsApp journey collects
the business NAME, the manager should do what the web onboarding does — kick auto-discovery
(LLM+WebSearch candidates leg included) and fetch the business details, GST included. And the
live journey exposed two gaps this module closes alongside:

1. **Discovery kick** (`maybe_kick_discovery`): the web path anchors `auto_discovery_workflow`
   from the signup form at create time; the WhatsApp path had no anchor, so discovery never
   ran and the journey stayed draft-less forever. Now: the moment `business_name` lands in a
   WhatsApp-created tenant's journey answers, we build the SAME seed shape `run_signup` uses
   (name + city + free-text type + whatsapp_number; gstin ONLY if the flag-gated
   `entity_match.fetch_candidates` LLM+WebSearch leg surfaces exactly one candidate — a HINT
   for `discover_gst`'s Sandbox lookup, never an asserted fact) and start the workflow under
   the idempotent id ``wa_discovery_{tenant_id}`` (double-kicks are DBOS no-ops). Everything
   downstream is the EXISTING machinery: sources → business_profile_draft → confirm questions
   → the authoritative GST verify gate, all unchanged.

2. **Canonical promotion** (`promote_answers_to_tenant`): a WhatsApp tenant is created with
   ``business_name=''`` / type+city NULL (honest empties); the journey recorded answers but
   nothing ever promoted them to the `tenants` row (the web path writes them at INSERT — no
   post-create promoter existed anywhere). Fill-empty-only semantics: never clobber a
   non-empty value, so the web path is untouchable by construction. business_type promotes
   ONLY through `reconcile_business_type` onto the fixed taxonomy — an off-taxonomy free
   answer ("Business Intelligence Services") stays recorded but is never asserted (CL-390
   never-assert). owner_name promotes to the business_profile via the same `confirm_draft`
   choke every other seam uses.

3. **Completion belt** (`should_force_complete`): with no draft EVER coming (discovery absent
   or terminally done) the old hold branch looped the "give us a moment" opener forever on a
   finished queue. The belt says: a WhatsApp tenant whose core answers are all captured and
   whose discovery is not in flight should COMPLETE with the honest recap instead of holding.
   While discovery IS in flight, the hold message is honest ("setting up your assistant") and
   the next turn surfaces the draft confirms — so the belt only fires when waiting is a lie.

Everything here is best-effort / fail-soft: journey progress must never stall on this module.
CL-390: no owner text logged beyond field NAMES.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: The journey answer fields whose arrival triggers a kick/promotion attempt.
CORE_FIELDS = ("business_name", "owner_name", "business_type", "city")

_DISCOVERY_WF_PREFIX = "wa_discovery_"


def _tenant_row(tenant_id: UUID | str) -> dict[str, Any] | None:
    from orchestrator.db.tenant_connection import tenant_connection

    with tenant_connection(tenant_id) as conn:
        row = conn.execute(
            "SELECT created_via, business_name, business_type, city_tier, whatsapp_number "
            "FROM tenants WHERE id = %s",
            (str(tenant_id),),
        ).fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {
        "created_via": row[0], "business_name": row[1], "business_type": row[2],
        "city_tier": row[3], "whatsapp_number": row[4],
    }


def _is_whatsapp_tenant(row: dict[str, Any] | None) -> bool:
    return bool(row) and row.get("created_via") == "whatsapp"


def _gstin_candidate(business_name: str, city: str | None) -> str | None:
    """The flag-gated LLM+WebSearch candidates leg (entity_match.fetch_candidates — the Fazal
    'LLM+Websearch' ruling). Returns a gstin HINT only when the leg yields exactly ONE distinct
    GSTIN (ambiguity → no hint; `discover_gst` simply self-skips without one). Fail-soft None."""
    try:
        from orchestrator.feature_flags import llm_discovery_enabled

        if not llm_discovery_enabled():
            return None
        from orchestrator.onboarding.entity_match import fetch_candidates

        candidates = fetch_candidates(business_name, city or "")
        gstins = {
            (
                getattr(c, "candidate_gstin", None)
                or (c.get("candidate_gstin") if isinstance(c, dict) else None)
            )
            for c in (candidates or [])
        }
        gstins.discard(None)
        gstins.discard("")
        if len(gstins) == 1:
            return next(iter(gstins))
        return None
    except Exception:  # noqa: BLE001 — a hint source can never block the kick
        logger.warning("whatsapp_journey: gstin-candidate leg failed (fail-soft)", exc_info=True)
        return None


def maybe_kick_discovery(tenant_id: UUID | str, answers: dict[str, Any]) -> bool:
    """Kick auto-discovery ONCE for a WhatsApp-created tenant whose journey answers now carry
    ``business_name``. Idempotent via the ``wa_discovery_{tenant_id}`` workflow id (a second
    call is a DBOS no-op). Returns True iff a start was attempted this call. Fail-soft."""
    try:
        name = str(answers.get("business_name") or "").strip()
        if not name:
            return False
        row = _tenant_row(tenant_id)
        if not _is_whatsapp_tenant(row):
            return False

        from dbos import DBOS, SetWorkflowID

        wf_id = f"{_DISCOVERY_WF_PREFIX}{tenant_id}"
        if DBOS.get_workflow_status(wf_id) is not None:
            return False  # already kicked (any state) — never double-start

        city = str(answers.get("city") or "").strip() or None
        gstin_hint = _gstin_candidate(name, city)
        if gstin_hint:
            # VT-693 — persist the HINT into the draft (provenance llm_hint, never asserted):
            # the identity CARD shows its tail and the owner's YES routes it into the formal
            # Sandbox verify (entity_match.confirm_and_verify).
            try:
                from orchestrator.onboarding.draft_profile import write_draft

                write_draft(tenant_id, {"gstin_candidate": gstin_hint}, source="llm_hint")
            except Exception:  # noqa: BLE001 — hint bookkeeping never blocks the kick
                logger.warning("whatsapp_journey: gstin-hint draft write failed tenant=%s", tenant_id)
        seed: dict[str, Any] = {
            "business_name": name,
            "gstin": gstin_hint,
            "business_type": None,  # free text is NOT taxonomy — reconcile owns the mapping
            "city": city,
            "whatsapp_number": (row or {}).get("whatsapp_number"),
        }
        from orchestrator.onboarding.auto_discovery import auto_discovery_workflow

        with SetWorkflowID(wf_id):
            DBOS.start_workflow(auto_discovery_workflow, str(tenant_id), seed)
        logger.info(
            "whatsapp_journey: discovery kicked tenant=%s (gstin_hint=%s)",
            tenant_id, bool(seed["gstin"]),
        )
        return True
    except Exception:  # noqa: BLE001 — the journey must never stall on a kick failure
        logger.warning("whatsapp_journey: discovery kick failed (fail-soft) tenant=%s", tenant_id)
        return False


def promote_answers_to_tenant(tenant_id: UUID | str, answers: dict[str, Any]) -> None:
    """Promote captured core answers to canonical, FILL-EMPTY-ONLY (a non-empty tenants value is
    never overwritten, so web-created tenants are structurally untouchable here):

    - ``business_name`` → tenants.business_name (only when '')
    - ``city``          → tenants.city_tier via coarsen_city (only when NULL; raw city discarded)
    - ``business_type`` → tenants.business_type ONLY via reconcile_business_type onto the fixed
      taxonomy (only when NULL; off-taxonomy stays unasserted — never bent)
    - ``owner_name``    → business_profile via the same confirm_draft choke the web path uses

    Idempotent + fail-soft (each leg independent)."""
    row = _tenant_row(tenant_id)
    if not _is_whatsapp_tenant(row):
        return
    from orchestrator.db.tenant_connection import tenant_connection

    name = str(answers.get("business_name") or "").strip()
    if name and not (row or {}).get("business_name"):
        try:
            with tenant_connection(tenant_id) as conn:
                conn.execute(
                    "UPDATE tenants SET business_name = %s "
                    "WHERE id = %s AND business_name = ''",
                    (name[:200], str(tenant_id)),
                )
        except Exception:  # noqa: BLE001
            logger.warning("whatsapp_journey: business_name promote failed tenant=%s", tenant_id)

    city = str(answers.get("city") or "").strip()
    if city and not (row or {}).get("city_tier"):
        try:
            from orchestrator.privacy.coarsening import coarsen_city

            tier = str(coarsen_city(city))
            with tenant_connection(tenant_id) as conn:
                conn.execute(
                    "UPDATE tenants SET city_tier = %s WHERE id = %s AND city_tier IS NULL",
                    (tier, str(tenant_id)),
                )
        except Exception:  # noqa: BLE001
            logger.warning("whatsapp_journey: city_tier promote failed tenant=%s", tenant_id)

    free_type = str(answers.get("business_type") or "").strip()
    if free_type and not (row or {}).get("business_type"):
        try:
            from orchestrator.onboarding.business_type_reconcile import (
                is_valid_business_type,
                reconcile_business_type,
            )

            reconciled = reconcile_business_type(
                business_name=name or None, gbp_category=free_type
            )
            bt = getattr(reconciled, "business_type", None)
            if bt and is_valid_business_type(bt):
                with tenant_connection(tenant_id) as conn:
                    conn.execute(
                        "UPDATE tenants SET business_type = %s "
                        "WHERE id = %s AND business_type IS NULL",
                        (bt, str(tenant_id)),
                    )
        except Exception:  # noqa: BLE001 — off-taxonomy/reconcile failure = stay unasserted
            logger.warning("whatsapp_journey: business_type reconcile failed tenant=%s", tenant_id)

    owner = str(answers.get("owner_name") or "").strip()
    if owner:
        try:
            from orchestrator.onboarding.draft_profile import confirm_draft

            confirm_draft(tenant_id, {"owner_name": owner[:120]})
        except Exception:  # noqa: BLE001
            logger.warning("whatsapp_journey: owner_name promote failed tenant=%s", tenant_id)


def on_answers_advanced(tenant_id: UUID | str, answers: dict[str, Any]) -> None:
    """The single post-answer hook (called from journey._advance AND the specialist write path):
    kick discovery when the name is in, promote whatever core answers are new. Fail-soft, cheap
    no-op for non-WhatsApp tenants and for answer sets without core fields."""
    try:
        if not any(answers.get(f) for f in CORE_FIELDS):
            return
        maybe_kick_discovery(tenant_id, answers)
        promote_answers_to_tenant(tenant_id, answers)
    except Exception:  # noqa: BLE001 — never stall the journey write path
        logger.warning("whatsapp_journey: on_answers_advanced failed (fail-soft) tenant=%s", tenant_id)


def should_force_complete(tenant_id: UUID | str, answers: dict[str, Any] | None) -> bool:
    """The completion belt: True iff this is a WhatsApp-created tenant whose CORE answers are all
    captured AND whose discovery is NOT pending (absent or terminal) AND the draft is still empty
    — i.e. holding for a draft would be a lie, so the journey should complete with the honest
    recap instead of looping the opener. While discovery is ENQUEUED/PENDING, False (the hold
    message is honest — the next turn surfaces the draft confirms)."""
    try:
        a = answers or {}
        if not all(str(a.get(f) or "").strip() for f in CORE_FIELDS):
            return False
        row = _tenant_row(tenant_id)
        if not _is_whatsapp_tenant(row):
            return False
        from orchestrator.onboarding.draft_profile import get_draft

        if (get_draft(tenant_id) or {}).get("attributes"):
            return False  # a draft exists — the normal recompose path owns it
        from dbos import DBOS

        status = DBOS.get_workflow_status(f"{_DISCOVERY_WF_PREFIX}{tenant_id}")
        state = getattr(status, "status", None) if status is not None else None
        if state in ("PENDING", "ENQUEUED", "DELAYED"):
            return False  # discovery genuinely in flight — holding is honest
        return True
    except Exception:  # noqa: BLE001 — belt failure = keep today's behavior (hold)
        logger.warning("whatsapp_journey: force-complete check failed (fail-soft) tenant=%s", tenant_id)
        return False


# VT-693 — the GST identity card. The discovered registration payload is SHOWN and confirmed,
# never silently used and never auto-asserted (the GSTIN came from a web hint — could be the
# wrong company; the owner's explicit YES is what anchors it and routes the GSTIN into the
# formal Sandbox verify).
_GST_PAYLOAD_FIELDS = (
    "legal_name", "trade_name", "constitution", "principal_address",
    "registration_date", "nature_of_business", "additional_addresses",
)


def _gst_display(draft_attrs: dict[str, Any]) -> dict[str, str]:
    name = str(draft_attrs.get("trade_name") or draft_attrs.get("legal_name") or "").strip()
    addr = str(draft_attrs.get("principal_address") or "").strip()
    nature = draft_attrs.get("nature_of_business")
    if isinstance(nature, list):
        nature = ", ".join(str(x) for x in nature[:3])
    nature = str(nature or "").strip()
    gstin = str(draft_attrs.get("gstin_candidate") or "").strip()
    return {"name": name, "addr": addr[:120], "nature": nature[:120],
            "gstin_tail": gstin[-4:] if gstin else ""}


def gst_identity_pending(tenant_id: UUID | str, answers: dict[str, Any] | None) -> bool:
    """True iff the draft carries a discovered GST identity the owner has not yet
    confirmed/declined ('gst_identity' absent from answers)."""
    try:
        if "gst_identity" in (answers or {}):
            return False
        from orchestrator.onboarding.draft_profile import get_draft

        attrs = (get_draft(tenant_id) or {}).get("attributes") or {}
        d = _gst_display(attrs)
        return bool(d["name"] and (d["addr"] or d["nature"]))
    except Exception:  # noqa: BLE001
        return False


def gst_identity_card_question(tenant_id: UUID | str) -> dict[str, Any] | None:
    """The card as a journey CONFIRM question (field 'gst_identity') — the VT-479 machinery
    presents confirms as Yes/No/Skip buttons natively. None when nothing to show."""
    try:
        from orchestrator.onboarding.draft_profile import get_draft

        attrs = (get_draft(tenant_id) or {}).get("attributes") or {}
        d = _gst_display(attrs)
        if not d["name"]:
            return None
        bits_en = [f"Here's what I found online: {d['name']}"]
        bits_hi = [f"ऑनलाइन यह मिला: {d['name']}"]
        if d["addr"]:
            bits_en.append(f"registered at {d['addr']}")
            bits_hi.append(f"पता: {d['addr']}")
        if d["nature"]:
            bits_en.append(f"nature of business: {d['nature']}")
            bits_hi.append(f"कारोबार: {d['nature']}")
        if d["gstin_tail"]:
            bits_en.append(f"GSTIN ending …{d['gstin_tail']}")
            bits_hi.append(f"GSTIN अंत …{d['gstin_tail']}")
        return {
            "field": "gst_identity",
            "kind": "confirm",
            "draft_value": "yes",
            "prompt_en": "; ".join(bits_en) + ". Is this your business?",
            "prompt_hi": "; ".join(bits_hi) + "। क्या यही आपका बिज़नेस है?",
        }
    except Exception:  # noqa: BLE001
        return None


def with_gst_card(
    tenant_id: UUID | str, queue: list[dict[str, Any]], answers: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Prepend the pending GST identity card to a (re)composed queue — identity confirm comes
    BEFORE any residual question. No-op when nothing pending or the card is already queued."""
    try:
        if not gst_identity_pending(tenant_id, answers):
            return queue
        if any(q.get("field") == "gst_identity" for q in queue):
            return queue
        card = gst_identity_card_question(tenant_id)
        return ([card] + list(queue)) if card else queue
    except Exception:  # noqa: BLE001
        return queue


def accept_gst_identity(tenant_id: UUID | str) -> None:
    """Owner said YES to the card: anchor the draft (entity_resolution accept → the populate
    bar), promote derivable facts, and route the candidate GSTIN into the FORMAL Sandbox verify
    (entity_match.confirm_and_verify — the sole authority for verification stamps)."""
    from orchestrator.onboarding.draft_profile import get_draft, write_draft

    attrs = (get_draft(tenant_id) or {}).get("attributes") or {}
    write_draft(
        tenant_id,
        {"entity_resolution": {"decision": "accept", "source": "owner_gst_confirm"}},
        source="owner_confirm",
    )
    try:
        from orchestrator.onboarding.journey import populate_profile_from_draft

        populate_profile_from_draft(tenant_id)
    except Exception:  # noqa: BLE001
        logger.warning("whatsapp_journey: post-accept populate failed tenant=%s", tenant_id)
    gstin = str(attrs.get("gstin_candidate") or "").strip()
    if not gstin:
        # Self-heal (live first-customer gap): a draft whose discovery predated the hint-persist
        # (or whose hint was lost) re-runs the flag-gated candidates leg NOW, anchored on the
        # owner-CONFIRMED identity — still only a single-candidate HINT into the formal verify.
        name = str(attrs.get("legal_name") or attrs.get("trade_name") or "").strip()
        addr = str(attrs.get("principal_address") or "")
        city = addr.split(",")[-2].strip() if addr.count(",") >= 2 else None
        if name:
            gstin = _gstin_candidate(name, city) or ""
            if gstin:
                try:
                    from orchestrator.onboarding.draft_profile import write_draft

                    write_draft(tenant_id, {"gstin_candidate": gstin}, source="llm_hint")
                except Exception:  # noqa: BLE001
                    pass
    if gstin:
        try:
            from orchestrator.onboarding.entity_match import confirm_and_verify

            name_anchor = str(attrs.get("legal_name") or attrs.get("trade_name") or "") or None
            confirm_and_verify(tenant_id, gstin, name_anchor=name_anchor)
        except Exception:  # noqa: BLE001 — verification is best-effort here; VT-408 owns the hard gate
            logger.warning("whatsapp_journey: confirm_and_verify failed tenant=%s", tenant_id)


def decline_gst_identity(tenant_id: UUID | str) -> None:
    """Owner said NO: wrong company — every discovered GST field AND the candidate GSTIN are
    removed from the draft (nothing survives, even as a hint)."""
    from orchestrator.onboarding.draft_profile import remove_draft_fields

    remove_draft_fields(
        tenant_id, [*(_GST_PAYLOAD_FIELDS), "gstin_candidate", "entity_resolution"]
    )


def push_next_question_after_discovery(tenant_id: UUID | str) -> bool:
    """VT-692 addendum (the Fazal 'nothing came after the hold message' flag) — close the
    copy-promise: when discovery COMPLETES for a WhatsApp tenant with an active journey, the
    system must speak NEXT, not wait to be spoken to. This enqueues the follow-through into the
    VT-683 owner-comms queue — the post-turn drain / */10 sweep then delivers it in-session at
    idle pace (session-open gated by the drainer, so nothing ever pushes outside the window):

      - draft yields pending questions → recompose + install the queue, enqueue the HEAD
        question's bilingual prompt;
      - nothing pending AND the profile is deterministically complete → complete the journey
        and enqueue the honest recap;
      - nothing pending, not complete → enqueue nothing (never an empty promise).

    Dedup: at most one queued journey-push item per tenant (payload marker). Fail-soft
    everywhere — discovery's own result recording must never be disturbed. Returns True iff an
    item was enqueued."""
    try:
        row = _tenant_row(tenant_id)
        if not _is_whatsapp_tenant(row):
            return False
        from orchestrator.onboarding import journey as j

        g = j.get_journey(tenant_id)
        if g is None or g.get("status") != "active":
            return False

        # Dedup — one pending push at a time (queued-only; a delivered one may be followed).
        from orchestrator.db.tenant_connection import tenant_connection

        with tenant_connection(tenant_id) as conn:
            pending = conn.execute(
                "SELECT 1 FROM owner_comms_queue WHERE tenant_id = %s AND status = 'queued' "
                "AND payload->>'journey_push' = 'true' LIMIT 1",
                (str(tenant_id),),
            ).fetchone()
        if pending:
            return False

        answers = dict(g.get("answers") or {})
        skipped = list(g.get("skipped") or [])
        _, business_type = j._tenant_phase_and_type(tenant_id)
        j.populate_profile_from_draft(tenant_id)
        queue = with_gst_card(tenant_id, j._compose_queue(tenant_id, business_type), answers)

        from orchestrator.owner_surface import owner_comms_queue as comms_q

        if queue:
            j._install_recomposed_queue(tenant_id, queue, None)
            head = queue[0]
            payload = {
                "journey_push": "true",
                "text_en": head.get("prompt_en", ""),
                "text_hi": head.get("prompt_hi", ""),
            }
            if head.get("field") == "gst_identity":
                # VT-693 — the identity card delivers as tappable Yes/No buttons (the VT-479
                # in-session object; the drainer falls back to freeform on any failure).
                payload["interactive_template"] = "onboarding_confirm_yesno"
        elif j._journey_profile_complete(tenant_id, business_type, answers, skipped):
            j._complete(tenant_id)
            done = j._completion_message(answers)
            payload = {
                "journey_push": "true",
                "text_en": done.get("reply_en", ""),
                "text_hi": done.get("reply_hi", ""),
            }
        else:
            return False
        if not payload["text_en"]:
            return False
        comms_q.enqueue(tenant_id, kind="notice", payload=payload, priority=60)
        logger.info("whatsapp_journey: post-discovery follow-through queued tenant=%s", tenant_id)
        return True
    except Exception:  # noqa: BLE001 — never disturb the discovery run
        logger.warning(
            "whatsapp_journey: post-discovery push failed (fail-soft) tenant=%s", tenant_id
        )
        return False


__all__ = [
    "CORE_FIELDS",
    "maybe_kick_discovery",
    "on_answers_advanced",
    "promote_answers_to_tenant",
    "push_next_question_after_discovery",
    "should_force_complete",
]
