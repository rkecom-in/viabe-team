"""VT-313 — L4 skill-corpus seed canary.

Two layers:
  * NO-DEP parse + taxonomy gate (always runs, no DB / no Voyage): every
    ``skill_corpus/NNN-slug.md`` parses via ``l4_corpus.parse_doc``, carries
    ``authored_by: fazal`` + a non-empty body + the seed-prior header, draws
    tags ONLY from the locked taxonomy (no sprawl), and the GATED notes carry
    the gated header + ``gated`` tag. Asserts the kept-note count.
  * DB + VOYAGE-gated live seed+retrieve (skipif no DATABASE_URL / VOYAGE_API_KEY):
    seed the real corpus into a throwaway DB via ``seed_l4_corpus``, then run an
    L4 query for a relevant phrase and assert a real seeded note comes back.
    Mirrors ``test_l4_corpus.py`` for the seed/query API + pool wiring.

The seed-prior header records that Fazal's magnitudes are priors to validate
against real attribution, not ground truth. The gated header records that a
gated lever's enforcement is the structural guardrail, not the prose.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


def _parse_doc(path: Path) -> dict:
    """Dep-less mirror of ``l4_corpus.parse_doc`` (yaml only). Importing
    ``l4_corpus`` pulls voyageai + langgraph (via embeddings + graph), which the
    dep-less CI ``test`` job lacks — so this always-on parse/taxonomy gate parses
    the frontmatter inline instead. The REAL ``parse_doc``/``seed_l4_corpus`` are
    imported inside the DB+VOYAGE-gated live test below."""
    text = path.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path.name}: missing YAML frontmatter")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    if not meta.get("title") or not meta.get("authored_by"):
        raise ValueError(f"{path.name}: frontmatter needs title + authored_by")
    body = body.strip()
    if not body:
        raise ValueError(f"{path.name}: empty body")
    return {
        "title": str(meta["title"]),
        "authored_by": str(meta["authored_by"]),
        "tags": list(meta.get("tags") or []),
        "applies_to_business_types": meta.get("applies_to_business_types"),
        "priority": int(meta.get("priority", 3)),
        "body": body,
    }

# --- locked taxonomy (must mirror the brief; no new terms) -------------------

LOCKED_TAGS = {
    "timing", "festivals", "whatsapp", "sms", "email", "local", "in_person",
    "retention", "churn", "win_back", "referral", "reviews", "pricing",
    "discount", "bundling", "loyalty", "cadence", "onboarding", "complaint",
    "gated",
}

# Restricting key only present on tag-scoped notes; these are the only valid values.
VALID_BUSINESS_TYPES = {
    "gym", "salon_membership", "subscription_box", "services",
    "multi_tier_retail", "online_seller",
}

SEED_PRIOR_LINE = (
    "*Seed prior — validate against this tenant's real attribution before "
    "treating as fact.*"
)
GATED_HEADER_FRAGMENT = "Gated lever — the agent never executes this autonomously"
# #20 carries a bespoke gated header (the complaint freeze).
GATED_20_HEADER_FRAGMENT = "Gated (NON-configurable #20)"

# Number of KEEP notes per the VT-313 disposition (~69; 13 dropped).
EXPECTED_KEEP_COUNT = 69

CORPUS_DIR = Path(__file__).resolve().parents[3] / "skill_corpus"


def _corpus_files() -> list[Path]:
    # README.md is documentation, not a corpus doc — exclude it.
    return sorted(p for p in CORPUS_DIR.glob("*.md") if p.name != "README.md")


def test_corpus_dir_exists_and_is_populated():
    assert CORPUS_DIR.is_dir(), f"missing corpus dir: {CORPUS_DIR}"
    files = _corpus_files()
    # Hard floor from the brief (>= 60); also assert the exact kept count.
    assert len(files) >= 60, f"too few corpus docs: {len(files)}"
    assert len(files) == EXPECTED_KEEP_COUNT, (
        f"expected {EXPECTED_KEEP_COUNT} kept notes, found {len(files)}"
    )


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.name)
def test_each_doc_parses_and_is_well_formed(path: Path):
    doc = _parse_doc(path)  # raises on missing title / authored_by / empty body

    # authored_by — Fazal's verbatim content.
    assert doc["authored_by"] == "fazal", path.name
    assert doc["title"].strip(), path.name
    assert doc["body"].strip(), path.name

    # Seed-prior header present in EVERY body.
    assert SEED_PRIOR_LINE in doc["body"], f"{path.name}: missing seed-prior header"

    # Tags from the locked taxonomy ONLY (no sprawl), 1-4 of them.
    tags = doc["tags"]
    assert tags, f"{path.name}: no tags"
    assert 1 <= len(tags) <= 4, f"{path.name}: {len(tags)} tags (want 1-4)"
    stray = set(tags) - LOCKED_TAGS
    assert not stray, f"{path.name}: tags outside locked taxonomy: {stray}"

    # priority in [1,5].
    assert 1 <= doc["priority"] <= 5, path.name

    # applies_to_business_types only valid values when present; universal notes omit it.
    bts = doc["applies_to_business_types"]
    if bts is not None:
        assert isinstance(bts, list) and bts, path.name
        bad = set(bts) - VALID_BUSINESS_TYPES
        assert not bad, f"{path.name}: invalid business types: {bad}"


def test_gated_notes_carry_gated_header_and_tag():
    """The gated levers (#20,21,35,38,45,46,47,48,50,53,55) must carry the gated
    tag AND a gated header in the body — the structural enforcement reminder."""
    expected_gated_numbers = {20, 21, 35, 38, 45, 46, 47, 48, 50, 53, 55}
    found_gated_numbers: set[int] = set()

    for path in _corpus_files():
        num = int(path.name.split("-", 1)[0])
        doc = _parse_doc(path)
        is_gated_tag = "gated" in doc["tags"]
        has_gated_header = (
            GATED_HEADER_FRAGMENT in doc["body"]
            or GATED_20_HEADER_FRAGMENT in doc["body"]
        )
        # gated tag <=> gated header (they travel together).
        assert is_gated_tag == has_gated_header, (
            f"{path.name}: gated tag/header mismatch (tag={is_gated_tag}, "
            f"header={has_gated_header})"
        )
        if is_gated_tag:
            found_gated_numbers.add(num)

    assert found_gated_numbers == expected_gated_numbers, (
        f"gated set mismatch: extra={found_gated_numbers - expected_gated_numbers}, "
        f"missing={expected_gated_numbers - found_gated_numbers}"
    )


def test_complaint_freeze_has_bespoke_header():
    """#20 (NON-configurable complaint freeze) carries its bespoke VT-321 header
    and the complaint + gated tags."""
    path = next(p for p in _corpus_files() if p.name.startswith("020-"))
    doc = _parse_doc(path)
    assert GATED_20_HEADER_FRAGMENT in doc["body"]
    assert "VT-321 complaint-freeze exclusion" in doc["body"]
    assert "complaint" in doc["tags"]
    assert "gated" in doc["tags"]


# --- DB + VOYAGE-gated live seed + retrieve ----------------------------------

pytestmark_live = pytest.mark.skipif(
    not (
        os.environ.get("DATABASE_URL")
        and os.environ.get("VOYAGE_API_KEY")
        and os.environ.get("GITHUB_ACTIONS")
    ),
    reason="live seed+retrieve embeds the full corpus — gated to CI (GITHUB_ACTIONS) "
    "where VOYAGE_API_KEY is the usage-tier-1 secret (VT-314). Skipped in the local "
    "pre-push: the dev voyage.env key is free-tier (3 RPM / 10K TPM) and the "
    "full-corpus seed tips the shared per-minute budget alongside the other voyage "
    "live tests. The dep-less parse/taxonomy gate (always-on) covers the corpus locally.",
)


@pytest.fixture(scope="module")
def live_pool():
    import apply_migrations

    dsn = os.environ["DATABASE_URL"]
    r = apply_migrations.apply(dsn=dsn)
    assert not r["failed"], r["failed"]
    os.environ["TEAM_SUPABASE_DB_URL"] = dsn

    from orchestrator import graph as graph_mod

    if graph_mod._pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        graph_mod._pool = ConnectionPool(
            dsn, min_size=1, max_size=4,
            kwargs={"autocommit": True, "row_factory": dict_row}, open=True,
        )
    return graph_mod.get_pool()


@pytestmark_live
def test_live_seed_and_retrieve(live_pool):
    """Seed the REAL corpus (real Voyage embeddings) into a throwaway DB, then
    retrieve for a relevant phrase and assert a real seeded note comes back."""
    from orchestrator.knowledge.l4_corpus import seed_l4_corpus
    from orchestrator.knowledge.l4_query import retrieve_documents

    result = seed_l4_corpus(CORPUS_DIR)
    assert result["seeded"] == EXPECTED_KEEP_COUNT

    docs = retrieve_documents("festival greeting timing", top_k=5)
    assert docs, "retrieval returned nothing from a seeded corpus"
    titles = {d.title for d in docs}
    # The festival-timing note (#001) must surface for this query.
    assert any("Festival greetings ship" in t for t in titles), (
        f"festival-timing note not retrieved; got: {sorted(titles)}"
    )
    # Every retrieved doc is Fazal-authored corpus content.
    assert all(d.authored_by == "fazal" for d in docs)
