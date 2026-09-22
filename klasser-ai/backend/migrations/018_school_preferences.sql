-- =============================================================================
-- KLASSER AI - 018 PER-SCHOOL TIMETABLE PREFERENCES
--
-- A generated timetable put 36% of student-days across both campuses, and 932
-- of those campus changes fell between back-to-back periods - a 20 minute trip
-- with zero minutes to make it. The deterministic solution check passed it,
-- because nothing was ever asked to compare a route's travel time against the
-- gap a student actually had.
--
-- routes.travel_minutes already held the 20; layer 3 simply never consulted
-- it. What was missing was the school's own answer to a question only they can
-- settle: may a student cross campuses mid-day at all?
--
--   false (default) - every class a student attends runs at their own campus.
--   true            - crossing is allowed, but only over a break long enough
--                     to walk or bus it, which layer 3 now enforces.
--
-- Defaulting to false is the safe end: a school that has not thought about it
-- gets a timetable nobody has to sprint across town for.
--
-- A table rather than columns on `schools`, because preferences accumulate and
-- `schools` is deliberately about identity, not policy.
-- =============================================================================

CREATE TABLE IF NOT EXISTS school_preferences (
    school_id                   UUID PRIMARY KEY REFERENCES schools(id),
    allow_cross_campus_travel   BOOLEAN NOT NULL DEFAULT false,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE school_preferences ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS school_isolation ON school_preferences;
CREATE POLICY school_isolation ON school_preferences
    FOR ALL
    USING (school_id = current_school_id())
    WITH CHECK (school_id = current_school_id());

DROP POLICY IF EXISTS dev_access ON school_preferences;
CREATE POLICY dev_access ON school_preferences
    FOR ALL USING (is_dev()) WITH CHECK (is_dev());

-- Every existing school gets the safe default explicitly, so a missing row
-- never has to mean anything.
INSERT INTO school_preferences (school_id)
SELECT id FROM schools
ON CONFLICT (school_id) DO NOTHING;

-- How hard layer 3 tries to pair periods into doubles. This is a preference,
-- not a constraint: a class that cannot be paired is still placed.
INSERT INTO settings (key, value, category, description) VALUES
('prefer_double_periods', 'true', 'timetable',
 'Pair each class''s periods into adjacent doubles where the timetable allows. A class that cannot be paired is still placed singly.')
ON CONFLICT (key) DO NOTHING;
