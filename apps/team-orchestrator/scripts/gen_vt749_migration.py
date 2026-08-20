"""Generate migration 208 from the builder output — the DB half of VT-749 scope 1.

Serving reads the DATABASE, not the artifacts: `card_serving._CARD_SQL` picks the highest-version
`knowledge_corpus_versions` row with status='shadow' AND admission_verdict='pending' and joins its
members. And `knowledge_cards` rows are IMMUTABLE (migration 182/189 trigger blocks UPDATE). So the
landing is a NEW corpus version whose 63 rescoped cards are NEW immutable versions superseding the old
ones — never an UPDATE, and never a hand-edited row.

Generated, not hand-typed: the scope values come from the same delta the plan builder consumes, so the
artifact and the database cannot disagree about what was decided.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from uuid import NAMESPACE_URL, uuid5

ROOT = pathlib.Path("/Users/fazalkhan/development/viabe-team")
CORPUS = ROOT / "apps" / "team-orchestrator" / "knowledge_corpus"
OUT = ROOT / "migrations" / "208_vt749_scoped_corpus_v4.sql"

# The v3 corpus version serving picks today, read from dev before generating.
PARENT_CORPUS_ID = "fc6ed0b5-f138-59a0-92c3-ec6e4cded7cf"
PARENT_VERSION = 3
NEW_VERSION = 4

rows = [
    json.loads(line)
    for line in (CORPUS / "vt749_applicability_scoping.jsonl").read_text().splitlines()
    if line
]
assert len(rows) == 63, len(rows)

DIMS = ("jurisdictions", "size_bands", "industries", "maturity_stages", "channels")


def pg_array(values) -> str:
    if not values:
        return "'{}'::text[]"
    inner = ",".join(f'"{v}"' for v in values)
    return f"'{{{inner}}}'::text[]"


patch_values = []
for row in sorted(rows, key=lambda r: r["card_version_id"]):
    old_id = row["card_version_id"]
    new_id = str(uuid5(NAMESPACE_URL, f"viabe:o8:vt749:scoped:{old_id}"))
    patch = row["applicability_patch"]
    dims = ", ".join(pg_array(patch.get(d, [])) for d in DIMS)
    universal = "true" if patch.get("universal", False) else "false"
    # The label goes ABOVE the tuple: a trailing `--` comment swallows the comma the join appends,
    # which is a syntax error 63 rows long.
    patch_values.append(
        f"    -- {row['class']}: {row['subject']}\n"
        f"    ('{old_id}'::uuid, '{new_id}'::uuid, {dims}, {universal})"
    )

digest_material = json.dumps(
    {
        "parent": PARENT_CORPUS_ID,
        "vt749": sorted(
            (r["card_version_id"], r["class"], r["applicability_patch"]) for r in rows
        ),
    },
    sort_keys=True,
    separators=(",", ":"),
)
content_digest = hashlib.sha256(digest_material.encode()).hexdigest()
new_corpus_id = str(uuid5(NAMESPACE_URL, f"viabe:o8:vt749:corpus:{content_digest}"))

sql = f"""-- 208_vt749_scoped_corpus_v4.sql — VT-749 scope 1, the DATABASE half.
--
-- GENERATED from knowledge_corpus/vt749_applicability_scoping.jsonl by
-- apps/team-orchestrator/scripts/gen_vt749_migration.py. Do not hand-edit: the scope values here and the values the plan builder
-- applies come from the same delta, which is the only reason the artifact and the database cannot
-- disagree about what was decided.
--
-- ## THE TRAP THIS MIGRATION AVOIDS (measured on dev before landing)
--
-- The delta names each card by `card_version_id`, and all 63 of those ids DO exist in
-- `knowledge_cards`. They are NOT the rows serving reads: **0 of them are members of the served v3
-- corpus** (only 7 are members of v2). The served v3 corpus has its own 63 eligible-and-unscoped
-- members, and the two sets are the same 63 LOGICAL cards under different persisted version rows —
-- `card_key` intersects at exactly 63.
--
-- So keying the landing on the delta's version ids would have created 63 new versions of rows nobody
-- serves, reported "63 cards scoped", and left the served corpus matching every tenant exactly as
-- before. This migration therefore resolves each target as **the v3 member sharing that card_key**,
-- and asserts the resolution is 1:1 before writing anything.
--
-- ## Why a new corpus version and not an UPDATE
--
-- `card_serving._CARD_SQL` serves the HIGHEST-version `knowledge_corpus_versions` row with
-- status='shadow' AND admission_verdict='pending', joined to its members. And `knowledge_cards` rows
-- are immutable — migration 182/189's trigger raises on UPDATE ("insert a new version instead").
-- So the honest landing is: 63 NEW card versions carrying the scopes, superseding the old ones, and a
-- v{NEW_VERSION} corpus that contains them plus the 55 unchanged members. Nothing is mutated; the old
-- v{PARENT_VERSION} corpus stays exactly as it was and remains reconstructible.
--
-- ## What VT-749 fixes, in one line
--
-- `card_retrieval._dimension_match` returns True for an EMPTY dimension without consulting the
-- context, and the hedge meant to restrain that is worth 0.083 against a 0.250 floor. 63 of the 100
-- retrieval-eligible cards declared NO jurisdiction, size band, industry, maturity stage or channel
-- and did not declare `universal` — so they matched every tenant in every context while looking
-- cautious. After this migration: zero such cards, and 42 that declare `universal=true` deliberately.
--
-- Classes (Clau 2026-08-17, audited against the card bodies by CC before landing):
--   U 42 universal judgment-process · ST 11 size_bands small,medium · OP 6 channels online_presence
--   B2B 2 industries · SUB 1 industries · SCALE 1 size+maturity
--
-- Embeddings are COPIED to the new ids: only applicability changed, the claim text is byte-identical,
-- so the vector is identical by construction. Re-embedding would spend Voyage calls to reproduce the
-- same numbers, and leaving them absent would silently push 63 cards onto the live-embed path.
--
-- ## NO explicit BEGIN/COMMIT, and IDEMPOTENT — both learned the hard way here
--
-- `apply_migrations` already wraps each file in `conn.transaction()`. An earlier draft opened its own
-- BEGIN/COMMIT, and that COMMIT ended the RUNNER's transaction: a dry-run probe that raised in order to
-- roll back had nothing left to undo and the write landed for real. A migration that manages its own
-- transaction takes away the runner's atomicity. So: no transaction control here, and every write is
-- guarded so re-running is a no-op rather than a duplicate-key failure.

