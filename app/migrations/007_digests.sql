-- One row per day: a written summary of that day's stories, plus its audio.
alter table episodes add column day date;
alter table episodes add column headline text;
alter table episodes add column summary jsonb;       -- [{"text": ..., "post_ids": [...]}]
alter table episodes add column summary_model text;
alter table episodes add column sources jsonb;       -- [{"id", "title", "url", "source"}] cited in the summary
alter table episodes add column audio_error text;
create unique index episodes_day_idx on episodes (day) where day is not null;
