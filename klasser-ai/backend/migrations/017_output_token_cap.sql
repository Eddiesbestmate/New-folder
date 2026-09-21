-- =============================================================================
-- KLASSER AI - 017 A DEFAULT OUTPUT TOKEN BUDGET
--
-- Calls used to go out with no max_tokens at all. That is not a neutral
-- choice: OpenRouter reserves credit for the model's entire context window
-- when the field is absent and refuses the request -
--
--     402: This request requires more credits, or fewer max_tokens
--
-- so every pipeline call to Gemini through OpenRouter failed, while
-- `admin.py test-all` reported it healthy - the connectivity test passes
-- max_tokens=800 and therefore never hit it. Five tasks route through that
-- model.
--
-- 8000 is chosen to be larger than any response the pipeline actually
-- produces, while still bounding a runaway one. A caller that needs more says
-- so explicitly.
-- =============================================================================

INSERT INTO settings (key, value, category, description) VALUES
('ai_max_output_tokens', '8000', 'ai_assignment',
 'Default max_tokens for an AI call when the caller does not set one. Absent, OpenRouter reserves the whole context window and returns 402.')
ON CONFLICT (key) DO NOTHING;