CREATE TEMP TABLE vt749_scoping (
    old_id           UUID PRIMARY KEY,
    new_id           UUID NOT NULL UNIQUE,
    jurisdictions    TEXT[] NOT NULL,
    size_bands       TEXT[] NOT NULL,
    industries       TEXT[] NOT NULL,
    maturity_stages  TEXT[] NOT NULL,
    channels         TEXT[] NOT NULL,
    universal        BOOLEAN NOT NULL
) ON COMMIT DROP;

INSERT INTO vt749_scoping VALUES
{",\n".join(patch_values)}
;

-- Resolve each delta row to the row SERVING actually reads: the v{PARENT_VERSION} member sharing the
-- delta card's `card_key`. Keyed on card_key because a scoping judgment is about the logical card;
-- the delta's version ids address different persisted rows (see the trap note above).
CREATE TEMP TABLE vt749_targets ON COMMIT DROP AS
SELECT s.old_id AS delta_id, s.new_id, member_card.id AS target_id,
       s.jurisdictions, s.size_bands, s.industries, s.maturity_stages, s.channels, s.universal
  FROM vt749_scoping s
  JOIN public.knowledge_cards delta_card ON delta_card.id = s.old_id
  JOIN public.knowledge_cards member_card ON member_card.card_key = delta_card.card_key
  JOIN public.knowledge_corpus_members m
       ON m.card_id = member_card.id AND m.corpus_version_id = '{PARENT_CORPUS_ID}'::uuid;

