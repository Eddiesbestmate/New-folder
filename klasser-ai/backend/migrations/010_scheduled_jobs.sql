-- =============================================================================
-- KLASSER AI - 010 SCHEDULED JOBS
--
-- Phase 10 adds three jobs that run on a clock rather than on a request:
-- daily interest accrual, monthly invoice generation, and daily invoice
-- enforcement (BILLING.md).
--
-- All three are destructive if they run twice. Interest accrued twice in a day
-- overcharges a school; a second invoice for the same month bills them again.
-- Several workers may be running, a process may restart just after a job fired,
-- and a dev may trigger one by hand from the portal - so "once per day" cannot
-- rely on the scheduler firing once.
--
-- This table is the guard. A job claims its slot with
-- INSERT ... ON CONFLICT DO NOTHING: exactly one caller inserts the row for a
-- given (job, date) and does the work, and every other caller sees zero rows
-- affected and stops. It doubles as the run history - what ran, when, and what
-- it did.
-- =============================================================================

CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    job_name        TEXT NOT NULL,
    run_date        DATE NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',
                    -- 'running' | 'complete' | 'failed'
    triggered_by    TEXT NOT NULL DEFAULT 'scheduler',
    summary         JSONB,
    error           TEXT,
    PRIMARY KEY (job_name, run_date)
);

-- The enforcement job reads every unpaid invoice past its due date daily.
CREATE INDEX IF NOT EXISTS idx_invoices_due
    ON invoices (due_date) WHERE status IN ('sent', 'overdue');

-- The interest job reads every outstanding charge daily.
CREATE INDEX IF NOT EXISTS idx_outstanding_open
    ON outstanding_charges (school_id) WHERE status = 'outstanding';

-- One invoice per school per billing period. The monthly job checks for an
-- existing invoice before creating one, but a unique index is what actually
-- prevents a double bill if two callers pass that check at the same moment.
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_school_period
    ON invoices (school_id, period_start);

-- A PAYG charge is attempted once per generation. Retrying a failed charge
-- updates the existing row rather than writing a second one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payg_charge_timetable
    ON payg_charge_log (timetable_id);

-- Tracks the last day interest was applied to a charge, so accrual is correct
-- even if a day is missed (a deploy, an outage) - the job charges for the days
-- that actually elapsed rather than assuming exactly one.
ALTER TABLE outstanding_charges
    ADD COLUMN IF NOT EXISTS last_interest_date DATE;

-- Scheduled jobs run as the service role only; no school ever reads this table.
ALTER TABLE scheduled_job_runs ENABLE ROW LEVEL SECURITY;
