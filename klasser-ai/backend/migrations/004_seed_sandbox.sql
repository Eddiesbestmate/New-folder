-- =============================================================================
-- KLASSER AI - 004 SANDBOX SEED
-- Westfield College: the pre-loaded dummy school used for the free sandbox run
-- during onboarding. Two campuses, a 5-day cycle, transport between campuses.
--
-- Re-runnable: does nothing if the sandbox school already exists.
-- =============================================================================

DO $$
DECLARE
    v_school     UUID;
    v_north      UUID;
    v_south      UUID;
    v_layout     UUID;
    v_yard_duty  UUID;
    v_bus_duty   UUID;
BEGIN

IF EXISTS (SELECT 1 FROM schools WHERE name = 'Westfield College') THEN
    RAISE NOTICE 'Sandbox school already seeded - skipping';
    RETURN;
END IF;

-- --- School and campuses -----------------------------------------------------

INSERT INTO schools (name, timezone)
VALUES ('Westfield College', 'Australia/Sydney')
RETURNING id INTO v_school;

INSERT INTO campuses (school_id, name, address)
VALUES (v_school, 'North Campus', '12 Rosedale Avenue, Westfield NSW 2145')
RETURNING id INTO v_north;

INSERT INTO campuses (school_id, name, address)
VALUES (v_school, 'South Campus', '88 Marion Street, Westfield NSW 2145')
RETURNING id INTO v_south;

-- --- Layout: 5-day cycle, 8 slots per day ------------------------------------

INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
VALUES (v_school, 'Standard 5-day cycle', 5, true)
RETURNING id INTO v_layout;

INSERT INTO layout_periods
    (layout_id, school_id, day_number, period_number, period_type, start_time, end_time, label)
SELECT v_layout, v_school, d.day_number, p.period_number, p.period_type,
       p.start_time, p.end_time, p.label
FROM generate_series(1, 5) AS d(day_number)
CROSS JOIN (VALUES
    (1, 'teaching', TIME '09:00', TIME '10:00', 'Period 1'),
    (2, 'teaching', TIME '10:00', TIME '11:00', 'Period 2'),
    (3, 'break',    TIME '11:00', TIME '11:20', 'Recess'),
    (4, 'teaching', TIME '11:20', TIME '12:20', 'Period 3'),
    (5, 'teaching', TIME '12:20', TIME '13:20', 'Period 4'),
    (6, 'lunch',    TIME '13:20', TIME '14:00', 'Lunch'),
    (7, 'teaching', TIME '14:00', TIME '15:00', 'Period 5'),
    (8, 'mentor',   TIME '15:00', TIME '15:20', 'Mentor')
) AS p(period_number, period_type, start_time, end_time, label);

-- --- Rooms -------------------------------------------------------------------

INSERT INTO rooms (school_id, campus_id, name, capacity, preferred_min_capacity, room_type)
VALUES
    (v_school, v_north, 'N-101', 30, 18, 'classroom'),
    (v_school, v_north, 'N-102', 30, 18, 'classroom'),
    (v_school, v_north, 'N-103', 26, 16, 'classroom'),
    (v_school, v_north, 'N-104', 32, 20, 'classroom'),
    (v_school, v_north, 'N-Lab1', 24, 14, 'science_lab'),
    (v_school, v_north, 'N-Lab2', 24, 14, 'science_lab'),
    (v_school, v_north, 'N-Comp1', 28, 16, 'computer_lab'),
    (v_school, v_north, 'N-Gym', 60, 20, 'gym'),
    (v_school, v_south, 'S-201', 28, 16, 'classroom'),
    (v_school, v_south, 'S-202', 28, 16, 'classroom'),
    (v_school, v_south, 'S-203', 24, 14, 'classroom'),
    (v_school, v_south, 'S-Comp1', 26, 16, 'computer_lab');

-- --- Teachers ----------------------------------------------------------------