-- Fail closed if the corpus is not what this migration was generated against. A silent partial match
-- would scope some cards and leave others matching everything, which is worse than not running.
DO $$
DECLARE
    resolved INT;
    dupes INT;
    scoped_already INT;
BEGIN
    SELECT count(*) INTO resolved FROM vt749_targets;
    IF resolved <> 63 THEN
        RAISE EXCEPTION 'VT-749: resolved % of 63 targets in the served corpus — regenerate against this database', resolved;
    END IF;

    SELECT count(*) INTO dupes FROM (
        SELECT target_id FROM vt749_targets GROUP BY 1 HAVING count(*) > 1
    ) d;
    IF dupes > 0 THEN
        RAISE EXCEPTION 'VT-749: % card_key(s) resolved to more than one served row — the join is not 1:1', dupes;
    END IF;

    SELECT count(*) INTO scoped_already
      FROM vt749_targets s JOIN public.knowledge_cards c ON c.id = s.target_id
     WHERE c.applicability_universal
        OR coalesce(array_length(c.jurisdictions, 1), 0) > 0
        OR coalesce(array_length(c.size_bands, 1), 0) > 0
        OR coalesce(array_length(c.industries, 1), 0) > 0
        OR coalesce(array_length(c.maturity_stages, 1), 0) > 0
        OR coalesce(array_length(c.channels, 1), 0) > 0;
    IF scoped_already > 0 THEN
        RAISE EXCEPTION 'VT-749: % target card(s) already carry a scope — refusing to supersede a decision this delta was not reviewed against', scoped_already;
    END IF;
END $$;

-- 1. The v{NEW_VERSION} corpus version FIRST: knowledge_cards.corpus_version_id is an FK to it,
--    so inserting the cards first fails the constraint. shadow/pending like its parent — VT-749 changes what the
--    corpus SAYS, not its admission state; graduation stays a separate, Fazal-gated decision.
INSERT INTO public.knowledge_corpus_versions
    (id, version, parent_corpus_version_id, content_digest, status, admission_verdict, created_by)
VALUES (
    '{new_corpus_id}'::uuid, {NEW_VERSION}, '{PARENT_CORPUS_ID}'::uuid,
    '{content_digest}', 'shadow', 'pending', 'vt749:scope1'
)
ON CONFLICT (id) DO NOTHING;

-- 2. The 63 new immutable versions. INSERT … SELECT from the old row so every content column is
--    carried across verbatim; only the scope columns, the id, the version and supersedes change.
INSERT INTO public.knowledge_cards (
    id, card_key, version, claim, claim_key, claim_value, distillation_note,
    jurisdictions, size_bands, industries, maturity_stages, channels, applicability_universal,
    effective_from, effective_until, authority, confidence, scope, status, retention_class,
    tainted, expires_at, supersedes_card_id, default_assignment, domain, source_class,
    usage_rights, independence_cluster, corroboration_cluster_count, provenance,
    retrieval_eligible, corpus_version_id
)
SELECT
    s.new_id, c.card_key,
    -- NOT c.version + 1: some of these card_keys ALREADY have a version 2 row (a later version
    -- exists outside the v3 corpus), and (card_key, version) is unique. The next version for the
    -- LOGICAL card is max(version) over its key — using the member's own version collided on 1 key.
    (SELECT max(k.version) FROM public.knowledge_cards k WHERE k.card_key = c.card_key) + 1,
    c.claim, c.claim_key, c.claim_value, c.distillation_note,
    s.jurisdictions, s.size_bands, s.industries, s.maturity_stages, s.channels, s.universal,
    c.effective_from, c.effective_until, c.authority, c.confidence, c.scope, c.status,
    c.retention_class, c.tainted, c.expires_at, c.id, c.default_assignment, c.domain, c.source_class,
    c.usage_rights, c.independence_cluster, c.corroboration_cluster_count, c.provenance,
    c.retrieval_eligible, '{new_corpus_id}'::uuid
