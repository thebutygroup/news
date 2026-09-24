-- Known sources: organisations we follow, and every channel we know for each of them.

create table orgs (
  id serial primary key,
  slug text not null unique,
  name text not null,
  kind text not null default 'company',     -- company | lab | regulator | publication | person | community
  homepage text,
  priority text not null default 'core',    -- core: checked every scan | watch: checked once a day
  status text not null default 'following', -- following | proposed | dismissed
  topics text[] not null default '{}',
  aliases text[] not null default '{}',     -- other names the curator may use, e.g. google for alphabet
  default_tier smallint not null default 2,
  notes text,
  tag_id int references tags(id) on delete set null,  -- the entity tag posts about this org carry
  mention_count int not null default 0,     -- posts tagged with this org in the last 30 days
  search_hits int not null default 0,       -- times web search landed on this org's site
  last_mentioned_at timestamptz,
  last_swept_at timestamptz,                -- last time web search was asked to check this org
  channels_discovered_at timestamptz,       -- last time we read the homepage for feeds and socials
  created_by text not null default 'seed',  -- seed | relevance | search | a user email
  created_at timestamptz not null default now()
);
create index orgs_status_idx on orgs (status);

alter table sources alter column topic drop not null;
alter table sources add column org_id int references orgs(id) on delete cascade;
alter table sources add column channel text not null default 'blog';
  -- newsroom | blog | research | substack | podcast | youtube | x | linkedin | bluesky
  -- | mastodon | threads | instagram | github | community | search-feed | other
alter table sources add column origin text not null default 'seed';  -- seed | discovered | added
alter table sources add column external_id text;     -- e.g. an X user id
alter table sources add column cursor text;          -- e.g. the newest X post id we have seen
alter table sources add column etag text;
alter table sources add column last_modified text;
alter table sources add column link_pattern text;    -- page sources: regex an article link must match
alter table sources add column check_every_hours int not null default 0;  -- 0 = every scan
alter table sources add column last_new_item_at timestamptz;
alter table sources add column verified_at timestamptz;
create index sources_org_idx on sources (org_id);

-- kind is now "how we read it": rss | page | hn | x | none (none = stored as a link only)
update sources set channel = 'community' where kind = 'hn';

-- Proposed sources from the first version become proposed organisations.
insert into orgs (slug, name, kind, homepage, status, search_hits, created_by)
select regexp_replace(regexp_replace(lower(coalesce(homepage, url)), '^https?://(www\.)?', ''), '[^a-z0-9]+', '-', 'g'),
       name, 'publication', coalesce(homepage, url), 'proposed', times_seen, 'search'
from sources where status = 'proposed'
on conflict (slug) do nothing;
delete from sources where status = 'proposed';

-- Page sources: every article link already seen on the listing page, so only new ones count.
create table source_links (
  source_id int not null references sources(id) on delete cascade,
  canonical_url text not null,
  first_seen_at timestamptz not null default now(),
  primary key (source_id, canonical_url)
);

alter table candidates add column org_id int references orgs(id) on delete set null;
create index candidates_source_idx on candidates (source_id, created_at);
