-- =============================================================================
-- KLASSER AI - 012 SUPPORT TICKET HANDLING
--
-- SUPPORT.md gives the dev portal two things the schema had no room for:
-- internal notes ("not visible to school") and a record of who resolved a
-- ticket. Without somewhere to put them, "add an internal note" would have to
-- be appended to the body the school can read - which is the opposite of what
-- internal means.
-- =============================================================================

ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS internal_notes TEXT,
    ADD COLUMN IF NOT EXISTS resolved_by    TEXT,
    ADD COLUMN IF NOT EXISTS first_response_at TIMESTAMPTZ;

-- The dev queue lists open tickets worst-first, and a school lists its own.
CREATE INDEX IF NOT EXISTS idx_tickets_queue
    ON support_tickets (status, priority, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_school
    ON support_tickets (school_id, created_at DESC);

-- A school reads its own tickets through the API, never through the Supabase
-- client, and internal_notes must never reach it either way. The table already
-- has school_isolation from 002; this makes the intent explicit in the schema.
COMMENT ON COLUMN support_tickets.internal_notes IS
    'Dev-only. Never returned by a school-facing endpoint.';


-- --- Export templates --------------------------------------------------------
-- Two schools can both have a template called "Sentral", but one school should
-- not end up with two of them - the dropdown on the export page would show the
-- same name twice with no way to tell them apart.

CREATE UNIQUE INDEX IF NOT EXISTS idx_export_template_name
    ON csv_output_templates (school_id, lower(name));
