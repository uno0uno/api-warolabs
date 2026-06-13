-- Normalize profile emails: case-insensitive unique constraint (run after scripts/dedup_profile_emails.py --apply)
CREATE UNIQUE INDEX IF NOT EXISTS profile_email_lower_unique
  ON profile (lower(trim(email)));
