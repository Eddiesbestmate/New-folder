-- =============================================================================
-- KLASSER AI - 013 THE FREE SANDBOX RUN
--
-- `onboarding` already had sandbox_used and sandbox_used_at from migration 001,
-- but nothing ever set them: the free run the marketing site promises was never
-- built. What was missing is somewhere to record *which* timetable a school was
-- given, because the run happens on the sandbox school's data, not theirs.
--
-- That is the whole design. Copying Westfield College's 48 students into a real
-- school to demonstrate the product would leave them deleting sample data
-- before they could start, and a school that abandoned setup would be left
-- with a fake roll. So the generation runs on the sandbox school and the
-- requesting school is granted read access to that one timetable - recorded
-- here, and nowhere else.
-- =============================================================================

ALTER TABLE onboarding
    ADD COLUMN IF NOT EXISTS sandbox_timetable_id UUID REFERENCES timetables(id);

COMMENT ON COLUMN onboarding.sandbox_timetable_id IS
    'The one sandbox-school timetable this school may read. Read-only: it can '
    'never be published, edited or exported as their own.';

-- The read allowance is checked on every output request for a sandbox viewer,
-- so it needs to be a lookup rather than a scan.
CREATE INDEX IF NOT EXISTS idx_onboarding_sandbox
    ON onboarding (sandbox_timetable_id)
    WHERE sandbox_timetable_id IS NOT NULL;

-- A sample timetable is marked in its own notes, so it is obvious in the
-- sandbox school's history which runs were demonstrations and for whom.
-- No new column: timetable_versions.notes already exists for exactly this.
