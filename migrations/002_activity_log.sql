-- Activity log — one row per meaningful action taken on an event, so teams
-- can see who did what and when. Run this once against your Postgres DB
-- (e.g. via psql, or your DB provider's SQL console) before deploying the
-- API changes in this release — the new endpoints assume this table exists.

CREATE TABLE IF NOT EXISTS activity_log (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action VARCHAR(50) NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Every read of this table filters by event_id and orders by created_at,
-- so a composite index serves both at once.
CREATE INDEX IF NOT EXISTS idx_activity_log_event_created
    ON activity_log (event_id, created_at DESC);
