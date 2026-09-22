-- =============================================================================
-- KLASSER AI - 019 AI SLOT PROPOSALS OFF BY DEFAULT
--
-- layer3_period_slots asked the model for a slot suggestion per class before
-- placing anything, chunked one call per campus. That made sense when the
-- proposals were the only placement logic layer 3 had. They no longer are:
-- migration and code changes on 2026-09-22 added a deterministic packer that
-- guarantees every class its minimum first, and the AI's suggestion is now
-- consulted only for the best-effort top-up above that minimum.
--
-- Measured against three real generations after that change, the packer
-- alone placed every class cleanly with no AI input. The proposal call was
-- still 16.8% of total wall-clock time - the single most expensive step
-- outside layer2 and layer10 - for influence that no longer shaped whether
-- the timetable succeeded.
--
-- Off by default. A school that wants the model's opinion on slot placement
-- can switch it back on from the dev portal and pay the time for it knowingly.
-- =============================================================================

INSERT INTO settings (key, value, category, description) VALUES
('layer3_use_ai_proposals', 'false', 'timetable',
 'Ask the AI to suggest period slots before the deterministic packer runs. Off by default: the packer now guarantees every class its minimum on its own, so this call only shapes best-effort top-up periods and was measured to add ~17% of total generation time for that alone.')
ON CONFLICT (key) DO NOTHING;
