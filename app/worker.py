"""Scheduler. Runs scans on SCAN_CRON (default 04:00 and 13:00 UK time) and picks up
"scan now" requests from the web app.

  python -m app.worker          run forever
  python -m app.worker --once   run one scan now and exit
"""
from __future__ import annotations

import argparse
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .db import conn
from .pipeline.run import run_scan
from .seed import bootstrap

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("news.worker")


def scheduled() -> None:
    run_scan("schedule")


def check_requests() -> None:
    with conn() as c:
        row = c.execute(
            """update scan_requests set picked_up_at = now()
               where id = (select id from scan_requests where picked_up_at is null order by id limit 1)
               returning requested_by"""
        ).fetchone()
    if row:
        log.info("scan requested by %s", row["requested_by"])
        run_scan(f"manual:{row['requested_by'] or 'unknown'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    args = parser.parse_args()
    bootstrap()
    if args.once:
        print(run_scan("cli"))
        return
    sched = BlockingScheduler(timezone=settings.timezone)
    sched.add_job(scheduled, CronTrigger.from_crontab(settings.scan_cron, timezone=settings.timezone),
                  id="scan", max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.add_job(check_requests, "interval", seconds=30, id="requests", max_instances=1, coalesce=True)
    if settings.podcast_enabled:
        from .podcast import make_episode

        sched.add_job(make_episode, CronTrigger.from_crontab(settings.podcast_cron, timezone=settings.timezone),
                      id="podcast", max_instances=1, coalesce=True, misfire_grace_time=3600)
    log.info("worker up, scans on '%s' (%s)", settings.scan_cron, settings.timezone)
    sched.start()


if __name__ == "__main__":
    main()
