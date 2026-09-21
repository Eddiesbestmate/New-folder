-- =============================================================================
-- KLASSER AI - 006 PERIODS PER CYCLE
--
-- How many times a class meets per cycle is stated by the school, per subject
-- and year level. Nothing in the original schema held it, which left Layer 3
-- with no basis for deciding how many slots to allocate.
--
-- NULL means "use the school-wide default" (the default_periods_per_cycle
-- setting), so a school can set it once and override only where it differs.
-- =============================================================================

ALTER TABLE subject_settings
    ADD COLUMN IF NOT EXISTS periods_per_cycle INTEGER;

ALTER TABLE subject_settings
    DROP CONSTRAINT IF EXISTS subject_settings_periods_per_cycle_check;
ALTER TABLE subject_settings
    ADD CONSTRAINT subject_settings_periods_per_cycle_check
    CHECK (periods_per_cycle IS NULL
           OR (periods_per_cycle >= 1 AND periods_per_cycle <= 60));

INSERT INTO settings (key, value, category, description) VALUES
('default_periods_per_cycle', '5', 'timetable',
 'Times a class meets per cycle when the subject does not state its own')
ON CONFLICT (key) DO NOTHING;

-- Give the sandbox school a realistic spread so a generated timetable looks
-- like a real one rather than every subject meeting the same number of times.
UPDATE subject_settings SET periods_per_cycle = v.n
FROM (VALUES
    ('English', 5), ('Mathematics', 5), ('Science', 4), ('Computing', 3),
    ('History', 3), ('Geography', 3), ('PE', 2), ('Health', 1)
) AS v(subject, n)
WHERE subject_settings.subject = v.subject
  AND subject_settings.periods_per_cycle IS NULL
  AND subject_settings.school_id = (
      SELECT id FROM schools WHERE name = 'Westfield College' LIMIT 1);
