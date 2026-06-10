"""VT-368 Gap-4 — the business-plan JSON contract: citation validator, downgrade
stripper, and the deterministic no-LLM degrade template.

The grounding rule (the anti-fabrication gate): every *claim-bearing token* in the
plan's English prose — numbers (ints, floats/ratings like ``4.2``, percentages,
currency like ``₹500``) and proper-noun tokens (platform/category names) — must be
backed by a literal in the version's FROZEN ``fact_bundle_json``. Citation ids
(``cited_facts``) must name real bundle entries. ``validate_plan`` reports; on
violations the caller DOWNGRADES via ``strip_violations`` (remove the offending
sentence / drop the offending item — never rewrite, never fabricate), and if
nothing grounded survives, falls back to ``degrade_template`` (bundle literals
only, EMPTY roadmap).

Deterministic-extractor design notes (documented limitations, all fail-closed
toward stripping rather than fabricating):

- Proper-noun candidates are capitalized Latin-script tokens. The LEADING run of
  capitalized words in a sentence is exempt (indistinguishable from sentence
  case — "Sharma Snacks Corner — ..." / "Reply to reviews"), so a fabricated
  platform name is only caught mid-sentence. Devanagari has no case; ``text_hi``
  is not scanned by ``validate_plan`` (the EN text is the canonical gate) but IS
  cleaned by ``strip_violations``.
- Sentence split is on ``. ! ? | ।`` with a decimal guard: a ``.`` followed by a
  digit (``4.2``) does not terminate a sentence.
- Numbers ``1``–``6`` immediately preceded by the word "month"/"months" (or
  "महीने"/"महीना") are exempt — the roadmap's own month axis ("in month 2") is
  structural, not a factual claim.
- Numbers are compared as normalized decimals (``4.20`` == ``4.2``); commas,
  ``₹`` and ``%`` are cosmetic. Bundle grounding walks every leaf of every fact
  entry (key/value/source), extracting numerals embedded in string values
  ("4.2/5 on Swiggy" grounds both 4.2 and 5).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from orchestrator.business_plan.store import ITEM_STATUSES, OWNING_AGENTS

OBJECTIVE_MAX_CHARS = 120
MONTH_MIN, MONTH_MAX = 1, 6

# A numeric token: not preceded by a word char or '.', digits with optional
# thousands-commas and a decimal tail. ₹/% are stripped by position (₹ is not \w).
_NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")

# Inline citation markers ([F1], [F12]) are the GROUNDING RECEIPTS the prompt requires — strip them
# before token extraction or the extractor flags the digits/Fid of every compliant citation and every
# clean generation degrades (adversarial-verify finding B2). Fabricated ids are still caught by the
# cited_facts-vs-bundle check, which runs on the structured lists, not the prose.
_CITATION_RE = re.compile(r"\[\s*F\d+\s*\]")
# Latin-script word tokens (proper-noun candidates + bundle word grounding).
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]*")
# Sentence terminators per the contract (. ! ? | ।) — '.' not followed by a
# digit, so decimals ("4.2") survive the split.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?|।])(?!\d)")
# Trailing word (any script) before a number — the month-axis carve-out.
_TRAILING_WORD_RE = re.compile(r"([^\W\d_]+)$", re.UNICODE)

_MONTH_WORDS = frozenset({"month", "months", "महीने", "महीना"})
# Capitalized-but-generic words that are calendar structure, not factual claims.
_CAP_STOPWORDS = frozenset(
    {"i", "month", "months", "week", "weeks", "day", "days", "quarter", "quarters"}
)


# --------------------------------------------------------------------------- grounding


def _canon_number(token: str) -> str | None:
    """Normalize a numeric token to a canonical decimal string ('4.20' -> '4.2',
    '1,200' -> '1200'); None if unparseable."""
    try:
        return format(Decimal(token.replace(",", "")).normalize(), "f")
    except InvalidOperation:
        return None


def _iter_leaves(node: Any):
    if isinstance(node, dict):
        for v in node.values():
            yield from _iter_leaves(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _iter_leaves(v)
    else:
        yield node


class _Grounding:
    """The bundle-literal sets the extractor checks against."""

    __slots__ = ("numbers", "words", "strings")

    def __init__(self, fact_bundle: dict[str, Any]) -> None:
        self.numbers: set[str] = set()
        self.words: set[str] = set()
        self.strings: list[str] = []
        for entry in (fact_bundle or {}).values():
            for leaf in _iter_leaves(entry):
                if isinstance(leaf, bool):
                    continue
                if isinstance(leaf, (int, float)):
                    canon = _canon_number(str(leaf))
                    if canon is not None:
                        self.numbers.add(canon)
                elif isinstance(leaf, str):
                    folded = leaf.casefold()
                    self.strings.append(folded)
                    for tok in _NUM_RE.findall(leaf):
                        canon = _canon_number(tok)
                        if canon is not None:
                            self.numbers.add(canon)
                    self.words.update(w.casefold() for w in _WORD_RE.findall(leaf))

    def grounds_word(self, word: str) -> bool:
        folded = word.casefold()
        return folded in self.words or any(folded in s for s in self.strings)


# --------------------------------------------------------------------------- extractor


def _split_sentences(text: str) -> list[str]:
    return _SENT_SPLIT_RE.split(text or "")


def _is_month_axis_ref(sentence: str, match: re.Match[str]) -> bool:
    """True for 1..6 immediately after the word month/months — roadmap structure,
    not a factual claim ("in month 2")."""
    canon = _canon_number(match.group())
    if canon not in {"1", "2", "3", "4", "5", "6"}:
        return False
    pre = sentence[: match.start()].rstrip(" \t-–—")
    trailing = _TRAILING_WORD_RE.search(pre)
    return trailing is not None and trailing.group(1).casefold() in _MONTH_WORDS


def _proper_noun_candidates(sentence: str) -> list[str]:
    """Capitalized Latin tokens past the leading capitalized run (sentence case)."""
    out: list[str] = []
    leading = True
    for word in _WORD_RE.findall(sentence):
        if not word[0].isupper():
            leading = False
            continue
        if leading:
            continue
        if len(word) >= 2 and word.casefold() not in _CAP_STOPWORDS:
            out.append(word)
    return out


def _sentence_ungrounded(sentence: str, grounding: _Grounding) -> list[tuple[str, str]]:
    """(token, kind) pairs in this sentence with no bundle-literal backing."""
    sentence = _CITATION_RE.sub(" ", sentence)  # B2: citation receipts are not claims
    bad: list[tuple[str, str]] = []
    for match in _NUM_RE.finditer(sentence):
        canon = _canon_number(match.group())
        if canon is None or canon in grounding.numbers:
            continue
        if _is_month_axis_ref(sentence, match):
            continue
        bad.append((match.group(), "number"))
    for word in _proper_noun_candidates(sentence):
        if not grounding.grounds_word(word):
            bad.append((word, "proper noun"))
    return bad


def _ungrounded_tokens(text: str, grounding: _Grounding) -> list[tuple[str, str]]:
    bad: list[tuple[str, str]] = []
    for sentence in _split_sentences(text):
        bad.extend(_sentence_ungrounded(sentence, grounding))
    return bad


# --------------------------------------------------------------------------- validate


def _item_violations(
    where: str, item: dict[str, Any], fact_ids: set[str], grounding: _Grounding
) -> list[str]:
    """Per-item checks (everything except list-level seq density / id uniqueness)."""
    v: list[str] = []
    item_id = item.get("item_id")
    if not (isinstance(item_id, str) and item_id.strip()):
        v.append(f"{where}: item_id empty")
    for fid in item.get("cited_facts") or []:
        if fid not in fact_ids:
            v.append(f"{where}.cited_facts: unknown fact id '{fid}' — not in fact_bundle")
    objective = item.get("objective") or ""
    why = item.get("why") or ""
    if not objective.strip():
        v.append(f"{where}: objective empty")
    elif len(objective) > OBJECTIVE_MAX_CHARS:
        v.append(f"{where}: objective exceeds {OBJECTIVE_MAX_CHARS} chars ({len(objective)})")
    if not why.strip():
        v.append(f"{where}: why empty")
    # owner_action/_hi are DELIVERED to the owner — scan them too, or a fabricated number rides the
    # action prompt past the gate (adversarial-verify Probe-2: the VTR-smuggle path).
    for field_name, text in (
        ("objective", objective),
        ("why", why),
        ("owner_action", item.get("owner_action") or ""),
        ("owner_action_hi", item.get("owner_action_hi") or ""),
    ):
        for token, kind in _ungrounded_tokens(text, grounding):
            v.append(
                f"{where}.{field_name}: ungrounded {kind} '{token}' — not a fact_bundle value"
            )
    agent = item.get("owning_agent")
    if agent not in OWNING_AGENTS:
        v.append(f"{where}: owning_agent '{agent}' not in OWNING_AGENTS")
    status = item.get("status")
    if status not in ITEM_STATUSES:
        v.append(f"{where}: status '{status}' not in ITEM_STATUSES")
    month = item.get("month")
    if not (
        isinstance(month, int) and not isinstance(month, bool) and MONTH_MIN <= month <= MONTH_MAX
    ):
        v.append(f"{where}: month {month!r} not in {MONTH_MIN}..{MONTH_MAX}")
    return v


def validate_plan(
    summary: dict[str, Any], roadmap: list[dict[str, Any]], fact_bundle: dict[str, Any]
) -> list[str]:
    """The anti-fabrication gate. Returns violation strings; [] == clean.

    Checks: (a) every cited fact id exists in the bundle; (b) every claim-bearing
    token in summary.text + each item's objective/why is a bundle literal;
    (c) owning_agent/status in their closed enums; (d) seq dense 1..N ascending;
    (e) month 1..6, objective/why non-empty, objective <=120 chars; (f) item_id
    non-empty + unique.
    """
    violations: list[str] = []
    grounding = _Grounding(fact_bundle)
    fact_ids = set(fact_bundle or {})

    for fid in summary.get("cited_facts") or []:
        if fid not in fact_ids:
            violations.append(
                f"summary.cited_facts: unknown fact id '{fid}' — not in fact_bundle"
            )
    for field_name in ("text", "text_hi"):
        for token, kind in _ungrounded_tokens(summary.get(field_name) or "", grounding):
            violations.append(
                f"summary.{field_name}: ungrounded {kind} '{token}' — not a fact_bundle value"
            )
    # headline_metrics are displayed verbatim — every value must be a bundle literal.
    for mkey, mval in (summary.get("headline_metrics") or {}).items():
        if isinstance(mval, bool):
            continue
        if isinstance(mval, (int, float)):
            if _canon_number(str(mval)) not in grounding.numbers:
                violations.append(
                    f"summary.headline_metrics.{mkey}: ungrounded number '{mval}'"
                )
        elif isinstance(mval, str) and mval.strip() and not grounding.grounds_word(mval):
            violations.append(
                f"summary.headline_metrics.{mkey}: ungrounded value '{mval}'"
            )

    seen_ids: dict[str, int] = {}
    seqs: list[Any] = []
    for idx, item in enumerate(roadmap):
        where = f"roadmap[{idx}]"
        violations.extend(_item_violations(where, item, fact_ids, grounding))
        item_id = item.get("item_id")
        if isinstance(item_id, str) and item_id.strip():
            if item_id in seen_ids:
                violations.append(
                    f"{where}: duplicate item_id '{item_id}' "
                    f"(first seen at roadmap[{seen_ids[item_id]}])"
                )
            else:
                seen_ids[item_id] = idx
        seqs.append(item.get("seq"))
    if seqs != list(range(1, len(seqs) + 1)):
        violations.append(f"roadmap: seq must be dense 1..{len(seqs)} ascending — got {seqs}")
    return violations


# ------------------------------------------------------------------------ downgrade


def _strip_text(text: str, grounding: _Grounding) -> str:
    """Drop every sentence containing an ungrounded token; keep the rest verbatim."""
    kept = [s for s in _split_sentences(text) if not _sentence_ungrounded(s, grounding)]
    return "".join(kept).strip()


def strip_violations(
    summary: dict[str, Any], roadmap: list[dict[str, Any]], fact_bundle: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """DOWNGRADE, never fabricate: remove offending sentences from summary text
    (text + text_hi — the Hindi mirror must not retain a stripped claim), drop
    unknown citation ids, DROP offending roadmap items wholesale, then re-seq
    densely 1..N. Inputs are not mutated.

    Returns ``(summary, roadmap, remaining_violations)`` where
    ``remaining_violations`` is ``validate_plan`` re-run on the cleaned plan —
    [] in every reachable case (defense-in-depth; if non-empty the caller falls
    back to ``degrade_template``).
    """
    grounding = _Grounding(fact_bundle)
    fact_ids = set(fact_bundle or {})

    new_summary = dict(summary)
    new_summary["cited_facts"] = [
        fid for fid in (summary.get("cited_facts") or []) if fid in fact_ids
    ]
    for key in ("text", "text_hi"):
        if isinstance(summary.get(key), str):
            new_summary[key] = _strip_text(summary[key], grounding)

    kept: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(roadmap):
        if _item_violations(f"roadmap[{idx}]", item, fact_ids, grounding):
            continue
        item_id = item["item_id"]
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        kept.append(dict(item))
    for new_seq, item in enumerate(kept, start=1):
        item["seq"] = new_seq

    return new_summary, kept, validate_plan(new_summary, kept, fact_bundle)


# ----------------------------------------------------------------------- degrade


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list, tuple)):
        return ", ".join(_format_value(leaf) for leaf in _iter_leaves(value))
    return str(value)


def degrade_template(
    fact_bundle: dict[str, Any], business_name: str | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The deterministic no-LLM fallback: a bilingual summary built ONLY from
    bundle literals + an EMPTY roadmap (no fabricated objectives). The output
    passes ``validate_plan`` against the same bundle by construction (the
    business_name leads its sentence, so a title-case name rides the leading-run
    exemption; a name with interior lowercase words — "Cafe de Paris" — would
    need the name in the bundle to validate clean).
    """
    fact_lines: list[str] = []
    headline_metrics: dict[str, Any] = {}
    for fid, entry in (fact_bundle or {}).items():
        if isinstance(entry, dict):
            key = str(entry.get("key") or fid)
            value = entry.get("value")
        else:
            key, value = str(fid), entry
        headline_metrics[key] = value
        fact_lines.append(f"{key}: {_format_value(value)}")

    name = (business_name or "").strip()
    prefix_en = f"{name} — here" if name else "Here"
    prefix_hi = f"{name} — " if name else ""
    facts_en = "; ".join(fact_lines) if fact_lines else "no verified facts yet"
    text = f"{prefix_en} is what we verified about your business: {facts_en}."
    text_hi = f"{prefix_hi}आपके व्यवसाय के सत्यापित तथ्य: {facts_en}।"

    summary = {
        "text": text,
        "text_hi": text_hi,
        "cited_facts": list(fact_bundle or {}),
        "headline_metrics": headline_metrics,
    }
    return summary, []
