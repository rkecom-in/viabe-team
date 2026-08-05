-- 196 — VT-725: let the application ROLE actually read the global knowledge corpus.
--
-- THE DEFECT
-- Eight O8 tables were created with `ENABLE ROW LEVEL SECURITY` and **no policy at all**. In
-- Postgres, RLS enabled with zero policies is DENY-ALL for every role that is neither the table
-- owner nor BYPASSRLS. `app_role` holds a SELECT grant and still saw ZERO rows:
--
--     via tenant_connection (app_role) -> knowledge_cards count = 0
--     direct (postgres)                -> knowledge_cards count = 182
--
-- So the curated corpus has never been readable by the application, on any environment. That is
-- why the VT-725 flip/narrowing canary fails with `candidates=0` for BOTH tenants and why the
-- specialist-narrowing gate cannot tell correct narrowing from a broken lane — there is nothing to
-- narrow. Wiring the Manager to the engine (VT-725's whole purpose) could never have served a card.
--
-- WHY A PLAIN READ POLICY IS THE RIGHT SHAPE
-- These eight tables are the GLOBAL curated corpus and its provenance: none of them carries a
-- `tenant_id` column, and `knowledge_cards.scope` is 'global' for all 182 rows. There is no tenant
-- data to partition, so the tenant-predicate pattern used by `knowledge_card_assignments`
-- (`tenant_id = app_current_tenant()`) has nothing to bind to here.
--
-- READ ONLY, AND ONLY FOR app_role
--   * SELECT only. No INSERT/UPDATE/DELETE policy is created, so the corpus stays immutable from
--     the application: loading remains the job of migrations and the authorized canaries, which run
--     as postgres/service_role and bypass RLS. An agent must never be able to write its own
--     knowledge — that is the whole point of a curated, governed corpus.
--   * `TO app_role`, NOT `TO public`. Supabase's default grants hand `anon` and `authenticated`
--     SELECT on these tables, and a `TO public` policy would expose the corpus through PostgREST.
--     The corpus is the moat; it is not a public API surface.
--   * The matching REVOKEs below close that PostgREST door outright rather than relying on the
--     absence of a policy to hold it shut.
--
-- Retrieval remains advisory regardless: `CardServingResult.AUTHORIZES_EFFECTS` and
-- `INJECTS_INTO_PROMPT` are both structurally false, and the D3 injection flip stays Fazal's.

BEGIN;

-- knowledge_cards is scope-qualified rather than blanket-true: the engine already refuses a
-- retrieval whose allowed_scopes include TENANT, and this keeps the policy honest if a
-- tenant-scoped card is ever introduced — such a row would need its own tenant-predicated policy
-- instead of silently inheriting global visibility.
CREATE POLICY knowledge_cards_select ON public.knowledge_cards
    FOR SELECT TO app_role USING (scope = 'global');

CREATE POLICY knowledge_card_embeddings_select ON public.knowledge_card_embeddings
    FOR SELECT TO app_role USING (true);

CREATE POLICY knowledge_card_sources_select ON public.knowledge_card_sources
    FOR SELECT TO app_role USING (true);

CREATE POLICY knowledge_corpus_members_select ON public.knowledge_corpus_members
    FOR SELECT TO app_role USING (true);

CREATE POLICY knowledge_corpus_versions_select ON public.knowledge_corpus_versions
    FOR SELECT TO app_role USING (true);

CREATE POLICY knowledge_evaluations_select ON public.knowledge_evaluations
    FOR SELECT TO app_role USING (true);

CREATE POLICY knowledge_lifecycle_events_select ON public.knowledge_lifecycle_events
    FOR SELECT TO app_role USING (true);

CREATE POLICY knowledge_sources_select ON public.knowledge_sources
    FOR SELECT TO app_role USING (true);

-- The corpus is not a PostgREST resource. Revoke the default Supabase grants so it cannot be read
-- or mutated over the public API even if a policy is later widened by accident.
--
-- Guarded on role existence: `anon` and `authenticated` are SUPABASE-created roles. They exist on
-- the Supabase dev/prod projects but NOT on a fresh local or CI cluster, where a bare
-- `REVOKE ... FROM anon` aborts with `role "anon" does not exist` and takes every DB-backed test
-- down with it (1666 errors on the pre-push run that caught this). The guard makes the statement a
-- no-op exactly where the role is absent, and identical to the bare REVOKE where it is present.
DO $$
DECLARE
    t text;
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            CONTINUE;
        END IF;
        FOREACH t IN ARRAY ARRAY[
            'knowledge_cards', 'knowledge_card_embeddings', 'knowledge_card_sources',
            'knowledge_corpus_members', 'knowledge_corpus_versions', 'knowledge_evaluations',
            'knowledge_lifecycle_events', 'knowledge_sources'
        ] LOOP
            EXECUTE format('REVOKE ALL ON public.%I FROM %I', t, r);
        END LOOP;
    END LOOP;
END
$$;

COMMIT;
