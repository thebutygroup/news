-- Summaries are now an inverted pyramid: `summary` is the short lead shown on the card,
-- `details` is the rest, shown behind "more".
alter table posts add column details text;
alter table posts drop column search;
alter table posts add column search tsvector generated always as (
  setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
  setweight(to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(delta, '')), 'B') ||
  setweight(to_tsvector('english', coalesce(details, '')), 'C') ||
  setweight(to_tsvector('simple', coalesce(source_name, '')), 'C')
) stored;
create index posts_search_idx on posts using gin (search);
