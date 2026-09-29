-- Two editions a day: morning (after the 04:00 scan) and afternoon (after the 13:00 scan).
-- Each covers only what was posted since the edition before it. Older whole-day summaries keep edition = null.
alter table episodes add column edition text;
alter table episodes add column window_start timestamptz;
alter table episodes add column window_end timestamptz;
drop index if exists episodes_day_idx;
create unique index episodes_day_edition_idx on episodes (day, (coalesce(edition, 'day'))) where day is not null;
