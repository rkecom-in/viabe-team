-- 112_vt347_app_role_grants.sql — VT-347: explicit app_role SELECT grants (belt-over-braces).
--
-- get_business_profile reads tenant_connector_status (mig 034) + l1_entities (mig 019) under the
-- app_role. Both were created AFTER mig 015, so their app_role SELECT currently rides ONLY on
-- mig 015's `ALTER DEFAULT PRIVILEGES`. If that default-privilege grant ever lapsed for one of
-- them, the read would raise insufficient_privilege (a 500). VT-347 (a) widened
-- _safe_query_undefined to degrade to None on insufficient_privilege; this is the (b) belt — an
-- EXPLICIT grant so the privilege doesn't depend solely on the default. GRANT is idempotent
-- (re-granting is a no-op) and additive (no data change). RLS still scopes the rows per-tenant.
GRANT SELECT ON tenant_connector_status TO app_role;
GRANT SELECT ON l1_entities TO app_role;
