-- =============================================================================
-- KLASSER AI - 009 JOB QUEUE
--
-- A durable work queue in Postgres, replacing FastAPI background_tasks.
--
-- BUILD_ORDER specifies Redis + RQ. RQ forks workers with os.fork() and does
-- not run on Windows, and it is synchronous while the whole pipeline is async.
-- Postgres already stores every other piece of state, supports
-- FOR UPDATE SKIP LOCKED for safe concurrent claiming, and LISTEN/NOTIFY for
-- immediate pickup - so the queue costs no new infrastructure and survives a
-- restart, which background_tasks does not.
--
-- Separate from generation_jobs: that row tracks the progress of one attempt,
-- and the retry loop creates attempts as it goes. The queued unit of work is
-- the timetable, which exists before any attempt does.
-- =============================================================================

CREATE TABLE IF NOT EXISTS job_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        TEXT NOT NULL,                      -- 'generate'
    school_id       UUID NOT NULL REFERENCES schools(id),
    timetable_id    UUID REFERENCES timetables(id),
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Lower runs sooner. 10 = urgent (a blocked school), 100 = standard.
    priority        INTEGER NOT NULL DEFAULT 100,

    status          TEXT NOT NULL DEFAULT 'queued',
                    -- 'queued' | 'running' | 'complete' | 'failed' | 'cancelled'
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,

    -- Set in the future to delay a retry (exponential backoff).
    available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    claimed_at      TIMESTAMPTZ,
    claimed_by      TEXT,
    -- Updated while a job runs. A row whose heartbeat has gone stale is
    -- assumed dead and requeued, which is how a killed worker recovers.
    heartbeat_at    TIMESTAMPTZ,

    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,

    CONSTRAINT job_queue_status_check
        CHECK (status IN ('queued','running','complete','failed','cancelled'))
);

-- The claim query orders by priority then age, filtered to runnable rows.
CREATE INDEX IF NOT EXISTS idx_job_queue_claimable
    ON job_queue (priority, available_at, created_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_job_queue_running
    ON job_queue (heartbeat_at) WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_job_queue_school
    ON job_queue (school_id, created_at DESC);

-- One live job per timetable: a double-submitted generation would otherwise
-- run twice and charge twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_queue_one_live_per_timetable
    ON job_queue (timetable_id)
    WHERE status IN ('queued', 'running') AND timetable_id IS NOT NULL;

ALTER TABLE job_queue ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS school_isolation ON job_queue;
CREATE POLICY school_isolation ON job_queue
    USING (school_id = current_school_id());

DROP POLICY IF EXISTS dev_access ON job_queue;
CREATE POLICY dev_access ON job_queue
    USING ((SELECT role FROM users WHERE id = auth.uid()) = 'dev');

INSERT INTO settings (key, value, category, description) VALUES
('queue_heartbeat_seconds', '30', 'queue',
 'How often a running job refreshes its heartbeat'),
('queue_stale_seconds', '180', 'queue',
 'A running job with no heartbeat for this long is requeued'),
('queue_poll_seconds', '5', 'queue',
 'Fallback poll interval when no NOTIFY arrives'),
('queue_max_concurrent', '2', 'queue',
 'Generations one worker will run at once')
ON CONFLICT (key) DO NOTHING;
