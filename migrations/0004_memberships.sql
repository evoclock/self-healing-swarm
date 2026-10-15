INSERT INTO memberships (tenant_id, user_id, role) VALUES
  ('t-a', 'approver-a', 'approver'),
  ('t-b', 'approver-b', 'approver'),
  ('t-c', 'approver-c', 'approver'),
  ('t-d', 'approver-d', 'approver')
ON CONFLICT (tenant_id, user_id) DO NOTHING;
