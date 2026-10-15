ALTER TABLE runs ADD COLUMN max_usd numeric;
ALTER TABLE runs ADD COLUMN tools text[];
UPDATE runs SET max_usd = 1.00, tools = ARRAY[]::text[] WHERE max_usd IS NULL;
ALTER TABLE runs ALTER COLUMN max_usd SET NOT NULL;
ALTER TABLE runs ALTER COLUMN tools SET NOT NULL;
ALTER TABLE runs ADD COLUMN generation integer NOT NULL DEFAULT 0;

ALTER TABLE steps ADD COLUMN started_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE steps ADD COLUMN generation integer NOT NULL DEFAULT 0;
ALTER TABLE run_tokens ADD COLUMN generation integer NOT NULL DEFAULT 0;

DROP FUNCTION resolve_token(text, inet);
CREATE FUNCTION resolve_token(p_token text, p_ip inet)
RETURNS TABLE (tenant_id text, run_id text, generation integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT run_tokens.tenant_id, run_tokens.run_id, run_tokens.generation
  FROM run_tokens
  WHERE run_tokens.token = p_token
    AND run_tokens.container_ip = p_ip
$$;

REVOKE ALL ON FUNCTION resolve_token(text, inet) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_token(text, inet) TO app;
