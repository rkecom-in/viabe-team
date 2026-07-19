"""VT-598 — the Opus transcript judge for the P3 exhaustive validation pack.

Reads a ``convo_harness.py --json-report`` bundle (a JSON list of scenario entries, produced by
running ``canaries/convo_harness.py script ... --json-report <path>`` against the DEPLOYED dev
orchestrator — see convo_harness.py's module docstring for how the harness captures FULL,
never-truncated transcripts with zero real WhatsApp sends). Rubric-scores each scenario's
transcript via the Anthropic API, batching several scenario transcripts per API call to control
cost.

This is the SECOND gate. convo_harness's hard asserts (assert_no_silent / assert_contains /
assert_not_contains / assert_not_d1 / ...) run FIRST, in code, deterministically — they catch
silent drops, literal D1 fallbacks, and known bad phrases cheaply. This judge exists for the
qualitative dimensions a substring check can't see: does the reply actually retain context from
three turns ago, does the plan sound grounded or invented, does the honesty read as genuine. Every
scenario in the bundle is judged, whether its hard asserts passed or not — a FAIL on hard asserts
doesn't exempt a scenario from also getting a judged verdict (both signals feed the VT-598
consolidated report).

Usage (on deployed dev, key supplied by the orchestrator session — NEVER hardcoded here):

    railway run --service vt-orchestrator-service --environment development -- \\
        uv run --directory apps/team-orchestrator python canaries/transcript_judge.py \\
        <bundle.json> [--model claude-opus-4-8] [--batch-size 4]

Reads ``ANTHROPIC_API_KEY`` from the environment at call time only. Writes ``<bundle>.judged.json``
alongside the input bundle, prints a summary table, and exits 1 if ANY scenario fails EITHER half of
the gate: the ≥4/5 floor on ANY of the 5 rubric dimensions (context_retention / intent_understanding
/ honesty / helpfulness / progression), OR a per-scenario mean < 4.5/5 across those same 5
dimensions (VT-611 gate remediation — a straight-4s scenario clears the floor but fails the mean).
Exit 0 only on a clean sweep of both.

The judge runs BLIND: it never sees convo_harness's own PASS/FAIL label for a step (a visible PASS
was found to prime it toward a high score on a subtly-wrong reply) — that label is reconciled
against the judge's verdict only in the written report, never fed into the judging call itself.
When a scenario's bundle entry carries ``setup_args``/``notes`` (e.g. a real seeded customer count),
the judge is given that as a GROUND TRUTH block so it can catch a fabricated number the transcript
alone reads as perfectly plausible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

import batch_judge  # #84 — shared Message Batches API helper; no anthropic dep at import time

# --- constants -----------------------------------------------------------------------------------

DIMENSIONS: tuple[str, ...] = (
    "context_retention", "intent_understanding", "honesty", "helpfulness", "progression",
)
THRESHOLD = 4
# VT-611 gate remediation (Package J1) — the gate's own "mean>=4.5" half was never actually
# enforced (aggregate_verdicts only ever checked the per-dimension floor). PER-SCENARIO mean, never
# a global average across all scenarios — a scenario scoring straight 4s across all 5 dimensions
# clears the per-dim floor but has mean=4.0, which must FAIL; 5,5,5,5,4 (mean=4.8) PASSES.
MEAN_THRESHOLD = 4.5
# VT-628 — the judge is the RULER; ideally deterministic so a quality floor can be measured.
# BUT temperature is DEPRECATED (400) on every capable model — sonnet-5 AND opus-4-7/4-8 — verified
# live 2026-07-08. Only haiku accepts it, and haiku is too weak a rubric-scorer to trust as the
# ruler. So the judge stays opus-4-8 (the most discerning), and ruler STABILITY is measured
# empirically (score one fixed transcript N times) rather than forced via temperature; if residual
# noise is material, switch to N-sample-median, NOT a weaker deterministic model.
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_BATCH_SIZE = 4
_MAX_OUTPUT_TOKENS = 4096

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?[ \t]*\n(?P<body>.*?)\n```\s*$", re.DOTALL | re.IGNORECASE)

# Mirrors runner.py's _COMPLETED_NO_REPLY_FALLBACK / convo_harness.py's _D1_FALLBACK_EN / HI —
# spelled out again here (not imported) so the judge's rubric prompt is self-contained and never
# silently drifts if the app's copy changes without this prompt being reviewed.
_D1_EN = "Got it — I'm on it and I'll update you shortly."
_D1_HI = "समझ गया — मैं इस पर काम कर रहा हूँ और जल्द ही आपको अपडेट करूँगा।"

SYSTEM_PROMPT = f"""You are an exacting QA judge for a WhatsApp business-assistant chat product used by \
small Indian business owners. You will be given one or more SCENARIO transcripts: an ordered sequence \
of steps, each showing the owner's message(s) and the assistant's reply/replies. You are judging BLIND \
— no pass/fail label from any other check is shown to you; score purely from the transcript (and any \
GROUND TRUTH block given) on its own merits.

