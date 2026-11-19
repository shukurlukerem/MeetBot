"""After the call: final transcript → Claude summary → delivery. Plus self-healing for missed webhooks."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import delivery, summarizer
from .attendee import AttendeeClient, AttendeeError
from .models import Meeting, Utterance
from .scheduling import STATE_FIELDS, apply_bot_state, backoff, schedule_bot

logger = logging.getLogger(__name__)

STALE_SUMMARIZING = timedelta(minutes=20)
POLL_IF_SILENT_FOR = timedelta(minutes=3)


def store_transcript(meeting, entries):
    """Replace the webhook-collected lines with Attendee's final transcript (the source of truth)."""
    rows = []
    for entry in entries:
        text = ((entry.get("transcription") or {}).get("transcript") or "").strip()
        if not text:
            continue
        rows.append(
            Utterance(
                meeting=meeting,
                speaker_name=(entry.get("speaker_name") or "")[:200],
                speaker_uuid=(entry.get("speaker_uuid") or "")[:200],
                timestamp_ms=entry.get("timestamp_ms") or 0,
                duration_ms=entry.get("duration_ms") or 0,
                text=text,
            )
        )
    if not rows:
        return 0
    with transaction.atomic():
        meeting.utterances.all().delete()
        Utterance.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def refresh_transcript(meeting, client):
    if not meeting.bot_id:
        return 0
    try:
        return store_transcript(meeting, client.get_transcript(meeting.bot_id))
    except AttendeeError as exc:
        # Keep the lines that arrived by webhook during the call.
        logger.warning("Final transcript fetch failed for %s, using webhook copy: %s", meeting.uid, exc)
        return 0


def summarize_meeting(meeting, client=None, claude=None):
    """Fetch the transcript and write the summary. Raises on retryable failure."""
    client = client or AttendeeClient()
    refresh_transcript(meeting, client)
    utterances = list(meeting.utterances.all())
    if not utterances:
        meeting.status = Meeting.Status.NO_TRANSCRIPT
        reason = meeting.bot_event or meeting.bot_state or "no speech captured"
        meeting.status_detail = f"Görüşdə danışıq qeydə alınmadı ({reason})"[:300]
        meeting.next_attempt_at = None
        meeting.save()
        return meeting

    data, markdown, result = summarizer.summarize(
        meeting, utterances, instructions=meeting.summary_instructions, client=claude
    )
    meeting.summary = data
    meeting.summary_markdown = markdown
    meeting.summary_model = result.model
    meeting.summary_usage = result.usage
    meeting.summarized_at = timezone.now()
    meeting.status = Meeting.Status.DONE
    meeting.status_detail = ""
    meeting.attempts = 0
    meeting.next_attempt_at = None
    meeting.last_error = ""
    meeting.save()
    if meeting.delivered_at is None:  # a re-run after an edit does not mail everyone again
        delivery.deliver(meeting)
    return meeting


def process_meeting(meeting, client=None, claude=None):
    try:
        summarize_meeting(meeting, client=client, claude=claude)
    except summarizer.SummaryRefused as exc:
        meeting.status = Meeting.Status.FAILED
        meeting.status_detail = "Claude xülasəni rədd etdi"
        meeting.last_error = str(exc)
        meeting.next_attempt_at = None
        meeting.save()
    except Exception as exc:  # noqa: BLE001 — any failure is retried with backoff, then parked as FAILED
        logger.exception("Summarizing meeting %s failed", meeting.uid)
        meeting.attempts += 1
        meeting.last_error = str(exc)[:4000]
        if meeting.attempts >= settings.MEETBOT["MAX_ATTEMPTS"]:
            meeting.status = Meeting.Status.FAILED
            meeting.status_detail = "Xülasə hazırlana bilmədi"
            meeting.next_attempt_at = None
        else:
            meeting.status = Meeting.Status.PROCESSING
            meeting.next_attempt_at = timezone.now() + backoff(meeting.attempts)
        meeting.save()
    return meeting


