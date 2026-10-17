-- Pre-assigned agent IPs: one row per address in use. The token is bound to the address before the container starts.
CREATE TABLE ip_leases (
  ip inet PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  run_id text NOT NULL,
  container_id text,
  allocated_at timestamptz NOT NULL DEFAULT now()
);

-- Metered provider use per tenant per minute: reserved before a call, settled to real usage after.
CREATE TABLE quota_minute (
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  provider text NOT NULL,
  minute timestamptz NOT NULL,
  tokens integer NOT NULL DEFAULT 0,
  requests integer NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, provider, minute)
);

ALTER TABLE memory ADD COLUMN expires_at timestamptz;

DO $$
DECLARE
  rel text;
BEGIN
  FOREACH rel IN ARRAY ARRAY['ip_leases', 'quota_minute'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', rel);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', rel);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
      rel
    );
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO app', rel);
  END LOOP;
END $$;

-- Tenants sharing a provider's per-minute limit right now: tenants with a running run (at least 1).
CREATE FUNCTION active_tenants()
RETURNS integer
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT GREATEST(1, count(DISTINCT tenant_id))::integer FROM runs WHERE state = 'RUNNING'
$$;

REVOKE ALL ON FUNCTION active_tenants() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION active_tenants() TO app;
