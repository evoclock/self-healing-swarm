CREATE FUNCTION expired_leases()
RETURNS TABLE (tenant_id text, run_id text, resume_count integer)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT runs.tenant_id, runs.id, runs.resume_count
  FROM runs
  WHERE runs.state = 'RUNNING'
    AND runs.lease_until IS NOT NULL
    AND runs.lease_until < now()
  ORDER BY runs.lease_until
$$;

REVOKE ALL ON FUNCTION expired_leases() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION expired_leases() TO app;
