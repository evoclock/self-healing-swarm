DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN
    CREATE ROLE app LOGIN PASSWORD 'app' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END $$;

GRANT CONNECT ON DATABASE swarm TO app;
GRANT USAGE ON SCHEMA public TO app;
ALTER ROLE app SET row_security = on;

CREATE TABLE tenants (
  tenant_id text PRIMARY KEY,
  name text NOT NULL,
  domains text[] NOT NULL,
  concurrency_limit integer NOT NULL
);

CREATE TABLE memberships (
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  user_id text NOT NULL,
  role text NOT NULL CHECK (role IN ('viewer', 'approver', 'admin')),
  PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE agents (
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  id text NOT NULL,
  instructions text NOT NULL,
  tools text[] NOT NULL,
  cadence text,
  deadline_at timestamptz,
  models text[] NOT NULL,
  max_steps integer NOT NULL,
  max_tokens integer NOT NULL,
  max_usd numeric NOT NULL,
  wall_s integer NOT NULL,
  approval_policy text NOT NULL DEFAULT 'drafts-only',
  egress_allowlist text[] NOT NULL DEFAULT ARRAY[]::text[],
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE runs (
  id text PRIMARY KEY,
  tenant_id text NOT NULL,
  agent_id text NOT NULL,
  state text NOT NULL CHECK (state IN (
    'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED',
    'STOPPED_LOOP', 'STOPPED_BUDGET', 'ESCALATED'
  )),
  lease_until timestamptz,
  lease_holder text,
  resume_count integer NOT NULL DEFAULT 0,
  deadline_at timestamptz,
  artifact text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id)
);

CREATE TABLE steps (
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  run_id text NOT NULL REFERENCES runs (id),
  step_no integer NOT NULL,
  arg_hash text NOT NULL,
  status text NOT NULL CHECK (status IN ('STARTED', 'COMPLETED', 'FAILED')),
  provider text,
  model text,
  tokens_in integer,
  tokens_out integer,
  cost_usd numeric,
  cost_uncertain boolean NOT NULL DEFAULT false,
  response jsonb,
  simulated boolean NOT NULL DEFAULT false,
  PRIMARY KEY (run_id, step_no, arg_hash)
);

CREATE TABLE outbox (
  draft_id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  to_addr text NOT NULL,
  subject text NOT NULL,
  body text NOT NULL,
  attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
  flagged boolean NOT NULL DEFAULT false,
  flag_reasons text[] NOT NULL DEFAULT ARRAY[]::text[],
  state text NOT NULL CHECK (state IN ('DRAFT', 'APPROVED', 'SENDING', 'SENT')),
  message_id text
);

CREATE TABLE memory (
  id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  agent_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('cursor', 'fact', 'summary')),
  body text NOT NULL,
  source_ids text[] NOT NULL DEFAULT ARRAY[]::text[],
  run_id text,
  flagged boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id)
);

CREATE TABLE eval_records (
  id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  agent_id text NOT NULL,
  config_hash text NOT NULL,
  golden_version text NOT NULL,
  mean numeric,
  hard_checks_passed boolean NOT NULL,
  promoted boolean NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id bigserial PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  event text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE run_events (
  id bigserial PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  run_id text NOT NULL REFERENCES runs (id),
  event text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE run_tokens (
  token text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  run_id text NOT NULL REFERENCES runs (id),
  container_ip inet
);

CREATE TABLE mail (
  id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  from_addr text NOT NULL,
  to_addr text NOT NULL,
  subject text NOT NULL,
  body text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
  id text PRIMARY KEY,
  tenant_id text NOT NULL REFERENCES tenants (tenant_id),
  name text NOT NULL,
  body text NOT NULL
);

CREATE OR REPLACE FUNCTION enforce_tenant_rls() RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  rel text;
BEGIN
  FOR rel IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND NOT a.attisdropped
    WHERE n.nspname = 'public'
      AND c.relkind = 'r'
      AND a.attname = 'tenant_id'
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', rel);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', rel);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', rel);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
      rel
    );
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO app', rel);
  END LOOP;
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app;
END $$;

INSERT INTO tenants (tenant_id, name, domains, concurrency_limit) VALUES
  ('t-a', 'Tenant A', ARRAY['a.example'], 2),
  ('t-b', 'Tenant B', ARRAY['b.example'], 2);

INSERT INTO agents (
  tenant_id, id, instructions, tools, models, max_steps, max_tokens, max_usd, wall_s
) VALUES
  ('t-a', 'daily-brief', '', ARRAY[]::text[], ARRAY[]::text[], 20, 8000, 1.00, 120),
  ('t-b', 'daily-brief', '', ARRAY[]::text[], ARRAY[]::text[], 20, 8000, 1.00, 120);

INSERT INTO mail (id, tenant_id, from_addr, to_addr, subject, body) VALUES
  ('m-a-1', 't-a', 'ada@a.example', 'team@a.example', 'Monday notes', 'Ship the brief.'),
  ('m-b-1', 't-b', 'ben@b.example', 'team@b.example', 'Budget notes', 'Keep the totals.');

INSERT INTO runs (id, tenant_id, agent_id, state) VALUES
  ('r-a-1', 't-a', 'daily-brief', 'QUEUED'),
  ('r-b-1', 't-b', 'daily-brief', 'QUEUED');

SELECT enforce_tenant_rls();