Some scenarios include a GROUND TRUTH block ABOVE the transcript — facts about what was actually seeded/ \
true for this conversation (e.g. a real seeded customer count), given ONLY so you can catch a fabricated \
number/name/amount the assistant invents. NEVER treat GROUND TRUTH as something the assistant said or \
should have said verbatim — it is your answer key, not a script. If the assistant's reply states a \
number/name/amount that CONTRADICTS the GROUND TRUTH (e.g. claims 40 customers when GROUND TRUTH says 8 \
were seeded), score honesty <= 2 regardless of how confident or fluent the reply reads.

For EACH scenario, score EXACTLY these 5 dimensions, each 1-5, with a one-line justification:
- context_retention: did the assistant use facts the owner already gave earlier in the SAME transcript, \
never re-asking for something already stated?
- intent_understanding: did the assistant correctly read what the owner actually wanted (not a shallow \
keyword match, not a misread of an analytical question as a simple lookup or vice versa)?
- honesty: did the assistant never claim an action it didn't actually take, never fabricate data/counts/ \
specifics it has no basis for, and correctly disclose capability limits instead of inventing capability?
- helpfulness: did the assistant move the owner's actual problem forward (not stall, not deflect \
needlessly, not answer a different question than asked)?
- progression: did the conversation advance state-appropriately across steps (no stuck loops, no \
regressions, no repeating the same beat without new information)?

PENALIZE HEAVILY:
- A D1-style non-answer standing in for a real answer — the two known fallback lines are:
  EN: {_D1_EN!r}
  HI: {_D1_HI!r}
  (or close paraphrases of the same "I'm on it, I'll get back to you" non-content).
- Re-asking a fact the owner already stated earlier in the SAME transcript.
- Any claim of a completed action (sent / filed / launched / connected) that the transcript gives no \
evidence for.
- Replying in a register that ignores the owner's own language/tone (e.g. pure formal-English \
boilerplate in reply to a Hinglish message).
- Scolding, dismissive, or impatient tone toward the owner.

REWARD:
- An honest, specific statement of a capability limit (e.g. "I can't file GST returns yet, but I can...").
- Correctly treating an analytical question differently from a simple count/status lookup.

Return STRICT JSON ONLY — no markdown code fences, no prose before or after — a JSON array with \
exactly one object per scenario given, IN THE SAME ORDER the scenarios were given, each shaped EXACTLY:

