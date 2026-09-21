-- =============================================================================
-- KLASSER AI - 011 CLOSING THE RLS GAPS
--
-- Seventeen tables were left without Row Level Security by migration 002. They
-- are the ones with no school_id of their own - pipeline internals, the class
-- group link tables, and invoice lines - and 002 covers tables by name, so
-- anything not on its list was simply never protected.
--
-- This was not theoretical. An anonymous caller holding only the publishable
-- anon key - the key the frontend ships to every browser - could read them:
--
--     anon GET /rest/v1/generation_events -> HTTP 200, 1 row
--
-- They looked safe in testing only because the suites clean up after
-- themselves, so the tables are empty between runs. A real school generating a
-- real timetable fills them with the mapping of which student sits in which
-- class at which time.
--
-- check_rls_leak.py reported PASS throughout, because it walks a hand-written
-- list of table names that did not include any of these. It now enumerates the
-- tables from the database instead.
--
-- None of these tables has a school_id column, so each is scoped through the
-- row that owns it, the way 002 already handles timetable_entries.
-- =============================================================================

-- --- Scoped through the generation attempt ----------------------------------
-- attempt -> timetable -> school. One predicate, twelve tables.

DO $$
DECLARE
    t TEXT;
    tables TEXT[] := ARRAY[
        'allocation_registry_students', 'allocation_registry_teachers',
        'allocation_registry_rooms', 'allocation_registry_buses',
        'allocation_registry_duties', 'allocation_registry_duty_counts',
        'class_consistency_registry', 'generation_events', 'generation_jobs',
        'partial_solutions', 'pipeline_state', 'validation_results'
    ];
    predicate TEXT := $p$
        attempt_id IN (
            SELECT ga.id FROM generation_attempts ga
            JOIN timetables tt ON tt.id = ga.timetable_id
            WHERE tt.school_id = current_school_id())
    $p$;
BEGIN
    FOREACH t IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);

        EXECUTE format('DROP POLICY IF EXISTS school_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY school_isolation ON %I FOR ALL USING (%s) '
            'WITH CHECK (%s)', t, predicate, predicate);

        EXECUTE format('DROP POLICY IF EXISTS dev_access ON %I', t);
        EXECUTE format(
            'CREATE POLICY dev_access ON %I FOR ALL USING (is_dev()) '
            'WITH CHECK (is_dev())', t);
    END LOOP;
END $$;


-- --- Scoped through their own parent ----------------------------------------

-- class_groups already carries school_id.
ALTER TABLE class_group_students ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON class_group_students;
CREATE POLICY school_isolation ON class_group_students
    FOR ALL
    USING (class_group_id IN (
        SELECT id FROM class_groups WHERE school_id = current_school_id()))
    WITH CHECK (class_group_id IN (
        SELECT id FROM class_groups WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON class_group_students;
CREATE POLICY dev_access ON class_group_students
    FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE class_group_explicit_students ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON class_group_explicit_students;
CREATE POLICY school_isolation ON class_group_explicit_students
    FOR ALL
    USING (class_group_def_id IN (
        SELECT id FROM class_group_definitions
        WHERE school_id = current_school_id()))
    WITH CHECK (class_group_def_id IN (
        SELECT id FROM class_group_definitions
        WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON class_group_explicit_students;
CREATE POLICY dev_access ON class_group_explicit_students
    FOR ALL USING (is_dev()) WITH CHECK (is_dev());

ALTER TABLE class_group_rules ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON class_group_rules;
CREATE POLICY school_isolation ON class_group_rules
    FOR ALL
    USING (class_group_def_id IN (
        SELECT id FROM class_group_definitions
        WHERE school_id = current_school_id()))
    WITH CHECK (class_group_def_id IN (
        SELECT id FROM class_group_definitions
        WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON class_group_rules;
CREATE POLICY dev_access ON class_group_rules
    FOR ALL USING (is_dev()) WITH CHECK (is_dev());

-- A school's billing detail, line by line.
ALTER TABLE invoice_line_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS school_isolation ON invoice_line_items;
CREATE POLICY school_isolation ON invoice_line_items
    FOR ALL
    USING (invoice_id IN (
        SELECT id FROM invoices WHERE school_id = current_school_id()))
    WITH CHECK (invoice_id IN (
        SELECT id FROM invoices WHERE school_id = current_school_id()));
DROP POLICY IF EXISTS dev_access ON invoice_line_items;
CREATE POLICY dev_access ON invoice_line_items
    FOR ALL USING (is_dev()) WITH CHECK (is_dev());


-- --- Internal ----------------------------------------------------------------
-- The migration ledger tells an attacker exactly which schema version is
-- deployed. No client has any reason to read it: RLS on, no policy, so only the
-- service role and the direct Postgres connection can see it.

ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;


-- --- Indexes supporting the new predicates -----------------------------------
-- Every policy above filters on the parent key, so each needs an index on it or
-- RLS turns cheap lookups into sequential scans on the biggest tables in the
-- schema.

CREATE INDEX IF NOT EXISTS idx_alloc_students_attempt
    ON allocation_registry_students (attempt_id);
CREATE INDEX IF NOT EXISTS idx_alloc_teachers_attempt
    ON allocation_registry_teachers (attempt_id);
CREATE INDEX IF NOT EXISTS idx_alloc_rooms_attempt
    ON allocation_registry_rooms (attempt_id);
CREATE INDEX IF NOT EXISTS idx_class_group_students_group
    ON class_group_students (class_group_id);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice
    ON invoice_line_items (invoice_id);
