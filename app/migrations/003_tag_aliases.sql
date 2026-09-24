-- Other words a tag answers to, so typing "hack" in the filter box finds #security.
alter table tags add column aliases text[] not null default '{}';
