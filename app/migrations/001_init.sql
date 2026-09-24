create extension if not exists pg_trgm;

-- Where items come from. Seeded from topics/*/sources.json. Discovery adds 'proposed' rows.
create table sources (
  id serial primary key,
  topic text not null,
  name text not null,
  url text not null unique,           -- feed URL, HN query key, or homepage for proposed sources
  homepage text,
  kind text not null,                 -- rss | hn | proposed
  tier smallint not null default 1,   -- 1 = on-topic source, 2 = broad source, gated strictly
  query text,                         -- for kind = hn
  min_points int,                     -- for kind = hn
  status text not null default 'active',   -- active | paused | proposed | dismissed
  times_seen int not null default 0,       -- discovery hits, for proposed sources
  fail_count int not null default 0,
  last_fetched_at timestamptz,
  last_error text,
  created_at timestamptz not null default now()
);

-- One row per scan.
create table runs (
  id serial primary key,
  trigger text not null default 'schedule',
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running',  -- running | ok | error
  stats jsonb not null default '{}',
  error text
);

-- Admin "scan now" button writes here, the worker picks it up.
create table scan_requests (
  id serial primary key,
  requested_by text,
  requested_at timestamptz not null default now(),
  picked_up_at timestamptz
);

-- Every item any scan found, kept or not. This is the audit trail for tuning the curator.
create table candidates (
  id bigserial primary key,
  run_id int references runs(id) on delete set null,
  topic text not null,
  url text not null,
  canonical_url text not null unique,
  title text not null,
  excerpt text,
  source_name text,
  source_id int references sources(id) on delete set null,
  tier smallint not null default 1,
  published_at timestamptz,
  found_via text not null,            -- feed | hn | search
  decision text not null default 'pending',  -- pending | kept | rejected | skipped | post | coverage
  reason text,
  post_id bigint,
  created_at timestamptz not null default now()
);
create index candidates_run_idx on candidates (run_id, decision);

create table tags (
  id serial primary key,
  slug text not null unique,
  label text not null,
  type text not null,                 -- topic | category | lens | story | entity | user
  description text,
  created_by text not null default 'system',
  created_at timestamptz not null default now()
);
create index tags_slug_trgm on tags using gin (slug gin_trgm_ops);
create index tags_label_trgm on tags using gin (label gin_trgm_ops);

-- A story is one real-world event. Posts and coverage hang off it.
create table stories (
  id bigserial primary key,
  headline text not null,
  keywords text[] not null default '{}',
  proposed_tag text,                  -- slug to use if the story gets big
  story_tag_id int references tags(id) on delete set null,
  article_count int not null default 0,
  first_seen_at timestamptz not null default now(),
  last_update_at timestamptz not null default now()
);
create index stories_headline_trgm on stories using gin (headline gin_trgm_ops);
create index stories_last_update_idx on stories (last_update_at desc);

create table posts (
  id bigserial primary key,
  story_id bigint references stories(id) on delete set null,
  url text not null,
  canonical_url text not null unique,
  title text not null,
  summary text not null,
  delta text,                         -- follow-ups: what's new versus earlier posts in the story
  source_name text,
  content_type text not null default 'article',  -- article | official | podcast | video | paper | repo | discussion | legislation
  published_at timestamptz,
  posted_at timestamptz not null default now(),
  feed_at timestamptz not null default now(),    -- what the feed sorts and groups by
  importance smallint not null default 2,        -- 1 routine .. 5 field-defining
  lens_score smallint not null default 0,        -- 0 .. 3, topic lens relevance (the Bauer boost for ai)
  legislation_stage text,             -- proposed | passed | in_force | enforcement | guidance
  jurisdiction text,
  posted_by text not null default 'curator',     -- 'curator' or a user email once public posting is on
  hidden boolean not null default false,
  search tsvector generated always as (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(summary, '') || ' ' || coalesce(delta, '')), 'B') ||
    setweight(to_tsvector('simple', coalesce(source_name, '')), 'C')
  ) stored
);
create index posts_search_idx on posts using gin (search);
create index posts_feed_at_idx on posts (feed_at desc);
create index posts_story_idx on posts (story_id);

-- Other outlets covering a story without adding anything new. Shown as "N more sources".
create table coverage (
  id bigserial primary key,
  story_id bigint references stories(id) on delete cascade,
  post_id bigint references posts(id) on delete cascade,
  url text not null,
  canonical_url text not null unique,
  title text not null,
  source_name text,
  published_at timestamptz,
  created_at timestamptz not null default now()
);
create index coverage_post_idx on coverage (post_id);

create table post_tags (
  post_id bigint not null references posts(id) on delete cascade,
  tag_id int not null references tags(id) on delete cascade,
  added_by text not null default 'curator',
  created_at timestamptz not null default now(),
  primary key (post_id, tag_id)
);
create index post_tags_tag_idx on post_tags (tag_id);

create table votes (
  post_id bigint not null references posts(id) on delete cascade,
  user_email text not null,
  created_at timestamptz not null default now(),
  primary key (post_id, user_email)
);

-- "Not relevant" flags. Fed back into the curator prompt.
create table feedback (
  post_id bigint not null references posts(id) on delete cascade,
  user_email text not null,
  kind text not null default 'not_relevant',
  created_at timestamptz not null default now(),
  primary key (post_id, user_email, kind)
);

create table comments (
  id bigserial primary key,
  post_id bigint not null references posts(id) on delete cascade,
  user_email text not null,
  body text not null,
  created_at timestamptz not null default now()
);
create index comments_post_idx on comments (post_id, created_at);
