CREATE OR REPLACE FUNCTION resolve_token(p_token text, p_ip inet)
RETURNS TABLE (tenant_id text, run_id text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT run_tokens.tenant_id, run_tokens.run_id
  FROM run_tokens
  WHERE run_tokens.token = p_token
    AND run_tokens.container_ip = p_ip
$$;

REVOKE ALL ON FUNCTION resolve_token(text, inet) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_token(text, inet) TO app;