INSERT INTO teachers (school_id, first_name, surname, subjects, campus_id, max_blocks, max_duties_per_cycle, gender)
VALUES
    (v_school, 'Aisha',   'Rahman',    ARRAY['Mathematics','Science'],        v_north, 5, 10, 'F'),
    (v_school, 'Daniel',  'O''Brien',  ARRAY['English','History'],            v_north, 5, 10, 'M'),
    (v_school, 'Priya',   'Patel',     ARRAY['Computing','Mathematics'],      v_north, 5, 10, 'F'),
    (v_school, 'Marcus',  'Webb',      ARRAY['Science','Mathematics'],        v_north, 5, 10, 'M'),
    (v_school, 'Elena',   'Kovac',     ARRAY['English'],                      v_north, 4, 8,  'F'),
    (v_school, 'Tom',     'Fitzgerald',ARRAY['PE','Health'],                  v_north, 5, 12, 'M'),
    (v_school, 'Grace',   'Lim',       ARRAY['History','Geography'],          v_north, 5, 10, 'F'),
    (v_school, 'Samuel',  'Ncube',     ARRAY['Mathematics'],                  v_north, 5, 10, 'M'),
    (v_school, 'Hannah',  'Fischer',   ARRAY['Science'],                      v_south, 5, 10, 'F'),
    (v_school, 'Joseph',  'Tanaka',    ARRAY['Computing'],                    v_south, 4, 8,  'M'),
    (v_school, 'Layla',   'Haddad',    ARRAY['English','History'],            v_south, 5, 10, 'F'),
    (v_school, 'Peter',   'Novak',     ARRAY['Mathematics','Science'],        v_south, 5, 10, 'M'),
    (v_school, 'Rosa',    'Delgado',   ARRAY['Geography','History'],          v_south, 4, 8,  'F'),
    (v_school, 'Nathan',  'Cole',      ARRAY['PE','Health'],                  v_south, 5, 12, 'M');

-- One teacher is exempt from duties (part-time), so Layer 6 has a real case to
-- respect rather than an empty exemption table.
INSERT INTO teacher_duty_exemptions (teacher_id, school_id, reason)
SELECT id, v_school, 'Part-time - no yard duty per agreement'
FROM teachers WHERE school_id = v_school AND surname = 'Kovac';

-- --- Students ----------------------------------------------------------------
-- 48 students: 16 in each of Years 10, 11 and 12, split across both campuses.

INSERT INTO students (school_id, full_name, campus_id, year_group, gender, ability_band)
SELECT
    v_school,
    n.name,
    CASE WHEN n.idx % 3 = 0 THEN v_south ELSE v_north END,
    y.year_group,
    CASE WHEN n.idx % 2 = 0 THEN 'F' ELSE 'M' END,
    CASE WHEN n.idx % 3 = 0 THEN 'high'
         WHEN n.idx % 3 = 1 THEN 'mid'
         ELSE 'low' END
FROM (VALUES
    (1,  'Olivia Barnes'),   (2,  'Liam Chen'),        (3,  'Mia Okafor'),
    (4,  'Noah Zielinski'),  (5,  'Ava Kaur'),         (6,  'Ethan Moreau'),
    (7,  'Sophia Ivanov'),   (8,  'Lucas Bianchi'),    (9,  'Isla Nguyen'),
    (10, 'Oscar Mwangi'),    (11, 'Chloe Andersson'),  (12, 'Leo Ferreira'),
    (13, 'Amelia Duarte'),   (14, 'Jack Halvorsen'),   (15, 'Zara Malik'),
    (16, 'Henry Castellano')
) AS n(idx, name)
CROSS JOIN (VALUES ('Year 10'), ('Year 11'), ('Year 12')) AS y(year_group);

-- Subject enrolments: every student takes English and Mathematics, plus two
-- electives chosen deterministically so the sandbox produces the same result
-- on every run.
INSERT INTO student_subjects (student_id, school_id, subject)
SELECT s.id, v_school, subj
FROM students s
CROSS JOIN LATERAL (
    SELECT unnest(ARRAY['English', 'Mathematics']) AS subj
    UNION ALL
    SELECT CASE (('x' || substr(md5(s.full_name), 1, 8))::BIT(32)::INT % 3)
             WHEN 0 THEN 'Science'
             WHEN 1 THEN 'Computing'
             ELSE 'History'
           END
    UNION ALL
    SELECT CASE (('x' || substr(md5(s.full_name || 'b'), 1, 8))::BIT(32)::INT % 2)
             WHEN 0 THEN 'Geography'
             ELSE 'PE'
           END
) AS e(subj)
WHERE s.school_id = v_school
ON CONFLICT (student_id, subject) DO NOTHING;

-- --- Subject settings --------------------------------------------------------

INSERT INTO subject_settings
    (school_id, subject, year_level, hard_max_size, soft_max_size, min_size,
     default_room_type, campus_locked_id, is_double_period, code_prefix)
