-- #947: persist waro_visitor_key through self-service registration challenges
ALTER TABLE onboarding_email_challenges
    ADD COLUMN IF NOT EXISTS first_visitor_key TEXT,
    ADD COLUMN IF NOT EXISTS last_visitor_key TEXT;

COMMENT ON COLUMN onboarding_email_challenges.first_visitor_key IS
    'First waro_visitor_key seen for this email registration flow.';
COMMENT ON COLUMN onboarding_email_challenges.last_visitor_key IS
    'Latest waro_visitor_key from register-magic-link; copied to lead_interactions on verify.';
