-- 186_vt711_specialist_memory_and_assignment.sql — VT-711 specialist thin memory + assignment.
--
-- WHAT: add a GLOBAL default-assignment label to immutable knowledge cards, then create three
-- TENANT-SCOPED tables for per-tenant assignment overrides, specialist task customisations, and
-- their attributable append-only lifecycle.
-- WHY: the Manager owns broad business knowledge while specialists receive only lane cards and
-- thin task customisation. Tenant customisation must never enter GLOBAL knowledge_lifecycle_events.
-- PRIVACY: every tenant table has tenant_id NOT NULL, RLS + FORCE RLS, and is registered in the
-- same change set's DSR inventory. DSR anonymizes tenants instead of deleting them, so FK cascades
-- are not accepted as erasure proof.
-- REVERSAL (not executed): remove the three DSR inventory entries, drop events, memory cards and
-- assignments, then remove knowledge_cards.default_assignment.

ALTER TABLE public.knowledge_cards
    ADD COLUMN default_assignment TEXT NOT NULL DEFAULT 'manager_global',
    ADD CONSTRAINT knowledge_cards_default_assignment_check CHECK (
        default_assignment IN ('manager_global', 'manager_tenant', 'disabled')
        OR default_assignment ~ '^specialist:[a-z][a-z0-9_]{0,99}$'
    );

CREATE INDEX knowledge_cards_default_assignment_status
    ON public.knowledge_cards (default_assignment, status);


CREATE TABLE public.knowledge_card_assignments (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES public.tenants (id) ON DELETE CASCADE,
    card_id                 UUID NOT NULL
                                 REFERENCES public.knowledge_cards (id) ON DELETE CASCADE,
    scope                   TEXT NOT NULL CHECK (
                                scope IN ('manager_global', 'manager_tenant', 'disabled')
                                OR scope ~ '^specialist:[a-z][a-z0-9_]{0,99}$'
                            ),
    enabled                 BOOLEAN NOT NULL DEFAULT true,
    reason                  TEXT NOT NULL CHECK (btrim(reason) <> ''),
    actor                   TEXT NOT NULL CHECK (actor IN ('vtr', 'manager')),
    actor_id                TEXT NOT NULL CHECK (btrim(actor_id) <> ''),
    change_idempotency_key  TEXT NOT NULL CHECK (btrim(change_idempotency_key) <> ''),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT knowledge_card_assignments_tenant_card_uniq UNIQUE (tenant_id, card_id),
    CONSTRAINT knowledge_card_assignments_tenant_id_id_uniq UNIQUE (tenant_id, id),
    CONSTRAINT knowledge_card_assignments_change_idempotency_uniq
        UNIQUE (tenant_id, change_idempotency_key),
    CONSTRAINT knowledge_card_assignments_disabled_coherent CHECK (
        scope <> 'disabled' OR NOT enabled
    )
);

CREATE INDEX knowledge_card_assignments_card_fk
    ON public.knowledge_card_assignments (card_id);
CREATE INDEX knowledge_card_assignments_tenant_scope
    ON public.knowledge_card_assignments (tenant_id, scope, enabled);


