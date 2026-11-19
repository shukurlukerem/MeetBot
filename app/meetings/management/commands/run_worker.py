"""The single long-running loop: calendar → bots, finished calls → summaries, and self-healing."""

import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from meetings import pipeline
from meetings.scheduling import ConfigurationError, sync_calendar, transcription_settings

logger = logging.getLogger("meetings.worker")

TICK_SECONDS = 5
RECONCILE_EVERY = 60


class Command(BaseCommand):
    help = "Runs the Meetbot worker loop (calendar sync, summarization, bot reconciliation)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run every job once and exit (for cron or debugging).")

    def handle(self, *args, **options):
        self.running = True
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        conf = settings.MEETBOT
        if not conf["ATTENDEE_API_KEY"]:
            logger.warning("ATTENDEE_API_KEY is empty — run scripts/bootstrap.sh first")
        if not conf["SABAH_DATABASE_URL"]:
            logger.warning("SABAH_DB_HOST/SABAH_DB_USER are empty — calendar sync is off, only API-created meetings run")
        try:
            logger.info("Transcription: %s", transcription_settings(conf))
        except ConfigurationError as exc:
            logger.error("%s — bots cannot be created until this is fixed", exc)

        last_sync = last_reconcile = 0.0
        while self.running:
            close_old_connections()
            now_mono = time.monotonic()
            self._safely("recover", pipeline.recover_stale)
            if now_mono - last_sync >= conf["SYNC_INTERVAL_SECONDS"] or options["once"]:
                last_sync = now_mono
                result = self._safely("calendar sync", sync_calendar)
                if result and (result.scheduled or result.cancelled):
                    logger.info(
                        "Calendar sync: %s events, %s bots scheduled, %s cancelled",
                        result.fetched,
                        len(result.scheduled),
                        len(result.cancelled),
                    )
            self._safely("schedule due bots", pipeline.schedule_due)
            if now_mono - last_reconcile >= RECONCILE_EVERY or options["once"]:
                last_reconcile = now_mono
                self._safely("reconcile", pipeline.reconcile_bots)
            done = self._safely("summaries", pipeline.process_due_meetings) or []
            for meeting in done:
                logger.info("Meeting %s → %s", meeting.uid, meeting.status)
            if options["once"]:
                break
            time.sleep(TICK_SECONDS)
        logger.info("Worker stopped at %s", timezone.now())

    def _stop(self, *_):
        logger.info("Stopping after the current tick…")
        self.running = False

    def _safely(self, name, fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — one failing job must not kill the loop
            logger.exception("Worker job '%s' failed", name)
            return None
