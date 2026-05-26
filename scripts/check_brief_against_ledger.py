#!/usr/bin/env python3
"""check_brief_against_ledger.py — pre-flight ledger check for brief-ready dispatch.

Usage:
    python scripts/check_brief_against_ledger.py .viabe/sprint/VT-<N>.md
    python scripts/check_brief_against_ledger.py .viabe/sprint/VT-28.md --strict

Reads the sprint file's title + body + dependencies, extracts domain
keywords, greps docs/clau/ledger-index.md for matching Standing decisions,
and prints any that need reconciliation BEFORE the brief-ready signal is
dispatched.

Exit codes:
    0  — no relevant Standing decisions found (or all surfaced as reconciled)
    1  — relevant Standing decisions found that the brief doesn't mention
    2  — sprint file missing / ledger-index missing / parse error

The script is intentionally noisy — over-surfacing is fine; under-surfacing is
the bug. The Cowork operator decides which surfaced decisions are actually
relevant (Type 2 Cowork call) and adds them to the brief-ready signal's
`cl_decisions_checked:` frontmatter field.

History: written 2026-05-26 04:20 IST after Cowork (without this check)
shipped VT-101/102/103/104 against LangSmith despite CL-56 (2026-05-16,
Standing) replacing LangSmith with Pydantic Logfire. Rule #16 codifies the
discipline; this script enforces it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_INDEX = REPO_ROOT / "docs" / "clau" / "active-context-summary.md"

# Words that are too generic to match on — skip them when extracting brief
# keywords. Otherwise every brief would match every Standing decision.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at",
    "for", "with", "is", "are", "was", "were", "be", "been", "being", "this",
    "that", "these", "those", "it", "its", "as", "by", "from", "not", "no",
    "must", "should", "will", "shall", "can", "may", "do", "does", "did",
    "has", "have", "had", "task", "subtask", "row", "phase", "step", "scope",
    "ship", "ships", "shipped", "build", "builds", "built", "see", "use",
    "uses", "used", "via", "per", "any", "all", "one", "two", "three", "four",
    "five", "vt", "cl", "id", "ids", "test", "tests", "data", "code", "file",
    "files", "name", "type", "value", "list", "set", "row", "rows", "table",
    "tables", "column", "columns", "field", "fields", "ref", "refs", "title",
    "body", "spec", "main", "dev", "true", "false", "null", "none", "page",
    "pages", "line", "lines", "ok", "yes", "added", "removed", "edit", "edits",
}

# Multi-word phrases that should be matched as a unit (not split into tokens).
# Add to this list as new domains emerge.
KEY_PHRASES = [
    "langsmith", "logfire", "pydantic logfire", "owner_inputs", "owner inputs",
    "campaign plan", "campaignplan", "rate limit", "rate-limit", "pipeline_log",
    "pipeline log", "pipeline_steps", "pipeline steps", "pipeline_runs",
    "knowledge graph", "knowledge-graph", "kg", "rls", "guc", "twilio",
    "anthropic", "voyage", "dbos", "langgraph", "supervisor", "haiku", "opus",
    "self-evaluate", "self evaluate", "dispatch", "checkpointer", "trace",
    "tracing", "observability", "consent", "privacy", "dpa", "dlt", "vilpower",
    "meta", "whatsapp", "k-anonymity", "k anonymity", "tier-a", "tier-b",
    "pre-filter", "pre filter", "scheduled trigger", "scheduled-trigger",
    "day-39", "day 39", "attribution", "monthly impact", "weekly cadence",
    "mem0", "memory tier", "memory-tier", "l0", "l1", "l2", "l3", "l4",
    "orchestrator", "specialist", "sales recovery", "sales-recovery",
    "claude code", "claude-code", "coderx", "coderc", "codex", "service key",
    "api key", "secret", "env var", "env-var", "migration", "migrations",
    "postgres", "pgvector", "supabase", "step record", "step-record",
    "draft_message_variants", "agent sdk", "agent-sdk", "messages api",
    "messages-api", "messages sdk", "two-mode", "two mode", "canary",
    "ship-thin", "ship thin", "tenant_id", "tenant id", "merge", "branch",
    "admin-bypass", "admin bypass", "snapshot", "discipline rule",
    "discipline-rule", "rule #14", "rule #15", "rule #16",
]


def extract_brief_text(sprint_file: Path) -> str:
    """Return the brief's title + body for keyword extraction."""
    if not sprint_file.exists():
        sys.exit(f"ERROR: sprint file not found: {sprint_file}")
    text = sprint_file.read_text(encoding="utf-8")
    # Strip frontmatter — between leading `---` lines — but KEEP the title field
    # value so it contributes to keywords.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            frontmatter = text[3:end]
            body = text[end + 4:]
            title_match = re.search(r"^title:\s*(.+)$", frontmatter, re.MULTILINE)
            title_text = title_match.group(1) if title_match else ""
            return title_text + "\n" + body
    return text