CREATE TABLE public.specialist_memory_cards (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES public.tenants (id) ON DELETE CASCADE,
    agent                    TEXT NOT NULL
                                  CHECK (agent ~ '^[a-z][a-z0-9_]{0,99}$'),
    assignment_scope         TEXT GENERATED ALWAYS AS ('specialist:' || agent) STORED,
    memory_type              TEXT NOT NULL DEFAULT 'task_customization'
                                  CHECK (memory_type = 'task_customization'),
    task_scope               TEXT NOT NULL CHECK (btrim(task_scope) <> ''),
    memory_key               TEXT NOT NULL CHECK (btrim(memory_key) <> ''),
    customization            TEXT NOT NULL CHECK (btrim(customization) <> ''),
    authored_by              TEXT NOT NULL CHECK (authored_by IN ('vtr', 'manager')),
    author_id                TEXT NOT NULL CHECK (btrim(author_id) <> ''),
    reason                   TEXT NOT NULL CHECK (btrim(reason) <> ''),
    status                   TEXT NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('active', 'disabled', 'superseded')),
    version                  INT NOT NULL DEFAULT 1 CHECK (version >= 1),
    supersedes_memory_card_id UUID NULL,
    idempotency_key          TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT specialist_memory_cards_tenant_id_id_uniq UNIQUE (tenant_id, id),
    CONSTRAINT specialist_memory_cards_version_uniq
        UNIQUE (tenant_id, agent, task_scope, memory_key, version),
    CONSTRAINT specialist_memory_cards_idempotency_uniq UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT specialist_memory_cards_version_chain CHECK (
        (version = 1 AND supersedes_memory_card_id IS NULL)
        OR (version > 1 AND supersedes_memory_card_id IS NOT NULL)
    ),
    CONSTRAINT specialist_memory_cards_supersedes_tenant_fk FOREIGN KEY (
        tenant_id, supersedes_memory_card_id
    ) REFERENCES public.specialist_memory_cards (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX specialist_memory_cards_supersedes_fk
    ON public.specialist_memory_cards (supersedes_memory_card_id);
CREATE INDEX specialist_memory_cards_tenant_agent_active
    ON public.specialist_memory_cards (tenant_id, agent, task_scope, memory_key, version DESC)
    WHERE status = 'active';


CREATE TABLE public.specialist_memory_events (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          UUID NOT NULL REFERENCES public.tenants (id) ON DELETE CASCADE,
    assignment_id      UUID NULL,
    assignment_ref     UUID NULL,
    memory_card_id     UUID NULL,
    memory_card_ref    UUID NULL,
    event_type         TEXT NOT NULL
                            CHECK (event_type IN ('write', 'change', 'disable', 'flip')),
    actor              TEXT NOT NULL CHECK (actor IN ('vtr', 'manager')),
    actor_id           TEXT NOT NULL CHECK (btrim(actor_id) <> ''),
    reason             TEXT NOT NULL CHECK (btrim(reason) <> ''),
    idempotency_key    TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT specialist_memory_events_exactly_one_target CHECK (
        num_nonnulls(assignment_ref, memory_card_ref) = 1
    ),
    CONSTRAINT specialist_memory_events_idempotency_uniq
        UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT specialist_memory_events_assignment_tenant_fk FOREIGN KEY (
        tenant_id, assignment_id
    ) REFERENCES public.knowledge_card_assignments (tenant_id, id) ON DELETE SET NULL,
    CONSTRAINT specialist_memory_events_card_tenant_fk FOREIGN KEY (
        tenant_id, memory_card_id
    ) REFERENCES public.specialist_memory_cards (tenant_id, id) ON DELETE SET NULL
);

CREATE INDEX specialist_memory_events_assignment_fk
    ON public.specialist_memory_events (assignment_id);
CREATE INDEX specialist_memory_events_memory_card_fk
    ON public.specialist_memory_events (memory_card_id);
CREATE INDEX specialist_memory_events_tenant_created
    ON public.specialist_memory_events (tenant_id, created_at);


ALTER TABLE public.knowledge_card_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.knowledge_card_assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY knowledge_card_assignments_select ON public.knowledge_card_assignments FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY knowledge_card_assignments_insert ON public.knowledge_card_assignments FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY knowledge_card_assignments_update ON public.knowledge_card_assignments FOR UPDATE
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY knowledge_card_assignments_delete ON public.knowledge_card_assignments FOR DELETE
    USING (false);

ALTER TABLE public.specialist_memory_cards ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.specialist_memory_cards FORCE ROW LEVEL SECURITY;
CREATE POLICY specialist_memory_cards_select ON public.specialist_memory_cards FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY specialist_memory_cards_insert ON public.specialist_memory_cards FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY specialist_memory_cards_update ON public.specialist_memory_cards FOR UPDATE
    USING (false) WITH CHECK (false);
CREATE POLICY specialist_memory_cards_delete ON public.specialist_memory_cards FOR DELETE
    USING (false);

ALTER TABLE public.specialist_memory_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.specialist_memory_events FORCE ROW LEVEL SECURITY;
CREATE POLICY specialist_memory_events_select ON public.specialist_memory_events FOR SELECT
    USING (tenant_id = app_current_tenant());
CREATE POLICY specialist_memory_events_insert ON public.specialist_memory_events FOR INSERT
    WITH CHECK (tenant_id = app_current_tenant());
CREATE POLICY specialist_memory_events_update ON public.specialist_memory_events FOR UPDATE
    USING (false) WITH CHECK (false);
CREATE POLICY specialist_memory_events_delete ON public.specialist_memory_events FOR DELETE
    USING (false);


CREATE OR REPLACE FUNCTION public.knowledge_card_assignment_change_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.change_idempotency_key IS NOT DISTINCT FROM OLD.change_idempotency_key THEN
        RAISE EXCEPTION
            'knowledge_card_assignments change requires a new idempotency key (VT-711)';
    END IF;
    IF (NEW.scope, NEW.enabled, NEW.reason, NEW.actor, NEW.actor_id)
       IS NOT DISTINCT FROM
       (OLD.scope, OLD.enabled, OLD.reason, OLD.actor, OLD.actor_id)
    THEN
        RAISE EXCEPTION 'knowledge_card_assignments no-op change blocked (VT-711)';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER knowledge_card_assignments_change_guard
    BEFORE UPDATE ON public.knowledge_card_assignments
    FOR EACH ROW EXECUTE FUNCTION public.knowledge_card_assignment_change_guard();


CREATE OR REPLACE FUNCTION public.specialist_memory_emit_assignment_event()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    kind TEXT;
BEGIN
    kind := CASE
        WHEN TG_OP = 'INSERT' THEN 'write'
        WHEN OLD.enabled AND NOT NEW.enabled THEN 'disable'
        WHEN NEW.scope IS DISTINCT FROM OLD.scope THEN 'flip'
        ELSE 'change'
    END;
    INSERT INTO public.specialist_memory_events (
        tenant_id, assignment_id, assignment_ref, event_type, actor, actor_id, reason,
        idempotency_key
    ) VALUES (
        NEW.tenant_id, NEW.id, NEW.id, kind, NEW.actor, NEW.actor_id, NEW.reason,
        'assignment:' || NEW.change_idempotency_key
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER knowledge_card_assignments_emit_event
    AFTER INSERT OR UPDATE ON public.knowledge_card_assignments
    FOR EACH ROW EXECUTE FUNCTION public.specialist_memory_emit_assignment_event();


CREATE OR REPLACE FUNCTION public.specialist_memory_cards_immutable_row()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'specialist_memory_cards rows are immutable (VT-711); insert a new version instead of %',
        TG_OP;
END;
$$;

CREATE TRIGGER specialist_memory_cards_no_update
    BEFORE UPDATE ON public.specialist_memory_cards
    FOR EACH ROW EXECUTE FUNCTION public.specialist_memory_cards_immutable_row();


CREATE OR REPLACE FUNCTION public.specialist_memory_emit_card_event()
    RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    kind TEXT;
BEGIN
    kind := CASE
        WHEN NEW.status = 'disabled' THEN 'disable'
        WHEN NEW.version = 1 THEN 'write'
        ELSE 'change'
    END;
    INSERT INTO public.specialist_memory_events (
        tenant_id, memory_card_id, memory_card_ref, event_type, actor, actor_id, reason,
        idempotency_key
    ) VALUES (
        NEW.tenant_id, NEW.id, NEW.id, kind, NEW.authored_by, NEW.author_id, NEW.reason,
        'memory:' || NEW.idempotency_key
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER specialist_memory_cards_emit_event
    AFTER INSERT ON public.specialist_memory_cards
    FOR EACH ROW EXECUTE FUNCTION public.specialist_memory_emit_card_event();


CREATE OR REPLACE FUNCTION public.specialist_memory_events_append_only()
    RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Preserve event tombstones when a referenced assignment/card is removed. PostgreSQL's
    -- ON DELETE SET NULL is a nested UPDATE; a direct imitation remains depth 1 and blocked.
    IF TG_OP = 'UPDATE'
       AND pg_trigger_depth() > 1
       AND (
            (OLD.assignment_id IS NOT NULL AND NEW.assignment_id IS NULL
             AND OLD.memory_card_id IS NOT DISTINCT FROM NEW.memory_card_id)
            OR
            (OLD.memory_card_id IS NOT NULL AND NEW.memory_card_id IS NULL
             AND OLD.assignment_id IS NOT DISTINCT FROM NEW.assignment_id)
       )
       AND (to_jsonb(NEW) - ARRAY['assignment_id', 'memory_card_id'])
           IS NOT DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['assignment_id', 'memory_card_id'])
    THEN
        RETURN NEW;
    END IF;

    -- DSR is the sole hard-delete exception. Tenant roles still fail the DELETE RLS policy;
    -- the privileged purge path sets this transaction-local marker before deleting children.
    IF TG_OP = 'DELETE'
       AND current_setting('app.dsr_purge', true) = 'on'
    THEN
        RETURN OLD;
    END IF;

    RAISE EXCEPTION
        'specialist_memory_events is append-only (VT-711); % blocked', TG_OP;
END;
$$;

CREATE TRIGGER specialist_memory_events_no_row_mutate
    BEFORE UPDATE OR DELETE ON public.specialist_memory_events
    FOR EACH ROW EXECUTE FUNCTION public.specialist_memory_events_append_only();
CREATE TRIGGER specialist_memory_events_no_truncate
    BEFORE TRUNCATE ON public.specialist_memory_events
    FOR EACH STATEMENT EXECUTE FUNCTION public.specialist_memory_events_append_only();


COMMENT ON COLUMN public.knowledge_cards.default_assignment IS
    'VT-711 GLOBAL default routing scope; tenant overrides live only in knowledge_card_assignments.';
COMMENT ON TABLE public.knowledge_card_assignments IS
    'VT-711 TENANT assignment overrides for GLOBAL immutable cards; RLS/FORCE; flip events automatic.';
COMMENT ON TABLE public.specialist_memory_cards IS
    'VT-711 TENANT specialist thin memory, structurally limited to task_customization.';
COMMENT ON TABLE public.specialist_memory_events IS
    'VT-711 TENANT append-only lifecycle twin; DSR is the sole hard-delete exception.';
