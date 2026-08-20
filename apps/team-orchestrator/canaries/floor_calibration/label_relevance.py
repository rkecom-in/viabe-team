"""VT-725 floor recalibration — BLIND relevance labels for every (case, card) pair.

Blindness rules, because the floor derived from these labels is only as honest as they are:
  * the labeller sees the case's AGENT VIEW only — never acceptable_characteristics, never the
    target answer, never risk flags. Labels steered by the answer key would make the floor a
    measurement of the answer key.
  * the labeller NEVER sees a retrieval score, rank, or card id. It cannot agree with the scorer
    because it cannot see the scorer.
  * card order is SHUFFLED per pass with a fixed per-pass seed, so position bias cannot survive the
    majority vote and the shuffle is reproducible.

Three independent passes; majority vote is the label; pairwise agreement is reported so anyone can
see how solid the labels actually are rather than taking the vote on faith.
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "apps/team-orchestrator/src")

IN = Path(sys.argv[1])
OUT = Path(sys.argv[2])
MODEL = sys.argv[3] if len(sys.argv) > 3 else "claude-opus-4-5-20251101"
PASSES = 3

RUBRIC = """You are labelling which reference claims are RELEVANT to one small-business owner's request.

A claim is RELEVANT only if a competent advisor writing the answer to THIS request would be
materially better off having that claim in front of them — it bears on the specific decision, its
risks, its constraints, or the numbers involved.

A claim is IRRELEVANT if it is about a different domain, a different kind of decision, or is a
general business truism that would not change anything the advisor writes here. Being "vaguely
business-related" is NOT relevance. Most claims in a broad corpus are irrelevant to any one
request; a typical answer marks well under a quarter of them relevant. Do not pad the list.

Judge each claim on its own merits. Return STRICT JSON only, no prose:
{"relevant": [<1-based indices of relevant claims>]}"""


def build_prompt(case: dict, ordering: list[int], claims: list[str]) -> str:
    lines = [
        RUBRIC,
        "",
        "=== THE OWNER'S SITUATION ===",
        case["objective"],
        "",
        f"(business context: industry={case['industry']!r}, jurisdiction={case['jurisdiction']!r})",
        "",
        f"=== {len(ordering)} CANDIDATE CLAIMS ===",
    ]
    for position, card_index in enumerate(ordering, start=1):
        lines.append(f"{position}. {claims[card_index]}")
    return "\n".join(lines)


def parse(raw: str, n: int) -> set[int]:
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError(f"labeller returned no JSON object: {raw[:200]!r}")
    payload = json.loads(match.group(0))
    picked = payload["relevant"]
    if not isinstance(picked, list):
        raise ValueError("relevant must be a list")
    out = set()
    for value in picked:
        position = int(value)
        if not 1 <= position <= n:
            raise ValueError(f"index {position} out of range 1..{n}")
        out.add(position)
    return out


def main() -> int:
    from anthropic import Anthropic

    client = Anthropic()
    data = json.loads(IN.read_text())
    result = {"model": MODEL, "passes": PASSES, "cases": []}

    for case in data["cases"]:
        claims = [row["claim"] for row in case["cards"]]
        n = len(claims)
        votes = [0] * n
        per_pass: list[list[int]] = []

        for pass_index in range(PASSES):
            ordering = list(range(n))
            random.Random(1000 + pass_index).shuffle(ordering)
            prompt = build_prompt(case, ordering, claims)
            response = client.messages.create(
                model=MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            positions = parse(text, n)
            # positions are 1-based INTO THE SHUFFLE — map back to card index.
            card_indices = sorted(ordering[p - 1] for p in positions)
            per_pass.append(card_indices)
            for card_index in card_indices:
                votes[card_index] += 1
            print(f"  {case['case_id']} pass {pass_index + 1}: {len(card_indices)} relevant", flush=True)

        labels = [1 if v >= 2 else 0 for v in votes]
        # Pairwise agreement across passes, reported not assumed.
        sets = [set(p) for p in per_pass]
        agreements = []
        for a in range(PASSES):
            for b in range(a + 1, PASSES):
                union = sets[a] | sets[b]
                agreements.append(len(sets[a] & sets[b]) / len(union) if union else 1.0)
        unanimous = sum(1 for v in votes if v in (0, PASSES))

        result["cases"].append(
            {
                "case_id": case["case_id"],
                "split": case["split"],
                "labels": labels,
                "votes": votes,
                "per_pass_counts": [len(p) for p in per_pass],
                "pairwise_jaccard_agreement": agreements,
                "unanimous_cards": unanimous,
                "relevant_count": sum(labels),
            }
        )
        print(
            f"{case['case_id']}: relevant={sum(labels)}/{n} "
            f"unanimous={unanimous}/{n} agreement={[round(a, 3) for a in agreements]}",
            flush=True,
        )

    OUT.write_text(json.dumps(result, indent=1))
    print(f"wrote {OUT}")
    print("relevant-per-case:", Counter(c["relevant_count"] for c in result["cases"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
