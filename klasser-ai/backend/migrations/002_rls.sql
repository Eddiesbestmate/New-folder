-- =============================================================================
-- KLASSER AI - 002 ROW LEVEL SECURITY
-- One school can never read or write another school's data. Enforced at the
-- database, so a mistake in an API call cannot leak across tenants.
--
-- The backend connects with the direct Postgres role and is NOT subject to RLS.
-- These policies protect the path where the frontend talks to Supabase with a
-- user JWT (supabase-js), and act as the backstop for the backend.
-- =============================================================================

-- Helper: the calling user's school. STABLE so the planner caches it per query.
CREATE OR REPLACE FUNCTION current_school_id()
RETURNS UUID
LANGUAGE SQL STABLE SECURITY DEFINER
SET search_path = public
AS $$
    SELECT school_id FROM users WHERE id = auth.uid()
$$;

CREATE OR REPLACE FUNCTION is_dev()
RETURNS BOOLEAN
LANGUAGE SQL STABLE SECURITY DEFINER
SET search_path = public
AS $$
    SELECT COALESCE((SELECT role = 'dev' FROM users WHERE id = auth.uid()), false)
$$;


-- --- Standard school_id isolation -------------------------------------------
-- Every table below has a school_id column. FOR ALL with WITH CHECK so INSERT
-- and UPDATE are covered too, not just SELECT.

DO $$
DECLARE
    t TEXT;
    tables TEXT[] := ARRAY[
        'campuses', 'rooms', 'teachers', 'teacher_availability',
        'teacher_duty_exemptions', 'students', 'student_subjects', 'buses',
        'routes', 'subject_settings', 'class_code_settings',
        'balancing_settings', 'timetable_layouts', 'layout_periods',
        'duty_types', 'layout_duty_slots', 'requirements',
        'class_group_definitions', 'timetables', 'timetable_versions',
        'class_groups', 'timetable_edits', 'consistency_pins',
        'school_credits', 'school_billing', 'credit_transactions',
        'generation_cost_breakdown', 'school_complexity',
        'invoices', 'outstanding_charges', 'payg_charge_log',
        'invoiced_billing_applications', 'import_jobs',
        'csv_output_templates', 'onboarding', 'support_tickets'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);

        EXECUTE format('DROP POLICY IF EXISTS school_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY school_isolation ON %I
                FOR ALL
                USING (school_id = current_school_id())
                WITH CHECK (school_id = current_school_id())
        $f$, t);

        EXECUTE format('DROP POLICY IF EXISTS dev_access ON %I', t);
        EXECUTE format($f$
            CREATE POLICY dev_access ON %I
                FOR ALL
                USING (is_dev())
                WITH CHECK (is_dev())
        $f$, t);
    END LOOP;
END $$;


-- --- Tables reached through a parent ----------------------------------------
-- These carry no school_id of their own, so they are scoped through the row
-- that owns them. They appear in the RLS list in DATABASE.md but need their own
-- predicate rather than the standard one.

ALTER TABLE timetable_entries ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON timetable_entries;
CREATE POLICY school_isolation ON timetable_entries
    FOR ALL
    USING (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()))
    WITH CHECK (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON timetable_entries;
CREATE POLICY dev_access ON timetable_entries FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE timetable_entry_students ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON timetable_entry_students;
CREATE POLICY school_isolation ON timetable_entry_students
    FOR ALL
    USING (timetable_entry_id IN (
        SELECT te.id FROM timetable_entries te
        JOIN timetables t ON t.id = te.timetable_id
        WHERE t.school_id = current_school_id()))
    WITH CHECK (timetable_entry_id IN (
        SELECT te.id FROM timetable_entries te
        JOIN timetables t ON t.id = te.timetable_id
        WHERE t.school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON timetable_entry_students;
CREATE POLICY dev_access ON timetable_entry_students FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE transport_schedule ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON transport_schedule;
CREATE POLICY school_isolation ON transport_schedule
    FOR ALL
    USING (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()))
    WITH CHECK (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON transport_schedule;
CREATE POLICY dev_access ON transport_schedule FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE duty_assignments ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON duty_assignments;
CREATE POLICY school_isolation ON duty_assignments
    FOR ALL
    USING (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()))
    WITH CHECK (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON duty_assignments;
CREATE POLICY dev_access ON duty_assignments FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE generation_attempts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON generation_attempts;
CREATE POLICY school_isolation ON generation_attempts
    FOR ALL
    USING (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()))
    WITH CHECK (timetable_id IN (SELECT id FROM timetables WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON generation_attempts;
CREATE POLICY dev_access ON generation_attempts FOR ALL USING (is_dev()) WITH CHECK (is_dev());


-- --- Identity tables ---------------------------------------------------------
-- Not in the DATABASE.md list, but they hold cross-tenant data and are readable
-- by the frontend Supabase client, so they are covered here too.

ALTER TABLE schools ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON schools;
CREATE POLICY school_isolation ON schools
    FOR ALL USING (id = current_school_id()) WITH CHECK (id = current_school_id());
DROP POLICY IF EXISTS dev_access ON schools;
CREATE POLICY dev_access ON schools FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON users;
CREATE POLICY school_isolation ON users
    FOR ALL USING (school_id = current_school_id()) WITH CHECK (school_id = current_school_id());
DROP POLICY IF EXISTS dev_access ON users;
CREATE POLICY dev_access ON users FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE invites ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON invites;
CREATE POLICY school_isolation ON invites
    FOR ALL USING (school_id = current_school_id()) WITH CHECK (school_id = current_school_id());
DROP POLICY IF EXISTS dev_access ON invites;
CREATE POLICY dev_access ON invites FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE notification_preferences ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS own_preferences ON notification_preferences;
CREATE POLICY own_preferences ON notification_preferences
    FOR ALL USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
DROP POLICY IF EXISTS dev_access ON notification_preferences;
CREATE POLICY dev_access ON notification_preferences FOR ALL USING (is_dev()) WITH CHECK (is_dev());


-- --- Dev-only tables ---------------------------------------------------------
-- No school may read these through the Supabase client at all. api_keys holds
-- encrypted provider keys; the others are cross-tenant operational data.

DO $$
DECLARE
    t TEXT;
    tables TEXT[] := ARRAY[
        'settings', 'api_keys', 'error_log', 'pipeline_log', 'credit_packages'
    ];
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS dev_access ON %I', t);
        EXECUTE format($f$
            CREATE POLICY dev_access ON %I FOR ALL USING (is_dev()) WITH CHECK (is_dev())
        $f$, t);
    END LOOP;
END $$;

-- credit_packages is the price list shown on the billing page, so it is
-- readable by any signed-in user. Writes stay dev-only via the policy above.
DROP POLICY IF EXISTS packages_readable ON credit_packages;
CREATE POLICY packages_readable ON credit_packages
    FOR SELECT USING (auth.uid() IS NOT NULL);
