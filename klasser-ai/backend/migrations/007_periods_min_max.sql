-- =============================================================================
-- KLASSER AI - 007 PERIODS PER CYCLE AS A RANGE
--
-- Supersedes the single periods_per_cycle added in 006. Schools state a minimum
-- and a maximum, so the allocator has room to move: it must give every class at
-- least the minimum, and may go up to the maximum where the timetable allows.
--
-- 006 is left in place rather than edited - it has already been applied, and a
-- migration that changes after running is a migration nobody can trust.
-- =============================================================================

ALTER TABLE subject_settings
    ADD COLUMN IF NOT EXISTS min_periods_per_cycle INTEGER,
    ADD COLUMN IF NOT EXISTS max_periods_per_cycle INTEGER;

-- Carry over anything 006 set: a fixed count becomes a range of one.
UPDATE subject_settings
SET min_periods_per_cycle = COALESCE(min_periods_per_cycle, periods_per_cycle),
    max_periods_per_cycle = COALESCE(max_periods_per_cycle, periods_per_cycle)
WHERE periods_per_cycle IS NOT NULL;

ALTER TABLE subject_settings DROP CONSTRAINT IF EXISTS subject_settings_periods_per_cycle_check;
ALTER TABLE subject_settings DROP COLUMN IF EXISTS periods_per_cycle;

ALTER TABLE subject_settings
    DROP CONSTRAINT IF EXISTS subject_settings_periods_range_check;
ALTER TABLE subject_settings
    ADD CONSTRAINT subject_settings_periods_range_check
    CHECK (
        (min_periods_per_cycle IS NULL OR
            (min_periods_per_cycle >= 1 AND min_periods_per_cycle <= 60))
        AND (max_periods_per_cycle IS NULL OR
            (max_periods_per_cycle >= 1 AND max_periods_per_cycle <= 60))
        AND (min_periods_per_cycle IS NULL OR max_periods_per_cycle IS NULL
             OR min_periods_per_cycle <= max_periods_per_cycle)
    );

DELETE FROM settings WHERE key = 'default_periods_per_cycle';

INSERT INTO settings (key, value, category, description) VALUES
('default_min_periods_per_cycle', '4', 'timetable',
 'Minimum times a class meets per cycle when the subject does not state its own'),
('default_max_periods_per_cycle', '5', 'timetable',
 'Maximum times a class meets per cycle when the subject does not state its own')
ON CONFLICT (key) DO NOTHING;

-- Sandbox: a realistic spread, with room to move on most subjects.
UPDATE subject_settings SET min_periods_per_cycle = v.lo, max_periods_per_cycle = v.hi
FROM (VALUES
    ('English', 4, 5), ('Mathematics', 4, 5), ('Science', 3, 4),
    ('Computing', 2, 3), ('History', 2, 3), ('Geography', 2, 3),
    ('PE', 1, 2), ('Health', 1, 1)
) AS v(subject, lo, hi)
WHERE subject_settings.subject = v.subject
  AND subject_settings.school_id = (
      SELECT id FROM schools WHERE name = 'Westfield College' LIMIT 1);
