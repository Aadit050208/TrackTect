"""Autonomous scheduled tracking via APScheduler (in-process, no external service).

Every active (non-paused) competitor gets an interval job that re-runs the
full pipeline. Separately, weekly and optional daily digest jobs generate
Markdown digests for users who opted in.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

import db
import db_connection
from backend_logic import run_and_store
from digests import generate_daily_for_enabled_users, generate_for_all_enabled_users

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _job(competitor_id: int) -> None:
    # Own Turso client on the scheduler thread — never the Flask request client.
    with db_connection.thread_db_scope():
        competitor = db.get_competitor(competitor_id)
        if competitor is None or not competitor["active"] or competitor["paused"]:
            logger.info("Competitor %s inactive/paused; skipping scheduled run", competitor_id)
            return
        logger.info("Scheduled run starting for competitor '%s'", competitor["name"])
        run_and_store(dict(competitor), trigger="scheduled")


def schedule_competitor(competitor_id: int, interval_hours: int) -> None:
    """Add or replace the recurring job for one competitor."""
    scheduler.add_job(
        _job,
        trigger="interval",
        hours=max(1, int(interval_hours)),
        args=[competitor_id],
        id=f"competitor-{competitor_id}",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduled competitor %s every %sh", competitor_id, interval_hours)


def unschedule_competitor(competitor_id: int) -> None:
    job_id = f"competitor-{competitor_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("Unscheduled competitor %s", competitor_id)


def _weekly_digests_job() -> None:
    with db_connection.thread_db_scope():
        generate_for_all_enabled_users()


def _daily_digests_job() -> None:
    with db_connection.thread_db_scope():
        generate_daily_for_enabled_users()


def schedule_weekly_digests() -> None:
    """Cron: generate digests every Monday 09:00 UTC for opted-in users."""
    scheduler.add_job(
        _weekly_digests_job,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=0,
        id="weekly-digests",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduled weekly digest generation (Mon 09:00 UTC)")


def schedule_daily_digests() -> None:
    """Cron: daily digests at 08:00 UTC for users with digest_daily enabled."""
    scheduler.add_job(
        _daily_digests_job,
        trigger="cron",
        hour=8,
        minute=0,
        id="daily-digests",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("Scheduled daily digest generation (08:00 UTC)")


def start() -> None:
    """Schedule all active competitors + digests and start the background scheduler."""
    if scheduler.running:
        return
    with db_connection.thread_db_scope():
        for competitor in db.get_all_active_competitors():
            schedule_competitor(competitor["id"], competitor["interval_hours"])
    schedule_weekly_digests()
    schedule_daily_digests()
    scheduler.start()
    logger.info("Scheduler started with %d job(s)", len(scheduler.get_jobs()))
