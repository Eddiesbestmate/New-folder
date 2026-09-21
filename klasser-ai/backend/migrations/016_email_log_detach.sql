-- =============================================================================
-- KLASSER AI - 016 THE EMAIL LOG MUST NOT PIN ITS SUBJECTS
--
-- Migration 015 gave email_log plain foreign keys to users and schools. That
-- made the log block deletion of the very rows it describes: removing a user
-- failed with a foreign key violation because an email had once been sent to
-- them.
--
-- Both halves of that are wrong. A record of "we emailed this person" should
-- not stop the person being deleted, and it should not disappear when they are
-- - the whole reason it exists is to answer "was the school told?" after the
-- fact, including about people who have since left.
--
-- ON DELETE SET NULL keeps the row, the address, the template and the outcome,
-- and lets the user or school go. The recipient column is plain text and is
-- never nulled, so the record stays meaningful.
-- =============================================================================

ALTER TABLE email_log DROP CONSTRAINT IF EXISTS email_log_user_id_fkey;
ALTER TABLE email_log DROP CONSTRAINT IF EXISTS email_log_school_id_fkey;

ALTER TABLE email_log
    ADD CONSTRAINT email_log_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL;

ALTER TABLE email_log
    ADD CONSTRAINT email_log_school_id_fkey
    FOREIGN KEY (school_id) REFERENCES schools(id) ON DELETE SET NULL;
