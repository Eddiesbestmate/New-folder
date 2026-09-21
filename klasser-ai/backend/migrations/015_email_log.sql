-- =============================================================================
-- KLASSER AI - 015 EMAIL LOG
--
-- Every email attempted, whether or not it was sent.
--
-- The question this exists to answer is "did the school get told?" - the first
-- one asked when a school says they were charged, or blocked, without warning.
-- Without a record, the honest answer is "probably".
--
-- 'skipped' is a first-class status, not an error: with no provider key
-- configured the template is still rendered and the intent recorded, so the
-- whole path is exercised and testable before Resend exists.
-- =============================================================================

CREATE TABLE IF NOT EXISTS email_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID REFERENCES schools(id),
    user_id         UUID REFERENCES users(id),
    recipient       TEXT NOT NULL,
    template_name   TEXT NOT NULL,
    subject         TEXT NOT NULL,
    status          TEXT NOT NULL,
                    -- 'sent' | 'skipped' | 'failed'
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The dev portal reads this newest-first, and per school when investigating one.
CREATE INDEX IF NOT EXISTS idx_email_log_recent
    ON email_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_log_school
    ON email_log (school_id, created_at DESC);

-- Recipient addresses across every school. A school has no business reading
-- this through the Supabase client; the dev portal reads it as the service
-- role. RLS on, no policy: deny all.
ALTER TABLE email_log ENABLE ROW LEVEL SECURITY;
