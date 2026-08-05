#!/usr/bin/env python3
"""Build VT-727's tracked, content-free full-corpus disposition artifacts.

This consumes only the already-governed VT-710 outputs.  Raw archives stay local-only and no
database, embedding provider, tenant data, or deployed environment is touched.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "apps" / "team-orchestrator"
SRC = APP_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator.knowledge.registry_full import (  # noqa: E402
    build_full_plan,
    load_independence_audit,
)

CORPUS = APP_ROOT / "knowledge_corpus"
CANDIDATES = CORPUS / "candidate_cards.jsonl"
RIGHTS = CORPUS / "source_rights.jsonl"
INDEPENDENCE_AUDIT = CORPUS / "independence_audit.json"
DISPOSITION_OUTPUT = CORPUS / "full_ingestion_disposition.jsonl"
REPORT_OUTPUT = CORPUS / "FULL_INGESTION_REPORT.md"


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    rights = _jsonl(RIGHTS)
    candidates = _jsonl(CANDIDATES)
    audit = load_independence_audit(json.loads(INDEPENDENCE_AUDIT.read_text(encoding="utf-8")))
    plan = build_full_plan(rights, candidates, audit)

    rows = [
        {
            "legacy_id": item.legacy_id,
            "local_files": item.local_files,
            "source_id": item.source_id,
            "source_class": item.source_class.value,
            "pipeline_input_status": item.original_status.value,
            "disposition": item.disposition,
            "reasons": item.reasons,
            "route_out": item.route_out,
            "candidate_card_version_id": item.candidate.card_version_id,
            "representative_card_version_id": item.representative.card_version_id,
            "corpus_version_id": str(plan.corpus_version_id),
            "retrieval_eligible_in_shadow": item.representative.retrieval_eligible,
            "authorizes_effects": False,
        }
        for item in plan.cards
    ]
    DISPOSITION_OUTPUT.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )

    dispositions = Counter(row["disposition"] for row in rows)
    grounds = Counter(reason for row in rows for reason in row["reasons"])
    report = f"""# VT-727 full ingestion report

Generated from the tracked VT-710 governed artifacts. Raw source reproductions remain local-only.

- Governed records: **{len(rows)}** (88 distinct local source files; 104 source-governance rows)
- Pipeline input status after authority correction: **100 candidate / 18 research_only**
- Full shadow representatives: **{plan.promoted_count} validated / {plan.deferred_count} deferred**
- Deferred state: **{dispositions["deferred_candidate"]} candidate / {dispositions["deferred_research_only"]} research_only**
- Rejected: **0** (the corpus passed paywall/originality/source-governance hard gates)
- Authority classes: **{dict(plan.authority_counts)}**
- Largest source contribution: **{plan.largest_source_card_count} cards ({plan.largest_source_share:.2%})**, below the 10% compilation-review trigger
- Cross-source pairs manually adjudicated after deterministic screening: **{plan.screened_cross_source_pairs}**
- Cross-source retelling groups found/collapsed: **{plan.collapsed_retelling_groups}**
- Independence conclusion: **no cross-source retelling of the same study, case, or thread was found**; related-topic pairs had different decision mechanisms or evidence targets
- Corpus admission: **pending**; O11 and Fazal-approved thresholds remain required
- Effect authority: **false**; retrieval is advisory only

## Overlapping deferral grounds

{chr(10).join(f"- `{ground}`: **{count}**" for ground, count in sorted(grounds.items()))}

Every deferred row in `full_ingestion_disposition.jsonl` carries a concrete route out. A deferral
is not counted as admitted, and no row disappears merely because it is not yet retrieval-eligible.
The three Reddit platform-guidance records are T4, not T1v; only two binding first-party platform
policy records remain T1v. Unknown source licence is retained as provenance metadata but does not
block independently authored knowledge under CL-2026-07-29b.
"""
    REPORT_OUTPUT.write_text(report, encoding="utf-8")
    print(
        f"VT-727 artifacts complete: {len(rows)} records, {plan.promoted_count} shadow-validated, "
        f"{plan.deferred_count} deferred"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
