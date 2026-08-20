-- 208_vt749_scoped_corpus_v4.sql — VT-749 scope 1, the DATABASE half.
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
-- v4 corpus that contains them plus the 55 unchanged members. Nothing is mutated; the old
-- v3 corpus stays exactly as it was and remains reconstructible.
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
    -- U: service_quality
    ('07c436a5-7202-5048-842c-1ac2e31283c9'::uuid, 'bf912779-9265-51b0-a5a6-a7d7e7aaa86f'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- ST: commercial_excellence
    ('0982be07-ab6c-5d2e-a2e7-5e50adc8f6af'::uuid, '31c8476e-e464-512c-a3c6-65c68146556b'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- ST: sales_coaching
    ('0a42f797-3928-5c74-a8db-5458d5f94cda'::uuid, 'a434787c-7762-56c9-86b8-0fb921e102fe'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: change_strategy
    ('0aec1614-1b38-5c8d-aebb-d05298726752'::uuid, 'd2616f65-d39b-5653-8182-5a8cdea3f849'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- OP: digital_advertising
    ('10da460f-00c1-53de-b2d4-7fa96220bfa4'::uuid, 'ffa779bb-29f9-55bf-96eb-57eed49bbfa7'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{"online_presence"}'::text[], false),
    -- ST: decision_process
    ('1c109a35-a71d-5337-9ad6-1b25bac9adb0'::uuid, '0421377d-6a1c-5eac-822e-93c6cafa3256'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- OP: promotions
    ('207e1bc0-6793-5894-a541-ae2cb389bead'::uuid, 'dd328b2f-dd73-5625-bafc-d8daf560113b'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{"online_presence"}'::text[], false),
    -- OP: online_reviews
    ('28a88fbb-df4a-5dd9-86c5-c13860c0a877'::uuid, 'e7771a48-42c0-5345-90c8-1f4ec983fa8f'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{"online_presence"}'::text[], false),
    -- U: information_overload
    ('28ae1b2f-074e-585f-982a-b8b85456bc14'::uuid, 'de0ed362-bfe0-565d-a540-6fcc1b47c599'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- ST: sales_quota
    ('2c30a5fc-56de-5b11-b7aa-1a26df8d375f'::uuid, 'f020a7b4-e131-5634-b72a-1b6bf5ca7c8c'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: owner_economics
    ('2cc17507-1a6a-5d90-a00f-2de57cec08b5'::uuid, '16a637e1-217c-52b8-a8c7-a37296f67243'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: tradeoff_transparency
    ('2f5e69c1-9a80-54dc-b1be-809630345127'::uuid, '41deb058-37e8-52a2-a651-692577b5542d'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- ST: employee_referrals
    ('2faba0e1-7412-5743-9591-15ce82d5eb3c'::uuid, '428d7229-8935-5f6e-bfef-5bcdd31c36b0'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: a3
    ('334cc596-e0db-52cc-b84c-65e580ccb21a'::uuid, '20049309-2df4-56b6-954c-2da8544eac20'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: change_management
    ('390f91f2-96ab-5b1e-9ab7-cb20f6b1108f'::uuid, 'e73f9905-a5fd-5c30-8a4f-cc5cfd85786a'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: proxies
    ('3caedb8b-ef4a-5741-8b5c-2cd6cbe1ac50'::uuid, 'd958e7c5-2416-5d3b-9ffe-a5793b9e2066'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- OP: ab_testing
    ('433b9c1b-9dd4-52e6-9d41-71d198b4ecf7'::uuid, 'd6be35f8-c37c-5bcc-bf0e-3806f8ca2578'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{"online_presence"}'::text[], false),
    -- ST: transformation
    ('4dbf0a35-adad-598a-958f-a8f9720459b4'::uuid, '4ab43ad8-b5d1-561e-83cc-096c86f3414e'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: negotiation
    ('56a09b0a-a3cf-5a3b-baec-73c597c1bc12'::uuid, '22e9c79e-5ec1-566a-bbf2-e80cd5ee88d0'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- ST: hybrid_work
    ('5d34df0a-3005-5dfc-b28a-feb26a740581'::uuid, '0a0b974a-2d90-55c0-92af-f32ca945fcea'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: decision_triage
    ('5ef7a3ed-648b-569d-9da8-0526cf622793'::uuid, '353fb9c7-9261-5245-8f17-5fa2755136fb'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: referrals
    ('61ad7ba7-d575-5fb6-85e9-564a8dac6ccf'::uuid, 'b542df16-4ade-51f0-8375-7cc921b753f5'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: coo_role
    ('61b5406a-60a5-5dc6-8a60-ddfa5be9d1df'::uuid, 'e72744b1-39a8-5d16-9182-6de79dec632b'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: hiring
    ('64ec61a7-af99-569d-bcbb-b37f0b4b393c'::uuid, '36847e0b-db4e-51db-b0ec-6cb7968fc341'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- B2B: channel_sales
    ('663fbd0e-c295-5f4c-8ba1-b899d4324548'::uuid, '9fc69f8f-c65e-5ce1-b390-39100a7a13a7'::uuid, '{}'::text[], '{}'::text[], '{"wholesale_distribution","b2b_services"}'::text[], '{}'::text[], '{}'::text[], false),
    -- SUB: subscription
    ('68d3f72a-a86d-545a-820c-32db41446d2e'::uuid, '2364b720-c4b1-5edd-8ea6-03160904f907'::uuid, '{}'::text[], '{}'::text[], '{"subscription_services"}'::text[], '{}'::text[], '{}'::text[], false),
    -- SCALE: scale
    ('691e9cea-b41d-50f6-a116-ce69ce6cf04a'::uuid, 'd796f6b2-8031-5275-9390-fb3b65d30f2f'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{"scaling"}'::text[], '{}'::text[], false),
    -- U: digital_merchandising
    ('73da0322-1a6d-57f4-8bcc-8ae0d9256d16'::uuid, 'e588d8cb-65d0-599e-9422-54ca9c69337a'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: operating_cadence
    ('7555d22b-ca8b-5c48-a181-84280726c1c8'::uuid, 'ad0f486d-8d94-5339-9231-32bfac3bf788'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: disagree_and_commit
    ('78ca6e4e-7057-529c-b2ea-e16051940b56'::uuid, '1611d57f-8aff-5503-9927-cb17af7d5cf5'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: cynefin
    ('7e482e4f-1f37-5732-96f4-2b14ac672d52'::uuid, '22ce476f-ba0f-5791-9dd8-ea55720d94c1'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: sales_methods
    ('81cd2bfc-ea39-56b1-8eee-92b766182367'::uuid, 'e026cc4a-b889-56b5-880e-fd03881f1cb0'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- B2B: pricing
    ('82c61a03-6cbe-5727-9377-1e18d5d01fe7'::uuid, '55a7b09b-b120-5359-bfa5-9bad74287d6e'::uuid, '{}'::text[], '{}'::text[], '{"wholesale_distribution","b2b_services"}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: incentives
    ('902526be-2bfb-5967-9ed3-d6dd6a009c98'::uuid, '2087acd4-b892-5fea-ba91-c20b6fca68f1'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- ST: manager_allocation
    ('92091f67-fff1-5265-b0cb-df3066b75cfc'::uuid, '5eecea46-8f4e-5be7-80f5-6c258c576b54'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: resource_allocation
    ('967dc393-76c4-5dc7-9695-de9a34e99d7d'::uuid, 'f2cb4a8d-05aa-5490-9c83-b3f8f3ecd4cc'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: product
    ('9cb6c5c8-bfd0-5865-838e-9d31b6d9298b'::uuid, '87a94337-165c-5521-831e-e8de71a20461'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: jidoka
    ('9d1ef1a1-ffa8-5986-b88e-e9c53e27871d'::uuid, 'd696b116-8f1e-5861-8190-e2d91032a048'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: migration
    ('9d73c61a-e7c0-5bba-b307-37d92b6a6a0c'::uuid, '13852b6b-258e-574d-a47b-374bd035f968'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: decision_quality
    ('9e4359f8-b059-5f69-8984-c4fad3f77821'::uuid, 'a0690f24-18ca-5635-8363-36c4395a399f'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: business_case
    ('a132d2a3-d035-5baf-9722-f5cf2494a19a'::uuid, 'e242ae62-7430-5bff-8e87-bba9d994e2cd'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: portfolio_management
    ('b10aa017-d53b-5f0f-a48d-d33f7d56ca80'::uuid, '40e83bec-0a62-5166-a6c4-dff958b3a7fc'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- OP: pricing
    ('b5c522cc-3414-55ae-86fd-5c280e2fd875'::uuid, '5b769811-1d3c-5323-9571-957557fdef23'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{"online_presence"}'::text[], false),
    -- ST: performance_management
    ('b7029cea-8730-5f0e-ac5f-95e4d87a4330'::uuid, '7490a034-e027-5ced-859c-ba1ee941ed22'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: business_benefit
    ('b786392b-0338-50fc-8f73-782df4ed6b24'::uuid, '3d9d1abd-95df-5337-a87b-094e53ce9412'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: reversible_decisions
    ('bbe7e4a1-f8c5-579b-a23c-48a0c42e6d3e'::uuid, '8541895a-9144-5d2a-a5a9-a4c6a7216d27'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: lending
    ('bbf3ecb1-e389-57b2-8265-d7dca70c98cc'::uuid, '23e88960-cafa-55c4-bf11-5b0d58a02933'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: customer_feedback
    ('c08e15cc-83f4-5c75-9fe8-1af5f9b3e5eb'::uuid, '8fd5f2ec-bee9-56a5-9b58-f05d2e829047'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: specialist_arbitration
    ('c7e91f9c-41de-5021-b887-7da4540e1dad'::uuid, '2a20cb0e-3d52-59e7-9459-f022edde6cd8'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- ST: sales_compensation
    ('c8b1618b-0e18-5828-a0a8-3b917f34e8a1'::uuid, '4ea2027a-a521-52f4-81e0-a2f1cd51e5b1'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: payments
    ('cfa2f2fc-bf05-5221-8a36-2e176b1ada65'::uuid, '9f39b82f-e807-5b21-9e67-cd855e30ffcf'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: operational_transparency
    ('d1e81f83-9860-5e0e-871d-2af18303e9a6'::uuid, 'b1381610-fc43-532e-9df0-eff016ac1b34'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: operating_model
    ('d6affc5e-f9f2-508e-9398-7b6cf8a7eedd'::uuid, '33e9cda3-dda2-5b9e-8a0b-c04e5921cbe0'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- ST: organizational_health
    ('d6bc4fa2-2276-574b-a2b0-72da30e83172'::uuid, '2467b3de-35c5-55c9-ba16-3faacc685eae'::uuid, '{}'::text[], '{"small","medium"}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], false),
    -- U: decision_analysis
    ('d7be3bcc-8c24-5f9a-a38e-2cf417714e58'::uuid, '70998ae4-1f52-5545-8806-03c0256f57b9'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: rapid
    ('db50e8e6-5984-5f06-a305-c55df1d356b7'::uuid, 'f843d00b-f370-5072-9b60-d3f06b443955'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: strategic_foresight
    ('e545fda4-0993-5ac0-8b68-1383e6e09a32'::uuid, 'fe197b10-49f4-5522-9f8a-353401e440d4'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: generative_ai
    ('e7e5bbf7-53dc-5361-9dd1-c50951af4c72'::uuid, '8c036f84-7bf4-587d-96ee-197068798504'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: robust_decision_making
    ('e97daabe-1ac6-5e7a-b116-ead68682640b'::uuid, '151020ea-71ec-54da-bf81-baa1a4c696e8'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: growth
    ('ecdef308-7c3c-58f8-a262-304640236d25'::uuid, '2a72bef3-8c2c-5a2c-9700-3520f7e2a8f0'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: trade_credit
    ('efac3ddd-d27d-5061-be3b-264ff962829f'::uuid, '38c656a9-9672-545c-b04d-406c7e5450fa'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- U: bias
    ('effb6de7-9094-5da2-a1de-d7112cb9a5ff'::uuid, 'b0190bad-9cad-5e2d-9ece-d119a5d2e905'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], true),
    -- OP: targeting
    ('f6d2c3e9-0c18-57d2-86e8-1f3f9f9d1584'::uuid, 'c34ef18b-62ef-5188-8e9e-cbc00d712029'::uuid, '{}'::text[], '{}'::text[], '{}'::text[], '{}'::text[], '{"online_presence"}'::text[], false)
;

-- Resolve each delta row to the row SERVING actually reads: the v3 member sharing the
-- delta card's `card_key`. Keyed on card_key because a scoping judgment is about the logical card;
-- the delta's version ids address different persisted rows (see the trap note above).
CREATE TEMP TABLE vt749_targets ON COMMIT DROP AS
SELECT s.old_id AS delta_id, s.new_id, member_card.id AS target_id,
       s.jurisdictions, s.size_bands, s.industries, s.maturity_stages, s.channels, s.universal
  FROM vt749_scoping s
  JOIN public.knowledge_cards delta_card ON delta_card.id = s.old_id
  JOIN public.knowledge_cards member_card ON member_card.card_key = delta_card.card_key
  JOIN public.knowledge_corpus_members m
       ON m.card_id = member_card.id AND m.corpus_version_id = 'fc6ed0b5-f138-59a0-92c3-ec6e4cded7cf'::uuid;

-- A database that has never been SEEDED has no v3 corpus to supersede. The corpus is loaded from
-- knowledge_corpus/ by registry_seed, not by migrations, so a fresh migration-only database holds
-- one placeholder card and zero corpus members — `test_clean_apply` runs exactly that database.
-- Raising there conflates "nothing to do" with "the corpus drifted", and the first is not an error:
-- a freshly seeded corpus carries these scopes already, from the same delta artifact this migration
-- was generated from. So: absent v3 ⇒ skip the whole migration cleanly; v3 PRESENT but not matching
-- ⇒ still fail closed, because a silent partial match would scope some cards and leave others
-- matching everything, which is worse than not running.
CREATE TEMP TABLE vt749_skip ON COMMIT DROP AS
SELECT 1 AS skip
 WHERE NOT EXISTS (
     SELECT 1 FROM public.knowledge_corpus_versions
      WHERE id = 'fc6ed0b5-f138-59a0-92c3-ec6e4cded7cf'::uuid
 );

DO $$
DECLARE
    resolved INT;
    dupes INT;
    scoped_already INT;
    skipping BOOLEAN;
BEGIN
    SELECT EXISTS (SELECT 1 FROM vt749_skip) INTO skipping;
    SELECT count(*) INTO resolved FROM vt749_targets;
    IF skipping THEN
        RAISE NOTICE 'VT-749: the v3 corpus this migration supersedes is absent — unseeded database, nothing to rescope. Every statement below is a no-op.';
    ELSIF resolved <> 63 THEN
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

-- 1. The v4 corpus version FIRST: knowledge_cards.corpus_version_id is an FK to it,
--    so inserting the cards first fails the constraint. shadow/pending like its parent — VT-749 changes what the
--    corpus SAYS, not its admission state; graduation stays a separate, Fazal-gated decision.
-- Every other write below reads FROM vt749_targets or from the v3 members, so all of them no-op on
-- an unseeded database by construction. This one does not, so it carries the skip guard explicitly.
INSERT INTO public.knowledge_corpus_versions
    (id, version, parent_corpus_version_id, content_digest, status, admission_verdict, created_by)
SELECT
    '775b193b-6916-57aa-a034-22c80079034c'::uuid, 4, 'fc6ed0b5-f138-59a0-92c3-ec6e4cded7cf'::uuid,
    '2e4ce1bf72dffe68e3ff6b61b118322254ceae14ccd36cd063e65afc64f4184a', 'shadow', 'pending', 'vt749:scope1'
 WHERE NOT EXISTS (SELECT 1 FROM vt749_skip)
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
    c.retrieval_eligible, '775b193b-6916-57aa-a034-22c80079034c'::uuid
FROM vt749_targets s
JOIN public.knowledge_cards c ON c.id = s.target_id
ON CONFLICT (id) DO NOTHING;

-- 3. Members: every v3 member that was NOT rescoped, plus the 63 new versions.
INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id)
SELECT '775b193b-6916-57aa-a034-22c80079034c'::uuid, m.card_id
  FROM public.knowledge_corpus_members m
 WHERE m.corpus_version_id = 'fc6ed0b5-f138-59a0-92c3-ec6e4cded7cf'::uuid
   AND m.card_id NOT IN (SELECT target_id FROM vt749_targets)
   AND NOT EXISTS (
       SELECT 1 FROM public.knowledge_corpus_members existing
        WHERE existing.corpus_version_id = '775b193b-6916-57aa-a034-22c80079034c'::uuid
          AND existing.card_id = m.card_id
   );

INSERT INTO public.knowledge_corpus_members (corpus_version_id, card_id)
SELECT '775b193b-6916-57aa-a034-22c80079034c'::uuid, s.new_id FROM vt749_targets s
 WHERE NOT EXISTS (
     SELECT 1 FROM public.knowledge_corpus_members existing
      WHERE existing.corpus_version_id = '775b193b-6916-57aa-a034-22c80079034c'::uuid AND existing.card_id = s.new_id
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
    -- Nothing was written on an unseeded database; there is no landing to assert.
    IF EXISTS (SELECT 1 FROM vt749_skip) THEN RETURN; END IF;

    SELECT count(*) INTO members FROM public.knowledge_corpus_members
     WHERE corpus_version_id = '775b193b-6916-57aa-a034-22c80079034c'::uuid;
    IF members <> 118 THEN
        RAISE EXCEPTION 'VT-749: v4 has % members, expected 118', members;
    END IF;

    SELECT count(*) INTO eligible
      FROM public.knowledge_corpus_members m JOIN public.knowledge_cards c ON c.id = m.card_id
     WHERE m.corpus_version_id = '775b193b-6916-57aa-a034-22c80079034c'::uuid AND c.retrieval_eligible;
    IF eligible <> 100 THEN
        RAISE EXCEPTION 'VT-749: v4 has % eligible cards, expected 100', eligible;
    END IF;

    SELECT count(*) INTO unscoped
      FROM public.knowledge_corpus_members m JOIN public.knowledge_cards c ON c.id = m.card_id
     WHERE m.corpus_version_id = '775b193b-6916-57aa-a034-22c80079034c'::uuid
       AND c.retrieval_eligible
       AND NOT c.applicability_universal
       AND coalesce(array_length(c.jurisdictions, 1), 0) = 0
       AND coalesce(array_length(c.size_bands, 1), 0) = 0
       AND coalesce(array_length(c.industries, 1), 0) = 0
       AND coalesce(array_length(c.maturity_stages, 1), 0) = 0
       AND coalesce(array_length(c.channels, 1), 0) = 0;
    IF unscoped <> 0 THEN
        RAISE EXCEPTION 'VT-749: v4 still has % eligible card(s) that scope nothing', unscoped;
    END IF;

    SELECT count(*) INTO universal
      FROM public.knowledge_corpus_members m JOIN public.knowledge_cards c ON c.id = m.card_id
     WHERE m.corpus_version_id = '775b193b-6916-57aa-a034-22c80079034c'::uuid
       AND c.retrieval_eligible AND c.applicability_universal;
    IF universal <> 42 THEN
        RAISE EXCEPTION 'VT-749: v4 has % universal eligible cards, expected 42', universal;
    END IF;
END $$;
