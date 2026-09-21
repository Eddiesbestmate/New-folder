-- =============================================================================
-- KLASSER AI - 005 CLASS GROUP SLOTS
--
-- Layer 3 assigns each class group the period slots it occupies. Nothing in the
-- original schema held that: class_groups says a class exists, and
-- timetable_entries is only written at the very end, after validation passes.
-- Without this table the result of Layer 3 has nowhere to live while Layers 4
-- to 9 build on it.
--
-- One row per meeting, so a class that runs once a day for a 5-day cycle has 5
-- rows. Double periods are two rows with consecutive period_numbers, which is
-- what the "double periods occupy consecutive blocks" check in VALIDATION.md
-- needs in order to be checkable at all.
-- =============================================================================

CREATE TABLE IF NOT EXISTS class_group_slots (
    attempt_id      UUID    NOT NULL REFERENCES generation_attempts(id),
    class_group_id  UUID    NOT NULL REFERENCES class_groups(id),
    day_number      INTEGER NOT NULL,
    period_number   INTEGER NOT NULL,
    -- Resolved once the layout period is known. Nullable so Layer 3 can record
    -- the slot before Layer 4 attaches rooms.
    layout_period_id UUID   REFERENCES layout_periods(id),
    is_double_first BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (attempt_id, class_group_id, day_number, period_number)
);

CREATE INDEX IF NOT EXISTS idx_cgs_attempt_slot
    ON class_group_slots (attempt_id, day_number, period_number);

CREATE INDEX IF NOT EXISTS idx_cgs_group
    ON class_group_slots (class_group_id);

ALTER TABLE class_group_slots ENABLE ROW LEVEL SECURITY;

-- Scoped through the attempt to its school, the same way the allocation
-- registries are.
DROP POLICY IF EXISTS school_isolation ON class_group_slots;
CREATE POLICY school_isolation ON class_group_slots
    USING (EXISTS (
        SELECT 1 FROM generation_attempts ga
        JOIN timetables t ON t.id = ga.timetable_id
        WHERE ga.id = class_group_slots.attempt_id
          AND t.school_id = current_school_id()
    ));

DROP POLICY IF EXISTS dev_access ON class_group_slots;
CREATE POLICY dev_access ON class_group_slots
    USING ((SELECT role FROM users WHERE id = auth.uid()) = 'dev');
