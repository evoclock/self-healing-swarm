-- Facts written before expiry existed get the same 30-day life, counted from when they were written.
UPDATE memory SET expires_at = created_at + interval '30 days' WHERE kind = 'fact' AND expires_at IS NULL;