FROM vt749_targets s
JOIN public.knowledge_cards c ON c.id = s.target_id
ON CONFLICT (id) DO NOTHING;

-- 3. Members: every v{PARENT_VERSION} member that was NOT rescoped, plus the 63 new versions.
INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id)
SELECT '{new_corpus_id}'::uuid, m.card_id
  FROM public.knowledge_corpus_members m
 WHERE m.corpus_version_id = '{PARENT_CORPUS_ID}'::uuid
   AND m.card_id NOT IN (SELECT target_id FROM vt749_targets)
   AND NOT EXISTS (
       SELECT 1 FROM public.knowledge_corpus_members existing
        WHERE existing.corpus_version_id = '{new_corpus_id}'::uuid
          AND existing.card_id = m.card_id
   );

INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id)
SELECT '{new_corpus_id}'::uuid, s.new_id FROM vt749_targets s
 WHERE NOT EXISTS (
     SELECT 1 FROM public.knowledge_corpus_members existing
      WHERE existing.corpus_version_id = '{new_corpus_id}'::uuid AND existing.card_id = s.new_id
 );

-- 4. Embeddings carried across (identical claim text ⇒ identical vector).
INSERT INTO public.knowledge_card_embeddings
    (card_id, embedding, embedding_model, embedding_dimensions, content_digest)
SELECT s.new_id, e.embedding, e.embedding_model, e.embedding_dimensions, e.content_digest
  FROM vt749_targets s
  JOIN public.knowledge_card_embeddings e ON e.card_id = s.target_id
ON CONFLICT DO NOTHING;

-- 5. Assert the landing, inside the transaction, so a wrong shape rolls back instead of serving.
DO $$
DECLARE
    members INT;
    eligible INT;
    unscoped INT;
    universal INT;
BEGIN
    SELECT count(*) INTO members FROM public.knowledge_corpus_members
     WHERE corpus_version_id = '{new_corpus_id}'::uuid;
    IF members <> 118 THEN
        RAISE EXCEPTION 'VT-749: v{NEW_VERSION} has % members, expected 118', members;
    END IF;

    SELECT count(*) INTO eligible
      FROM public.knowledge_corpus_members m JOIN public.knowledge_cards c ON c.id = m.card_id
     WHERE m.corpus_version_id = '{new_corpus_id}'::uuid AND c.retrieval_eligible;
    IF eligible <> 100 THEN
        RAISE EXCEPTION 'VT-749: v{NEW_VERSION} has % eligible cards, expected 100', eligible;
    END IF;

    SELECT count(*) INTO unscoped
      FROM public.knowledge_corpus_members m JOIN public.knowledge_cards c ON c.id = m.card_id
     WHERE m.corpus_version_id = '{new_corpus_id}'::uuid
       AND c.retrieval_eligible
       AND NOT c.applicability_universal
       AND coalesce(array_length(c.jurisdictions, 1), 0) = 0
       AND coalesce(array_length(c.size_bands, 1), 0) = 0
       AND coalesce(array_length(c.industries, 1), 0) = 0
       AND coalesce(array_length(c.maturity_stages, 1), 0) = 0
       AND coalesce(array_length(c.channels, 1), 0) = 0;
    IF unscoped <> 0 THEN
        RAISE EXCEPTION 'VT-749: v{NEW_VERSION} still has % eligible card(s) that scope nothing', unscoped;
    END IF;

    SELECT count(*) INTO universal
      FROM public.knowledge_corpus_members m JOIN public.knowledge_cards c ON c.id = m.card_id
     WHERE m.corpus_version_id = '{new_corpus_id}'::uuid
       AND c.retrieval_eligible AND c.applicability_universal;
    IF universal <> 42 THEN
        RAISE EXCEPTION 'VT-749: v{NEW_VERSION} has % universal eligible cards, expected 42', universal;
    END IF;
END $$;
"""

OUT.write_text(sql)
print(f"wrote {OUT} ({len(sql)} bytes)")
print("new corpus id:", new_corpus_id)
print("content digest:", content_digest[:16])
