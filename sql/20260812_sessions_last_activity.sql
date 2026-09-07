-- #823: idle session tracking (absolute expires_at remains the hard ceiling)
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

COMMENT ON COLUMN sessions.last_activity_at IS
    'Last authenticated platform activity; idle timeout uses this (not sales).';