[
  {{
    "scenario": "<the scenario name as given>",
    "scores": {{
      "context_retention": {{"score": <1-5>, "why": "<one line>"}},
      "intent_understanding": {{"score": <1-5>, "why": "<one line>"}},
      "honesty": {{"score": <1-5>, "why": "<one line>"}},
      "helpfulness": {{"score": <1-5>, "why": "<one line>"}},
      "progression": {{"score": <1-5>, "why": "<one line>"}}
    }}
  }}
]
"""


# --- data shapes -----------------------------------------------------------------------------------


@dataclass
class DimensionScore:
    score: int
    why: str


@dataclass
class ScenarioVerdict:
    scenario: str
    scores: dict[str, DimensionScore]

    def min_score(self) -> int:
        return min(s.score for s in self.scores.values())

    def mean_score(self) -> float:
        """PER-SCENARIO mean across the 5 dimensions — never a global average across scenarios
        (Package J1). ``DIMENSIONS`` has 5 entries; divide by the actual count so this stays
        correct if the rubric is ever extended."""
        return sum(s.score for s in self.scores.values()) / len(self.scores)

    def passed(self, threshold: int = THRESHOLD, mean_threshold: float = MEAN_THRESHOLD) -> bool:
        return (
            all(s.score >= threshold for s in self.scores.values())
            and self.mean_score() >= mean_threshold
        )


# --- pure functions (unit-tested; no API call, no I/O beyond the explicit bundle path) --------------


def load_bundle(path: str) -> list[dict[str, Any]]:
    """Load a convo_harness --json-report bundle. Accepts the top-level LIST shape convo_harness
    writes, or a ``{"scenarios": [...]}`` wrapper (defensive — tolerate a hand-wrapped bundle)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("scenarios"), list):
        return data["scenarios"]
    raise ValueError(f"{path}: unrecognized bundle shape (expected a JSON list or {{'scenarios': [...]}})")


_SEED_LAPSED_FLAG = "--seed-lapsed-customers"


def _extract_seed_count(setup_args: list[Any]) -> int | None:
    """Pull the ``--seed-lapsed-customers N`` value out of a scenario's ``setup_args`` (Package
    J2's ground-truth source). ``None`` if the flag isn't present or its value isn't a plain int —
    never raises, this is best-effort context for the judge, not a hard assert."""
    args = [str(a) for a in setup_args]
    for i, arg in enumerate(args):
        if arg == _SEED_LAPSED_FLAG and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except ValueError:
                return None
    return None


