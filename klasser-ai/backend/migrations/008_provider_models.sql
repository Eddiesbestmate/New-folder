-- =============================================================================
-- KLASSER AI - 008 NEW PROVIDER MODEL STRINGS
--
-- Adds settings rows for two providers that were not in the original design:
--
--   google    - Gemini on a Google AI Studio key. Gemini through OpenRouter is
--               billed (402 without credits); the same models on an AI Studio
--               key have a free tier.
--   anthropic - Claude, replacing Magistral for the allocation layers now that
--               Mistral's free tier is discontinued.
--
-- Assignments are NOT changed here. Point tasks at these with
-- `python admin.py set-model` once the corresponding key is stored, so the
-- database never references a model with no key behind it.
-- =============================================================================

INSERT INTO settings (key, value, category, description) VALUES
('model_gemini_free', 'gemini-2.5-flash', 'model',
 'Gemini on a Google AI Studio key (free tier)'),
('model_claude',      'claude-opus-5', 'model',
 'Claude for judgement work - interpretation, editing, validation'),
('model_claude_fast', 'claude-haiku-4-5', 'model',
 'Cheaper Claude for the bulk allocation layers')
ON CONFLICT (key) DO NOTHING;