def extract_keywords(text: str) -> set[str]:
    """Extract domain keywords + key phrases from brief text."""
    lower = text.lower()
    found: set[str] = set()
    # Key phrases first (multi-word matches).
    for phrase in KEY_PHRASES:
        if phrase in lower:
            found.add(phrase)
    # Single-word tokens (alphanumeric + hyphen/underscore), length >= 3,
    # not in stopwords.
    for tok in re.findall(r"[a-z][a-z0-9_\-]{2,}", lower):
        if tok in STOPWORDS:
            continue
        # Skip pure numeric IDs (VT-104, CL-56 etc — those are references not
        # domains).
        if re.fullmatch(r"vt-?\d+", tok) or re.fullmatch(r"cl-?\d+", tok):
            continue
        found.add(tok)
    return found


def parse_ledger_index(ledger_file: Path) -> list[dict]:
    """Parse ledger-index.md into structured rows.

    Returns a list of dicts: {cl_id, date, tags (set), one_line, status, line}
    """
    if not ledger_file.exists():
        sys.exit(f"ERROR: ledger-index not found: {ledger_file}")
    text = ledger_file.read_text(encoding="utf-8")
    rows: list[dict] = []
    # Match table rows of the form: | CL-N | date | tag1, tag2, ... | one-line | status |
    # Allow optional bold markers (** ... **) around any cell.
    row_re = re.compile(
        r"^\|\s*\**\s*(CL-\d+|DR-\d+)\s*\**\s*"
        r"\|\s*\**\s*([^|]+?)\s*\**\s*"
        r"\|\s*\**\s*([^|]+?)\s*\**\s*"
        r"\|\s*\**\s*([^|]+?)\s*\**\s*"
        r"\|\s*\**\s*([^|]+?)\s*\**\s*\|"
        r"\s*$",
        re.MULTILINE,
    )
    for m in row_re.finditer(text):
        cl_id, date, tag_csv, one_line, status = m.groups()
        tags = {t.strip() for t in tag_csv.split(",") if t.strip()}
        rows.append({
            "cl_id": cl_id,
            "date": date,
            "tags": tags,
            "one_line": one_line,
            "status": status,
            "raw_line": m.group(0),
        })
    if not rows:
        sys.exit(f"ERROR: no rows parsed from {ledger_file}. Format drift?")
    return rows


def find_matches(brief_keywords: set[str], ledger_rows: list[dict]) -> list[dict]:
    """Return ledger rows whose tags overlap with brief keywords."""
    matches: list[dict] = []
    for row in ledger_rows:
        overlap = row["tags"] & brief_keywords
        if overlap:
            row_with_overlap = dict(row)
            row_with_overlap["matched_tags"] = overlap
            matches.append(row_with_overlap)
    return matches


def print_report(sprint_file: Path, brief_keywords: set[str], matches: list[dict], strict: bool) -> int:
    """Print the report and return exit code."""
    print(f"=== Ledger check for {sprint_file.name} ===\n")
    print(f"Brief keywords extracted: {len(brief_keywords)} terms")
    if not matches:
        print("\nNo Standing decisions match this brief's domain tags.")
        print("If this seems wrong, expand the brief's domain coverage or add KEY_PHRASES.")
        return 0
    print(f"\n{len(matches)} Standing decision(s) touch this brief's domain:\n")
    # Sort: LOCKED / DR / bold-status first, then by date desc.
    def sort_key(r):
        status_lower = r["status"].lower()
        priority = 0
        if "locked" in status_lower or r["cl_id"].startswith("DR"):
            priority = -2
        elif "standing" in status_lower:
            priority = -1
        return (priority, r["date"])
    for row in sorted(matches, key=sort_key, reverse=True):
        marker = "[!]" if "locked" in row["status"].lower() or row["cl_id"].startswith("DR") else "[*]"
        print(f"  {marker} {row['cl_id']}  ({row['date']})  status={row['status']}")
        print(f"        matched tags: {', '.join(sorted(row['matched_tags']))}")
        print(f"        {row['one_line']}")
        print()
    print("Action required:")
    print("  1. Review each surfaced decision against the brief.")
    print("  2. If the brief CONTRADICTS or PRE-DATES a Standing decision, fix the brief FIRST.")
    print("  3. Add `cl_decisions_checked: [CL-N, CL-M, ...]` to the brief-ready signal.")
    print("  4. Note in the sprint file's 'Brief artifacts' section if any reconciliation was needed.")
    if strict:
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("sprint_file", help="Path to .viabe/sprint/VT-<N>.md")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit code 1 if any matches found (forces explicit reconciliation).",
    )
    args = p.parse_args()
    sprint_path = Path(args.sprint_file).resolve()
    text = extract_brief_text(sprint_path)
    brief_keywords = extract_keywords(text)
    ledger_rows = parse_ledger_index(LEDGER_INDEX)
    matches = find_matches(brief_keywords, ledger_rows)
    return print_report(sprint_path, brief_keywords, matches, args.strict)


if __name__ == "__main__":
    sys.exit(main())
