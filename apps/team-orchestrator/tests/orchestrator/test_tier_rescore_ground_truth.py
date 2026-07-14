"""VT-640 instrument fix — tier_rescore.py seed-aware GROUND TRUTH block (no Anthropic API call).

tier_rescore.py imports the `anthropic` SDK lazily (inside `_client()`), so its pure ground-truth
renderers are exercisable dep-less. These pin the seed-awareness that stops the blind sonnet judge
false-flagging harness-seeded facts (the reconnect_broken_sync connector-health false-positive) as
fabrication.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CANARIES = Path(__file__).resolve().parents[2] / "canaries"
sys.path.insert(0, str(_CANARIES))

import tier_rescore as tr  # noqa: E402 — after the sys.path insert


def test_seed_lapsed_renders_healthy_connector_ground_truth():
    # VT-640 — `--seed-lapsed-customers N` ALSO seeds a HEALTHY google_sheet connector + verified
    # GST/ownership (convo_harness _seed_lapsed_customers). The block must tell the judge so a grounded
    # "connected / recently-synced / not seeing a break" honest report is NOT flagged as fabrication.
    block = tr._render_ground_truth_block(
        {"setup_args": ["--onboarded", "--seed-lapsed-customers", "6"]}
    )
    assert block is not None
    assert "HEALTHY 'google_sheet' connector" in block
    assert "last synced just now" in block
    # the fabrication boundary: only a made-up ACTION the DB doesn't back is a breaker
    assert "reconnected" in block.lower() and "fixed the sync" in block.lower()
    assert "do NOT flag it as fabrication" in block
    # the seed-count line is still present (both facts render)
    assert "POOL of 6 customers" in block


def test_no_seed_lapsed_no_connector_ground_truth():
    # No --seed-lapsed-customers and no --journey → nothing factual to inject → None (unchanged).
    assert tr._render_ground_truth_block({"setup_args": ["--onboarded", "--flow", "ready_asked"]}) is None
    assert tr._render_ground_truth_block({"setup_args": []}) is None


def test_journey_draft_still_renders_without_connector_line():
    # A --journey scenario (no --seed-lapsed-customers) still gets the Chennai/type draft line, and
    # must NOT gain a spurious connector-health line (no connector was seeded).
    block = tr._render_ground_truth_block({"setup_args": ["--journey", "--draft-city", "Pune"]})
    assert block is not None
    assert "city='Pune'" in block
    assert "google_sheet' connector" not in block