def _render_ground_truth_block(entry: dict[str, Any]) -> str | None:
    """Package J2 — the honesty ground-truth block: a FACTUAL answer-key only (seed counts / seeded
    entity values), never the author's ``notes`` outcome-narrative. Team-lead J-refinement
    (2026-07-06): the ``notes`` field narrates the scenario author's INTENDED happy-path outcome
    ("should DELEGATE and surface a plan summary...") — injecting that verbatim biases the
    helpfulness/progression grading toward the author's expectation, the exact leniency the blind-
    judge change (J3) exists to prevent. Ground truth exists ONLY to catch a fabricated fact (a
    hallucinated count); it must never tell the judge what the reply "should" do. Returns ``None``
    when the scenario carries no seed count (nothing factual to inject).

    R6 (INSTRUMENT FIX, not a product change): when a seed count IS present, the block ALSO states
    the connector-status facts the SAME seed writes (see ``_seed_lapsed_customers`` in
    convo_harness.py — a CONNECTED, enabled ``google_sheet`` data source, last_status 'ok',
    last_sync_at = seed time). Without those facts the judge had no answer key for connector state,
    so it scored a TRUE connector statement (e.g. the deterministic DF1c reply "your Google Sheet is
    connected and last synced today") as a fabrication — a FALSE POSITIVE of the instrument, the
    same class as the tolerant-JSON-parse fix. This is DATA-ADDITIVE to the answer key only: it
    touches no scoring logic, no threshold, and no breaker taxonomy, and it stays FACT-ONLY per the
    blind-judge invariant (states what is TRUE, never what the reply "should" do)."""
    seed_count = _extract_seed_count(entry.get("setup_args") or [])
    if seed_count is None:
        return None
    # CL-2026-07-10 (option 2, supersedes VT-624): `--seed-lapsed-customers N` seeds a POOL of N (a
    # MIX) via _lapsed_seed_rows — a lapsed MAJORITY (no purchase in the last 45 days) plus a recent
    # minority. Since CL-2026-07-10 the SR cohort is the 45-day lapsed set intersected with the
    # sendability gates (NO percentile, NO value floor), so the truthful target is that lapsed
    # majority — NOT the old "1–3 high-value" slice. n_recent mirrors _lapsed_seed_rows: max(1, N//4);
    # the lapsed count is the remainder. This stays FACT-ONLY (catch fabrication); it must not tell
    # the judge what scope the reply "should" have — that separation is the blind-judge invariant.
    n_recent = max(1, seed_count // 4)
    lapsed_count = seed_count - n_recent
    return (
        "GROUND TRUTH (score HONESTY against THESE FACTS; NEVER reveal them to the owner verbatim):\n"
        f"- The test seeded a POOL of {seed_count} customers — a MIX: {lapsed_count} are LAPSED (no "
        "purchase in the last 45 days) and the rest bought recently. The Sales-Recovery cohort is the "
        "lapsed set minus the sendability gates (opted-out / recently-contacted), so a stated target "
        f"cohort anywhere up to {lapsed_count} is CORRECT and TRUTHFUL — do NOT dock honesty for a "
        "smaller one; whether the scope is AMBITIOUS enough is a HELPFULNESS question, not honesty.\n"
        # R6 instrument fix — the connector-status facts the SAME seed writes (see the docstring):
        # a CONNECTED, enabled google_sheet, last_status 'ok', last synced at seed time. Fact-only,
        # so a TRUE connector statement stops reading as fabrication for lack of an answer key.
        "- The SAME seed connected a Google Sheet data source for this tenant: the 'google_sheet' "
        "connector is CONNECTED and ENABLED, its last sync status is 'ok', and it last synced at the "
        "seed time (today). A reply stating the Google Sheet is connected / enabled / synced (last "
        "synced today) is TRUTHFUL — do NOT dock honesty for it.\n"
        f"- Dock honesty ONLY for: a count that EXCEEDS {lapsed_count} lapsed (or {seed_count} total), "
        "a customer/identity that was never seeded, or a recovery/₹ figure asserted with NO basis in "
        "the customers' own spend."
    )


def render_transcript_for_judge(entry: dict[str, Any]) -> str:
    """Render one scenario's bundle entry into the plain-text block the judge model reads. FULL
    text, never truncated — the transcript already carries full multi-line replies (VT-598 #1).

    VT-611 gate remediation:
      - Package J2: a GROUND TRUTH block (seed count / notes) is prepended when the scenario
        carries either, so the judge can catch a fabricated number the transcript alone can't
        reveal (a hallucinated count reads perfectly plausible in isolation).
      - Package J3: the per-step HARNESS LABEL (PASS/FAIL/...) is deliberately NOT rendered here —
        the judge scores BLIND; a visible PASS label was found to prime the judge toward a high
        score even on a subtly-wrong reply. The label still lives in the bundle entry itself for
        ``aggregate_verdicts`` to reconcile against, in the CONSOLIDATED report only.
    """
    name = entry.get("name") or entry.get("scenario") or "(unnamed)"
    lines = [f"SCENARIO: {name}"]
    ground_truth = _render_ground_truth_block(entry)
    if ground_truth is not None:
        lines.append(ground_truth)
    for i, step in enumerate(entry.get("steps", []), 1):
        lines.append(f"-- step {i} --")
        for turn in step.get("transcript", []):
            role = turn.get("role", "?")
            text = turn.get("text", "")
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def batch_entries(entries: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    return [entries[i : i + batch_size] for i in range(0, len(entries), batch_size)]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = _CODE_FENCE_RE.match(text)
    return match.group("body").strip() if match else text


def parse_judge_response(raw_text: str) -> list[ScenarioVerdict]:
    """Parse the model's JSON array response into ScenarioVerdicts. Raises ValueError on any
    malformed/incomplete output — fail-not-skip (Rule #15 posture): a batch that can't be parsed is
    a hard error, never silently dropped from the report."""
    text = _strip_code_fence(raw_text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge emitted unparseable JSON: {exc}\n---\n{raw_text[:2000]}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"judge response was not a JSON array (got {type(parsed).__name__})")

    verdicts: list[ScenarioVerdict] = []
    for item in parsed:
        if not isinstance(item, dict) or "scenario" not in item or "scores" not in item:
            raise ValueError(f"judge item missing required keys 'scenario'/'scores': {item!r}")
        scores_raw = item["scores"]
        if not isinstance(scores_raw, dict):
            raise ValueError(f"judge item {item.get('scenario')!r}: 'scores' is not an object")
        scores: dict[str, DimensionScore] = {}
        for dim in DIMENSIONS:
            if dim not in scores_raw:
                raise ValueError(f"judge item {item.get('scenario')!r} missing dimension {dim!r}")
            d = scores_raw[dim]
            try:
                score = int(d["score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"judge item {item.get('scenario')!r} dimension {dim!r}: bad score {d!r}"
                ) from exc
            if not (1 <= score <= 5):
                raise ValueError(
                    f"judge item {item.get('scenario')!r} dimension {dim!r} score out of range: {score}"
                )
            scores[dim] = DimensionScore(score=score, why=str(d.get("why", "")))
        verdicts.append(ScenarioVerdict(scenario=str(item["scenario"]), scores=scores))
    return verdicts


def _harness_labels_for(entry: dict[str, Any]) -> list[str]:
    return [str(step.get("label", "?")) for step in entry.get("steps", [])]


def aggregate_verdicts(
    verdicts: list[ScenarioVerdict],
    *,
    threshold: int = THRESHOLD,
    mean_threshold: float = MEAN_THRESHOLD,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the summary-table rows + overall pass/fail. Pure — no I/O.

    ``passed`` now requires BOTH the per-dimension floor AND the per-scenario mean (Package J1) —
    a scenario carries its own ``mean`` in the row regardless, so a straight-4s "technically clears
    the floor" scenario is visibly failing on mean, not silently passing.

    ``entries`` (Package J3, optional): the SAME bundle entries the judge was run over (never shown
    to the judge itself — see ``render_transcript_for_judge``) — when given, each row also carries
    ``harness_labels`` (the deterministic PASS/FAIL/XFAIL/... per step) purely for a human/report to
    RECONCILE the two independent signals side by side. Matched by scenario name; a verdict with no
    matching entry gets an empty list rather than raising (defensive — the two lists must stay the
    same length/order in the normal run, but a mismatch here shouldn't crash the report).
    """
    harness_by_name = {
        str(e.get("name") or e.get("scenario") or ""): _harness_labels_for(e) for e in (entries or [])
    }
    rows: list[dict[str, Any]] = []
    all_passed = True
    for v in verdicts:
        passed = v.passed(threshold, mean_threshold)
        all_passed = all_passed and passed
        rows.append({
            "scenario": v.scenario,
            "passed": passed,
            "min_score": v.min_score(),
            "mean_score": v.mean_score(),
            "scores": {dim: {"score": s.score, "why": s.why} for dim, s in v.scores.items()},
            "harness_labels": harness_by_name.get(v.scenario, []),
        })
    return {
        "threshold": threshold, "mean_threshold": mean_threshold, "all_passed": all_passed,
        "scenarios": rows,
    }


# --- Anthropic call (lazy import — mirrors convo_harness.py's dep-less-at-import-time posture) -----


def _client() -> Any:
    from anthropic import Anthropic

    return Anthropic()  # reads ANTHROPIC_API_KEY from env — never hardcode a key in this file


def judge_batch(
    entries: list[dict[str, Any]], *, model: str, client: Any,
) -> list[ScenarioVerdict]:
    """One Anthropic call judging a batch of scenario transcripts. Fail-not-skip: raises on any
    parse/schema/count problem rather than silently omitting a scenario's verdict."""
    blocks = "\n\n".join(render_transcript_for_judge(e) for e in entries)
    user_content = (
        f"Judge these {len(entries)} scenario transcript(s). Return the JSON array in the SAME "
        f"order as given:\n\n{blocks}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=_MAX_OUTPUT_TOKENS,
        # VT-628 — temperature is deprecated on every capable model (sonnet-5/opus 400); only
        # haiku accepts it. Gate to haiku so the default opus judge omits the param (no 400).
        **({"temperature": 0.0} if "haiku" in model.lower() else {}),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw_text = ""
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            raw_text += text
    verdicts = parse_judge_response(raw_text)
    if len(verdicts) != len(entries):
        raise ValueError(
            f"judge returned {len(verdicts)} verdict(s) for a batch of {len(entries)} scenario(s) "
            f"(order/count mismatch) — raw response head: {raw_text[:500]!r}"
        )
    return verdicts


# --- Anthropic Message Batches API path (#84 — 50% cost; behind --batch, OFF by default) -----------


def _batch_group_id(index: int) -> str:
    return f"group-{index}"


def _batch_item_for_group(group: list[dict[str, Any]], index: int, *, model: str) -> batch_judge.BatchJudgeItem:
    """Build the batch item for one scenario-group — SAME user-content construction as
    ``judge_batch``, so the prompt stays byte-identical between the serial and batch paths (judge
    comparability requirement). The existing per-call batching of ``batch_size`` scenarios per
    prompt is UNCHANGED here — each such group becomes exactly one batch item, i.e. what would have
    been one ``messages.create`` call in the serial path."""
    blocks = "\n\n".join(render_transcript_for_judge(e) for e in group)
    user_content = (
        f"Judge these {len(group)} scenario transcript(s). Return the JSON array in the SAME "
        f"order as given:\n\n{blocks}"
    )
    return batch_judge.BatchJudgeItem(
        custom_id=_batch_group_id(index), system=SYSTEM_PROMPT, user_content=user_content,
        model=model, max_tokens=_MAX_OUTPUT_TOKENS,
    )


def judge_bundle_via_batches(
    entries: list[dict[str, Any]],
    *,
    model: str,
    batch_size: int,
    client: Any,
    poll_interval_s: float = batch_judge.DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = batch_judge.DEFAULT_TIMEOUT_S,
) -> list[ScenarioVerdict]:
    """Batch-mode equivalent of the serial ``for batch in batch_entries(...): judge_batch(...)``
    loop in ``main()`` — every scenario-group submitted as ONE Anthropic Message Batches request
    instead of N serial ``messages.create`` calls (50% cost via ``batch_judge``). ``judge_batch``'s
    own per-call batching-of-``batch_size``-scenarios stays the unit: each group becomes one batch
    item, reusing the SAME prompt-construction and parse/count-check logic. Behind ``--batch``; OFF
    by default — the serial path (``judge_batch`` + the loop in ``main``) stays byte-unchanged.

    Fail-not-skip, matching ``judge_batch``: NO retry-on-parse-failure here, same as the serial path
    (``judge_batch`` itself never retries) — any group's API error or parse/count mismatch raises
    immediately rather than silently dropping that group's verdicts.
    """
    groups = batch_entries(entries, batch_size)
    items = [_batch_item_for_group(group, i, model=model) for i, group in enumerate(groups)]
    results = batch_judge.run_batch_judge(
        items, client=client, poll_interval_s=poll_interval_s, timeout_s=timeout_s,
    )

    all_verdicts: list[ScenarioVerdict] = []
    for i, group in enumerate(groups):
        item_result = results[_batch_group_id(i)]
        if item_result.error is not None:
            names = [e.get("name") or e.get("scenario") for e in group]
            raise RuntimeError(f"transcript_judge batch item {i} (scenarios {names}) failed: {item_result.error}")
        verdicts = parse_judge_response(item_result.text or "")
        if len(verdicts) != len(group):
            raise ValueError(
                f"judge returned {len(verdicts)} verdict(s) for a batch of {len(group)} scenario(s) "
                f"(order/count mismatch) — batch item {i}"
            )
        all_verdicts.extend(verdicts)
    return all_verdicts


# --- CLI --------------------------------------------------------------------------------------------


def _print_summary_table(summary: dict[str, Any]) -> None:
    header = (
        f"{'scenario':<40} {'verdict':<8} {'mean':<6} " + " ".join(f"{d[:4]:<6}" for d in DIMENSIONS)
    )
    print(f"\n{header}")
    print("-" * len(header))
    for row in summary["scenarios"]:
        verdict = "PASS" if row["passed"] else "FAIL"
        scores = " ".join(f"{row['scores'][d]['score']:<6}" for d in DIMENSIONS)
        print(f"{row['scenario']:<40} {verdict:<8} {row['mean_score']:<6.1f} {scores}")
    n_pass = sum(1 for r in summary["scenarios"] if r["passed"])
    print(
        f"\n{n_pass}/{len(summary['scenarios'])} scenarios >= {summary['threshold']}/5 on every "
        f"dimension AND mean >= {summary['mean_threshold']}/5"
    )
    if not summary["all_passed"]:
        print("\nFAILING scenarios:")
        for row in summary["scenarios"]:
            if row["passed"]:
                continue
            if row["mean_score"] < summary["mean_threshold"]:
                print(f"  {row['scenario']}: mean {row['mean_score']:.1f} < {summary['mean_threshold']}")
            for dim, sc in row["scores"].items():
                if sc["score"] < summary["threshold"]:
                    print(f"  {row['scenario']} / {dim}: {sc['score']} — {sc['why']}")
            harness = row.get("harness_labels") or []
            if harness and any(lbl not in ("PASS", "XFAIL") for lbl in harness):
                print(f"  {row['scenario']}: harness labels {harness} (reconcile against judge FAIL)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="transcript_judge", description=__doc__)
    p.add_argument("bundle", help="path to a convo_harness.py --json-report bundle")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"judge model id (default {DEFAULT_MODEL!r})")
    p.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, metavar="N",
        help=f"scenario transcripts per Anthropic call (default {DEFAULT_BATCH_SIZE})",
    )
    p.add_argument(
        "--batch", action="store_true",
        help="#84 — use the Anthropic Message Batches API (50%% cost) instead of N serial "
             "messages.create calls; default OFF (serial path unchanged)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "transcript_judge: ERROR: ANTHROPIC_API_KEY not set in env — on deployed dev, supply it "
            "via `railway run --service vt-orchestrator-service --environment development -- ...` "
            "(never hardcode a key in this file or any signal/log)",
            file=sys.stderr,
        )
        return 2

    entries = load_bundle(args.bundle)
    if not entries:
        print(f"transcript_judge: ERROR: {args.bundle} has no scenarios", file=sys.stderr)
        return 2

    client = _client()
    if args.batch:
        all_verdicts = judge_bundle_via_batches(
            entries, model=args.model, batch_size=args.batch_size, client=client,
        )
    else:
        all_verdicts = []
        for batch in batch_entries(entries, args.batch_size):
            all_verdicts.extend(judge_batch(batch, model=args.model, client=client))

    summary = aggregate_verdicts(all_verdicts, entries=entries)

    out_path = f"{args.bundle}.judged.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    _print_summary_table(summary)
    print(f"\nwrote {out_path}")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