VALUES
    (v_school, 'English',     NULL, 28, 24, 8, 'classroom',    NULL,    false, 'ENG'),
    (v_school, 'Mathematics', NULL, 28, 24, 8, 'classroom',    NULL,    false, 'MAT'),
    (v_school, 'Science',     NULL, 24, 20, 8, 'science_lab',  NULL,    true,  'SCI'),
    (v_school, 'Computing',   NULL, 26, 22, 6, 'computer_lab', NULL,    false, 'COM'),
    (v_school, 'History',     NULL, 28, 24, 8, 'classroom',    NULL,    false, 'HIS'),
    (v_school, 'Geography',   NULL, 28, 24, 8, 'classroom',    NULL,    false, 'GEO'),
    (v_school, 'PE',          NULL, 40, 32, 10,'gym',          v_north, false, 'PED'),
    (v_school, 'Health',      NULL, 28, 24, 8, 'classroom',    NULL,    false, 'HLT');

INSERT INTO class_code_settings (school_id, pattern, subject_code_length, uppercase)
VALUES (v_school, '{year}{subject}{index}', 3, true);

INSERT INTO balancing_settings
    (school_id, balance_size, balance_gender, balance_ability, balance_campus, size_tolerance_pct)
VALUES (v_school, true, true, true, true, 10);

-- --- Duties ------------------------------------------------------------------

INSERT INTO duty_types (school_id, name, campus_id, min_staff, is_bus_duty)
VALUES (v_school, 'Yard duty', NULL, 2, false)
RETURNING id INTO v_yard_duty;

INSERT INTO duty_types (school_id, name, campus_id, min_staff, is_bus_duty)
VALUES (v_school, 'Bus supervision', NULL, 1, true)
RETURNING id INTO v_bus_duty;

-- Yard duty at recess and lunch, both campuses, every day of the cycle.
INSERT INTO layout_duty_slots
    (layout_id, school_id, duty_type_id, day_number, timing, start_time, end_time, campus_id, min_staff)
SELECT v_layout, v_school, v_yard_duty, d.day_number, t.timing, t.start_time, t.end_time, c.campus_id, 2
FROM generate_series(1, 5) AS d(day_number)
CROSS JOIN (VALUES
    ('break', TIME '11:00', TIME '11:20'),
    ('lunch', TIME '13:20', TIME '14:00')
) AS t(timing, start_time, end_time)
CROSS JOIN (VALUES (v_north), (v_south)) AS c(campus_id);

-- Bus supervision after school, both campuses. Layer 8 replaces these times
-- with the real departure times once the transport schedule exists.
INSERT INTO layout_duty_slots
    (layout_id, school_id, duty_type_id, day_number, timing, start_time, end_time, campus_id, min_staff)
SELECT v_layout, v_school, v_bus_duty, d.day_number, 'after_school',
       TIME '15:20', TIME '15:45', c.campus_id, 1
FROM generate_series(1, 5) AS d(day_number)
CROSS JOIN (VALUES (v_north), (v_south)) AS c(campus_id);

-- --- Transport ---------------------------------------------------------------

INSERT INTO buses (school_id, name, capacity, home_campus_id)
VALUES
    (v_school, 'Bus 1', 45, v_north),
    (v_school, 'Bus 2', 45, v_north),
    (v_school, 'Bus 3', 30, v_south);

INSERT INTO routes (school_id, from_campus_id, to_campus_id, travel_minutes)
VALUES
    (v_school, v_north, v_south, 12),
    (v_school, v_south, v_north, 12);

-- --- Billing and onboarding state --------------------------------------------
-- The sandbox never charges credits, but the rows must exist so the same code
-- paths run as for a real school.

INSERT INTO school_credits (school_id, balance) VALUES (v_school, 0);
INSERT INTO school_billing (school_id, billing_mode) VALUES (v_school, 'credits');

INSERT INTO school_complexity
    (school_id, student_count, teacher_count, campus_count, transport_enabled,
     bus_route_count, duties_enabled, cycle_days, accelerated_enabled,
     double_periods_enabled)
VALUES (v_school, 48, 14, 2, true, 2, true, 5, false, true);

INSERT INTO onboarding
    (school_id, step_school_details, step_campuses, step_billing, step_layout,
     step_teachers, step_students, step_rooms, step_subjects, step_transport,
     completed, completed_at)
VALUES (v_school, true, true, true, true, true, true, true, true, true, true, now());

-- --- Point the app at this school --------------------------------------------

UPDATE settings SET value = v_school::TEXT, updated_at = now()
WHERE key = 'sandbox_school_id';

RAISE NOTICE 'Sandbox school seeded: Westfield College (%)', v_school;

END $$;
