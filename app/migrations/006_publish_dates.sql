-- Posts sit on the day they were published, not the day we found them.
-- published_estimated marks posts whose source gave no date; those fall back to when we found them.
alter table posts add column published_estimated boolean not null default false;
update posts set feed_at = published_at
  where published_at is not null and published_at <= posted_at + interval '5 minutes';
update posts set published_estimated = true where published_at is null;