def process_due_meetings(now=None, client=None, claude=None, limit=5):
    now = now or timezone.now()
    due = (
        Meeting.objects.filter(status=Meeting.Status.PROCESSING)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("next_attempt_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    done = []
    for meeting_id in list(due):
        # Conditional UPDATE = claim; a second worker gets 0 rows and moves on.
        if not Meeting.objects.filter(id=meeting_id, status=Meeting.Status.PROCESSING).update(
            status=Meeting.Status.SUMMARIZING, updated_at=now
        ):
            continue
        meeting = Meeting.objects.get(id=meeting_id)
        process_meeting(meeting, client=client, claude=claude)
        done.append(meeting)
    return done


def recover_stale(now=None):
    """A worker that died mid-summary leaves SUMMARIZING behind; put it back in the queue."""
    now = now or timezone.now()
    return Meeting.objects.filter(status=Meeting.Status.SUMMARIZING, updated_at__lt=now - STALE_SUMMARIZING).update(
        status=Meeting.Status.PROCESSING, next_attempt_at=now
    )


def reconcile_bots(now=None, client=None, limit=50):
    """Poll Attendee for bots we have not heard from lately (a missed webhook must not strand a meeting)."""
    now = now or timezone.now()
    client = client or AttendeeClient()
    silent = Meeting.objects.filter(
        status__in=[Meeting.Status.SCHEDULED, Meeting.Status.IN_MEETING],
        bot_id__isnull=False,
        updated_at__lt=now - POLL_IF_SILENT_FOR,
    ).order_by("updated_at")[:limit]
    changed = 0
    for meeting in silent:
        try:
            bot = client.get_bot(meeting.bot_id)
        except AttendeeError as exc:
            logger.warning("Polling bot %s failed: %s", meeting.bot_id, exc)
            if exc.status_code == 404:
                meeting.status = Meeting.Status.PROCESSING
                meeting.next_attempt_at = now
                meeting.status_detail = "Bot Attendee-də tapılmadı"
            meeting.save(update_fields=STATE_FIELDS)
            continue
        events = bot.get("events") or []
        last = events[-1] if events else {}
        before = meeting.status
        apply_bot_state(meeting, bot.get("state", ""), last.get("type", ""), last.get("sub_type", ""), save=False)
        # A bot still "alive" long after the meeting should have ended is stuck; summarize what we have.
        hard_stop = (meeting.end or meeting.start) + timedelta(minutes=settings.MEETBOT["MAX_MEETING_MINUTES"] + 60)
        if meeting.status in (Meeting.Status.SCHEDULED, Meeting.Status.IN_MEETING) and now > hard_stop:
            meeting.status = Meeting.Status.PROCESSING
            meeting.next_attempt_at = now
            meeting.status_detail = "Bot cavab vermir — mövcud transkriptlə davam edilir"
        meeting.save(update_fields=STATE_FIELDS)  # also bumps updated_at → next poll only after another quiet spell
        changed += meeting.status != before
    return changed


def schedule_due(now=None, client=None, limit=50):
    """Create bots for PENDING meetings starting within the lookahead (and retry failed attempts).

    Meetings asked for far ahead (a whole recurring series) wait here as rows; Attendee only
    gets a bot once the meeting is near.
    """
    now = now or timezone.now()
    lookahead = timedelta(minutes=settings.MEETBOT["LOOKAHEAD_MINUTES"])
    due = (
        Meeting.objects.filter(status=Meeting.Status.PENDING, start__lte=now + lookahead)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("start")[:limit]
    )
    for meeting in due:
        schedule_bot(meeting, client=client, now=now)


def queue_resummarize(meeting, instructions=None):
    """Operator-triggered re-run (admin action / API); the worker picks it up within seconds."""
    if meeting.status in Meeting.ACTIVE_STATUSES or meeting.status == Meeting.Status.SUMMARIZING:
        raise ValueError("Görüş hələ bitməyib və ya xülasə artıq hazırlanır")
    if not meeting.bot_id:
        raise ValueError("Bu görüşə bot qoşulmayıb")
    if instructions is not None:
        meeting.summary_instructions = instructions.strip()[:2000]
    meeting.status = Meeting.Status.PROCESSING
    meeting.attempts = 0
    meeting.next_attempt_at = timezone.now()
    meeting.save()
    return meeting
