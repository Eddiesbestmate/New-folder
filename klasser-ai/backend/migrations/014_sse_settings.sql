-- =============================================================================
-- KLASSER AI - 014 LIVE PROGRESS STREAM SETTINGS
--
-- The SSE progress stream polled every second and ran until its job reached a
-- terminal state. If that never happened - a worker killed mid-run, a job row
-- removed - it polled forever for as long as the tab stayed open.
--
-- Both numbers belong in settings rather than in the code: the poll interval is
-- the main cost of watching a generation, and the cap is the kind of thing that
-- wants changing under load without a redeploy.
-- =============================================================================

INSERT INTO settings (key, value, category, description) VALUES
('sse_max_seconds',  '1800', 'timetable',
 'Maximum lifetime of one live progress stream before it closes and asks the page to reconnect'),
('sse_poll_seconds', '1',    'timetable',
 'How often a live progress stream checks for new events')
ON CONFLICT (key) DO NOTHING;
