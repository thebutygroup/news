-- Daily audio briefing. Audio files live in MEDIA_DIR (a Docker volume), never in git.
create table episodes (
  id serial primary key,
  title text not null,
  script jsonb not null,             -- [{speaker, text}]
  post_ids bigint[] not null default '{}',
  notes text not null default '',    -- plain-text show notes: each story with its link
  audio_file text,                   -- file name inside MEDIA_DIR
  bytes int,
  duration_seconds int,
  status text not null default 'ok', -- ok | error
  error text,
  created_at timestamptz not null default now()
);
create index episodes_created_idx on episodes (created_at desc);
