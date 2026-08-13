"""VT-740 — the two defects adversarial review caught that the lane's own tests could not see.

Both were invisible to the original suite for structural reasons worth keeping in mind: it
monkeypatched `dispatch_alert` wholesale (so the dedup path never executed) and seeded exactly one
crashed campaign per tenant (so a per-tenant collision could not occur). Passing tests are not
evidence when the test shape excludes the failure.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from orchestrator import orphan_reaper  # noqa: E402
from orchestrator.agent import approval_resume  # noqa: E402


class TestAlertsAggregatePerTenant:
    """DEFECT 1 (blocking): `alerts.dispatch._dedup_key` is `tenant_id:trigger_kind` on a 5-minute
    window and is NOT campaign-scoped, while this sweep terminalizes up to 200 campaigns per tick.

    A per-campaign alert loop therefore fired alert #1 and had every other one deduped away — and
    because the terminal status IS the idempotency key (the CAS is on `status='approved'`), those
    campaigns were never candidates again. Un-alerted AND no longer findable by the
    "approved with no ledger progress" shape that used to surface them: a regression in
    recoverability, guaranteed to fire on the sweep's first production run against the backlog.
    """

    def test_two_crashed_campaigns_for_one_tenant_produce_ONE_alert_naming_BOTH(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fired: list = []
        monkeypatch.setattr(
            "orchestrator.alerts.dispatch.dispatch_alert", lambda t: fired.append(t)
        )
        tenant = "11111111-1111-1111-1111-111111111111"
        rows = [
            {"tenant_id": tenant, "campaign_id": "aaaaaaaa-0000-0000-0000-000000000001",
             "intended": 10, "delivered": 4, "attempted": 0},
            {"tenant_id": tenant, "campaign_id": "bbbbbbbb-0000-0000-0000-000000000002",
             "intended": 6, "delivered": 0, "attempted": 0},
        ]
        orphan_reaper._alert_crashed_campaigns(rows)

        assert len(fired) == 1, (
            "one alert per TENANT, not per campaign — the dedup key is tenant:kind, so a "
            f"per-campaign loop loses all but the first (got {len(fired)})"
        )
        text = fired[0].message_text
        for row in rows:
            assert row["campaign_id"] in text, (
                "every terminalized campaign id must appear in the single alert; an id that "
                "appears nowhere is a half-messaged cohort nobody can find again"
            )
        assert fired[0].payload["campaign_count"] == 2
        assert fired[0].payload["total_remainder"] == 12  # (10-4) + (6-0)

    def test_the_worst_case_in_the_group_decides_the_severity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One campaign that reached real people must not be downgraded by being grouped with
        campaigns that reached nobody."""
        fired: list = []
        monkeypatch.setattr(
            "orchestrator.alerts.dispatch.dispatch_alert", lambda t: fired.append(t)
        )
        tenant = "22222222-2222-2222-2222-222222222222"
        orphan_reaper._alert_crashed_campaigns([
            {"tenant_id": tenant, "campaign_id": "cccccccc-0000-0000-0000-000000000003",
             "intended": 5, "delivered": 0, "attempted": 0},
            {"tenant_id": tenant, "campaign_id": "dddddddd-0000-0000-0000-000000000004",
             "intended": 9, "delivered": 3, "attempted": 0},
        ])
        assert fired[0].trigger_kind == "escalation", (
            "a group containing a campaign that already messaged real customers is an escalation, "
            "not a silent_terminal"
        )
        assert fired[0].payload["campaigns_that_reached_customers"] == 1

    def test_separate_tenants_still_get_separate_alerts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Aggregation is per TENANT — it must not collapse two tenants into one alert, which
        would send one tenant's campaign ids to the other's context."""
        fired: list = []
        monkeypatch.setattr(
            "orchestrator.alerts.dispatch.dispatch_alert", lambda t: fired.append(t)
        )
        orphan_reaper._alert_crashed_campaigns([
            {"tenant_id": "33333333-3333-3333-3333-333333333333",
             "campaign_id": "eeeeeeee-0000-0000-0000-000000000005",
             "intended": 4, "delivered": 1, "attempted": 0},
            {"tenant_id": "44444444-4444-4444-4444-444444444444",
             "campaign_id": "ffffffff-0000-0000-0000-000000000006",
             "intended": 4, "delivered": 1, "attempted": 0},
        ])
        assert len(fired) == 2
        assert {str(t.tenant_id) for t in fired} == {
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        }

    def test_one_tenant_alert_failure_does_not_stop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list = []

        def _flaky(trigger):  # noqa: ANN001, ANN202
            if str(trigger.tenant_id).startswith("55555555"):
                raise OSError("alert transport down")
            seen.append(trigger)

        monkeypatch.setattr("orchestrator.alerts.dispatch.dispatch_alert", _flaky)
        orphan_reaper._alert_crashed_campaigns([
            {"tenant_id": "55555555-5555-5555-5555-555555555555",
             "campaign_id": "11111111-0000-0000-0000-000000000007",
             "intended": 3, "delivered": 1, "attempted": 0},
            {"tenant_id": "66666666-6666-6666-6666-666666666666",
             "campaign_id": "22222222-0000-0000-0000-000000000008",
             "intended": 3, "delivered": 1, "attempted": 0},
        ])
        assert len(seen) == 1, "a failing tenant alert must not swallow the next tenant's"


class TestTheTwoHalvesDoNotCancel:
    """DEFECT 2 (high): the redrive effect-check counted ONLY `approved` campaigns — which is
    exactly the status the crashed-campaign sweep in the same change flips away at 2h.

    The redrive path is reached only when the bound task is `blocked` or `dead_letter`: >=1h to the
    first blocked rung, ~6h to dead_letter per this module's own VT-668 note. So in the canonical
    case — the owner replies hours after the crash, the entire reason the seam exists — the
    campaign was already terminalized, the live set was empty, and the alert could never fire. Two
    changes shipped together, each correct alone, cancelling each other in production.
    """

    def test_terminalized_campaigns_are_still_surfaced_on_redrive(self) -> None:
        assert "failed" in approval_resume._LIVE_CAMPAIGN_STATUSES, (
            "the crashed-campaign sweep terminalizes 'approved' -> 'failed' at 2h, while the "
            "redrive path needs >=1h and typically ~6h; excluding 'failed' makes this check dead "
            "in exactly the case it was written for"
        )
        assert "approved" in approval_resume._LIVE_CAMPAIGN_STATUSES, (
            "the in-flight case must still surface"
        )

    def test_deliberate_human_decisions_are_not_surfaced(self) -> None:
        """'cancelled'/'rejected' are decisions, not crashes; 'sent' has no remainder to warn
        about. Admitting them would make the alert noise nobody reads."""
        for settled in ("cancelled", "rejected", "sent", "proposed"):
            assert settled not in approval_resume._LIVE_CAMPAIGN_STATUSES, settled
