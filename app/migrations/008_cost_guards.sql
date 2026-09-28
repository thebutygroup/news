-- Track text-to-speech usage so a monthly cap can stop runaway audio costs.
alter table episodes add column tts_provider text;
alter table episodes add column tts_chars int;
