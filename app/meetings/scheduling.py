"""Turns calendar meetings into Attendee bots and keeps their state in step."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .attendee import (
    FINISHED_STATES,
    IN_MEETING_STATES,
    PRE_JOIN_STATES,
    AttendeeClient,
    AttendeeError,
)
from .calendar_source import SabahCalendarSource, group_rows, make_dedup_key, normalize_meet_url, should_join
from .models import Meeting

logger = logging.getLogger(__name__)

# Attendee needs ~2 minutes to stage a scheduled bot; closer than that we join now.
MIN_SCHEDULE_LEAD = timedelta(minutes=2)
TRANSCRIPT_SETTLE = timedelta(seconds=10)


def backoff(attempts):
    return timedelta(seconds=min(30 * 2 ** max(attempts - 1, 0), 30 * 60))


class ConfigurationError(Exception):
    pass


# Attendee's ElevenLabs schema only accepts ISO 639-3 codes.
ISO_639_3 = {"az": "aze", "ru": "rus", "en": "eng", "tr": "tur", "uk": "ukr", "ka": "kat", "de": "deu", "fr": "fra"}


def transcription_settings(conf):
    """Attendee `transcription_settings` for the configured provider.

    Every provider except `captions` gets each participant's own audio stream, so speaker
    names stay exact; the provider's API key lives in Attendee (scripts/bootstrap_attendee.py).
    """
    if conf["TRANSCRIPTION_SETTINGS"]:
        return conf["TRANSCRIPTION_SETTINGS"]
    provider = conf["TRANSCRIPTION_PROVIDER"]
    languages = conf["LANGUAGES"]
    if provider == "gladia":
        # Restricting detection to our languages and allowing a switch per utterance is what makes
        # Azerbaijani/Russian/English meetings come out right.
        language_config = {"languages": languages}
        if len(languages) > 1:
            language_config["code_switching"] = True
        return {"gladia": {"model": "solaria-1", "language_config": language_config}}
    if provider == "elevenlabs":
        settings_ = {"model_id": "scribe_v2"}
        if len(languages) == 1:
            settings_["language_code"] = ISO_639_3.get(languages[0], languages[0])
        return {"elevenlabs": settings_}  # no language_code → detected per utterance
    if provider == "openai":
        settings_ = {"model": "gpt-4o-transcribe"}
        if len(languages) == 1:
            settings_["language"] = languages[0]
        else:
            names = {"az": "Azerbaijani", "ru": "Russian", "en": "English", "tr": "Turkish"}
            spoken = ", ".join(names.get(code, code) for code in languages)
            settings_["prompt"] = f"Business meeting. Speakers switch between {spoken}, sometimes mid-sentence."
        return {"openai": settings_}
    if provider == "deepgram":
        return {"deepgram": {"model": "nova-3", "language": "multi" if len(languages) > 1 else languages[0]}}
    if provider == "captions":
        # Google Meet's own captions: free, but one language for the whole meeting.
        return {
            "meeting_closed_captions": {
                "google_meet_language": conf["CAPTION_LANGUAGE"],
                "merge_consecutive_captions": True,
            }
        }
    raise ConfigurationError(f"Unknown MEETBOT_TRANSCRIPTION_PROVIDER '{provider}'")


def build_bot_payload(meeting, join_at=None, conf=None):
    conf = conf or settings.MEETBOT
    leave = {
        "max_uptime_seconds": conf["MAX_MEETING_MINUTES"] * 60,
        "only_participant_in_meeting_timeout_seconds": 120,
        "waiting_room_timeout_seconds": 900,
        "silence_timeout_seconds": 900,
    }
    leave.update(conf["AUTOMATIC_LEAVE_SETTINGS"] or {})
    payload = {
        "meeting_url": meeting.meeting_url,
        "bot_name": conf["BOT_NAME"],
        # Attendee metadata values must be strings.
        "metadata": {"meetbot_meeting": str(meeting.uid)},
        "deduplication_key": meeting.dedup_key,
        "transcription_settings": transcription_settings(conf),
        "recording_settings": {"format": conf["RECORDING_FORMAT"]},
        "automatic_leave_settings": leave,
        "webhooks": [
            {
                "url": conf["WEBHOOK_URL"],
                "triggers": ["bot.state_change", "transcript.update", "participant_events.join_leave"],
            }
        ],
    }
    if join_at:
        payload["join_at"] = join_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if conf["JOIN_MESSAGE"]:
        payload["bot_chat_message"] = {"to": "everyone", "message": conf["JOIN_MESSAGE"]}
    if conf["GOOGLE_MEET_USE_LOGIN"]:
        payload["google_meet_settings"] = {"use_login": True, "login_mode": "only_if_required"}
    return payload


def _record_failure(meeting, error, now):
    meeting.attempts += 1
    meeting.last_error = str(error)[:4000]
    if meeting.attempts >= settings.MEETBOT["MAX_ATTEMPTS"]:
        meeting.status = Meeting.Status.FAILED
        meeting.status_detail = "Bot yaradıla bilmədi"
        meeting.next_attempt_at = None
    else:
        meeting.next_attempt_at = now + backoff(meeting.attempts)
    meeting.save()


def schedule_bot(meeting, client=None, now=None):
    """Create the Attendee bot for a PENDING meeting. Safe to call repeatedly."""
    now = now or timezone.now()
    client = client or AttendeeClient()
    conf = settings.MEETBOT

    if meeting.status != Meeting.Status.PENDING:
        return meeting
    if meeting.end and meeting.end <= now + timedelta(minutes=1):
        meeting.status = Meeting.Status.CANCELLED
        meeting.status_detail = "Görüş artıq bitib"
        meeting.save()
        return meeting

    join_at = meeting.start - timedelta(seconds=conf["JOIN_EARLY_SECONDS"])
    if join_at < now + MIN_SCHEDULE_LEAD:
        join_at = None  # starting now (or already running) → ad-hoc bot joins immediately

    try:
        bot = client.create_bot(build_bot_payload(meeting, join_at))
    except AttendeeError as exc:
        bot = None
        # A previous attempt may have created the bot before we could save its id.
        if exc.status_code == 400 and "dedup" in (exc.body or "").lower():
            try:
                found = [b for b in client.find_bots_by_dedup_key(meeting.dedup_key) if b.get("state") not in FINISHED_STATES]
                bot = found[0] if found else None
            except AttendeeError:
                bot = None
        if bot is None:
            logger.warning("Creating bot for meeting %s failed: %s", meeting.uid, exc)
            _record_failure(meeting, exc, now)
            return meeting

    meeting.bot_id = bot["id"]
    meeting.attempts = 0
    meeting.next_attempt_at = None
    meeting.last_error = ""
    meeting.status = Meeting.Status.SCHEDULED
    apply_bot_state(meeting, bot.get("state", ""), save=False)
    meeting.save()
    logger.info("Bot %s scheduled for meeting %s (%s)", meeting.bot_id, meeting.uid, meeting.title)
    return meeting


def cancel_meeting(meeting, reason, client=None):
    client = client or AttendeeClient()
    if meeting.bot_id and meeting.bot_state not in FINISHED_STATES:
        try:
            if meeting.bot_state in ("scheduled", ""):
                client.delete_bot(meeting.bot_id)
            else:
                client.leave_meeting(meeting.bot_id)
        except AttendeeError as exc:
            logger.warning("Stopping bot %s failed: %s", meeting.bot_id, exc)
    meeting.status = Meeting.Status.CANCELLED
    meeting.status_detail = reason[:300]
    meeting.next_attempt_at = None
    meeting.save()


def apply_bot_state(meeting, new_state, event_type="", event_sub_type="", at=None, save=True):
    """Fold one Attendee state into our pipeline status. Never moves a meeting backwards."""
    at = at or timezone.now()
    if new_state:
        meeting.bot_state = new_state
    if event_type:
        meeting.bot_event = "/".join(p for p in (event_type, event_sub_type) if p)[:160]

    if new_state.startswith("joined") and not meeting.joined_at:
        meeting.joined_at = at

    if meeting.status in (Meeting.Status.PENDING, Meeting.Status.SCHEDULED, Meeting.Status.IN_MEETING):
        if new_state in PRE_JOIN_STATES:
            meeting.status = Meeting.Status.SCHEDULED
            if new_state == "waiting_room":
                meeting.status_detail = "Bot gözləmə otağındadır — görüşə qəbul edin"
        elif new_state in IN_MEETING_STATES:
            meeting.status = Meeting.Status.IN_MEETING
            meeting.status_detail = ""
        elif new_state in FINISHED_STATES:
            meeting.left_at = meeting.left_at or at
            meeting.status = Meeting.Status.PROCESSING
            meeting.status_detail = meeting.bot_event
            meeting.attempts = 0
            meeting.next_attempt_at = at + TRANSCRIPT_SETTLE
    if save:
        # Only the fields this function owns: a concurrent join/leave webhook may be writing participants.
        meeting.save(update_fields=STATE_FIELDS)
    return meeting


STATE_FIELDS = [
    "bot_state",
    "bot_event",
    "joined_at",
    "left_at",
    "status",
    "status_detail",
    "attempts",
    "next_attempt_at",
    "updated_at",
]


@dataclass
class SyncResult:
    fetched: int = 0
    scheduled: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def sync_calendar(now=None, source=None, client=None):
    """One pass: upcoming SABAH.OS Meet events → meetings + bots; vanished ones → cancelled."""
    now = now or timezone.now()
    conf = settings.MEETBOT
    source = source or SabahCalendarSource()
    client = client or AttendeeClient()
    result = SyncResult()
    if not source.enabled:
        return result

    until = now + timedelta(minutes=conf["LOOKAHEAD_MINUTES"])
    fetched = source.fetch(now, until)
    result.fetched = len(fetched.rows)

    wanted = set()
    for cand in group_rows(fetched.rows):
        join, reason = should_join(cand, conf)
        if not join:
            result.skipped.append((cand.dedup_key, reason))
            continue
        wanted.add(cand.dedup_key)
        meeting = _upsert_calendar_meeting(cand)
        if meeting.status == Meeting.Status.PENDING and (not meeting.next_attempt_at or meeting.next_attempt_at <= now):
            schedule_bot(meeting, client=client, now=now)
            if meeting.status == Meeting.Status.SCHEDULED:
                result.scheduled.append(meeting.dedup_key)

    # Only trust absence when we saw the whole window.
    if fetched.complete:
        stale = Meeting.objects.filter(
            source=Meeting.Source.CALENDAR,
            status__in=[Meeting.Status.PENDING, Meeting.Status.SCHEDULED],
            start__gt=now,
            start__lte=until,
        ).exclude(dedup_key__in=wanted)
        for meeting in stale:
            if meeting.bot_state not in ("", "scheduled", "staged"):
                continue  # already joining — leave it alone
            cancel_meeting(meeting, "Calendar-da görüş silinib, ləğv edilib və ya vaxtı dəyişib", client=client)
            result.cancelled.append(meeting.dedup_key)
    return result


def _upsert_calendar_meeting(cand):
    fields = {
        "title": cand.title[:300],
        "description": cand.description[:5000],
        "meeting_url": cand.meeting_url,
        "start": cand.start,
        "end": cand.end,
        "organizer_email": cand.organizer_email,
        "organizer_name": cand.organizer_name[:200],
        "invitees": cand.invitees,
        "source_event_ids": cand.event_ids,
    }
    meeting = Meeting.objects.filter(dedup_key=cand.dedup_key).first()
    if meeting is None:
        try:
            with transaction.atomic():
                return Meeting.objects.create(dedup_key=cand.dedup_key, source=Meeting.Source.CALENDAR, **fields)
        except IntegrityError:
            meeting = Meeting.objects.get(dedup_key=cand.dedup_key)
    if meeting.status not in Meeting.FINAL_STATUSES:
        changed = [k for k, v in fields.items() if getattr(meeting, k) != v]
        for k in changed:
            setattr(meeting, k, fields[k])
        if changed:
            meeting.save(update_fields=changed + ["updated_at"])
    return meeting


def create_manual_meeting(
    meeting_url, title="", start=None, end=None, invitees=None, organizer_email="", client=None, client_name=""
):
    """Send the bot to any Meet link right away (or at `start`)."""
    now = timezone.now()
    start = start or now.replace(second=0, microsecond=0)
    key = make_dedup_key(meeting_url, start, client_name)
    meeting, created = Meeting.objects.get_or_create(
        dedup_key=key,
        defaults={
            "source": Meeting.Source.MANUAL,
            "client": client_name,
            "meeting_url": normalize_meet_url(meeting_url),
            "title": title[:300],
            "start": start,
            "end": end,
            "invitees": invitees or [],
            "organizer_email": organizer_email.lower(),
        },
    )
    if created or meeting.status == Meeting.Status.PENDING:
        schedule_bot(meeting, client=client, now=now)
    return meeting, created
