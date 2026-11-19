"""SABAH.OS → Meetbot: the per-event Notetaker toggle.

A SABAH.OS backend posts the current state of each event whose toggle changed (or that changed
while the toggle is on). The call is idempotent — the backend just says what is true now:

    {"event_id": "123", "enabled": true, "meeting_url": "https://meet.google.com/abc-defg-hij",
     "start": "...", "end": "...", "title": "...", "description": "...",
     "organizer": {"email": "...", "name": "..."}, "invitees": [{"email": "...", "name": "..."}]}

Events with the same Meet link and start (members' copies of one Google meeting) share one
meeting and one bot. An event that is switched off, deleted, moved or re-linked leaves its old
meeting; a meeting no event wants any more has its bot cancelled.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .calendar_source import make_dedup_key, normalize_meet_url
from .models import EventLink, Meeting
from .scheduling import cancel_meeting, schedule_bot

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_CALL = 500


class EventPayloadError(ValueError):
    pass


@dataclass
class EventState:
    event_id: str
    enabled: bool
    meeting_url: str = ""
    start: object = None
    end: object = None
    title: str = ""
    description: str = ""
    organizer_email: str = ""
    organizer_name: str = ""
    invitees: list = None


def parse_event(item):
    if not isinstance(item, dict):
        raise EventPayloadError("each event must be an object")
    event_id = str(item.get("event_id") or "").strip()
    if not event_id or len(event_id) > 64:
        raise EventPayloadError("event_id is required (max 64 chars)")
    if not item.get("enabled"):
        return EventState(event_id=event_id, enabled=False)

    url = (item.get("meeting_url") or "").strip()
    if not url.startswith("https://meet.google.com/"):
        raise EventPayloadError(f"event {event_id}: meeting_url must be a Google Meet link")
    start = parse_datetime(item.get("start") or "")
    end = parse_datetime(item.get("end") or "") if item.get("end") else None
    if start is None or timezone.is_naive(start) or (end is not None and timezone.is_naive(end)):
        raise EventPayloadError(f"event {event_id}: start/end must be ISO 8601 with a timezone")
    organizer = item.get("organizer") or {}
    invitees = [
        {"email": (i.get("email") or "").strip().lower(), "name": (i.get("name") or "")[:200]}
        for i in (item.get("invitees") or [])
        if isinstance(i, dict) and i.get("email")
    ]
    return EventState(
        event_id=event_id,
        enabled=True,
        meeting_url=normalize_meet_url(url),
        start=start,
        end=end,
        title=(item.get("title") or "")[:300],
        description=(item.get("description") or "")[:5000],
        organizer_email=(organizer.get("email") or "").strip().lower(),
        organizer_name=(organizer.get("name") or "")[:200],
        invitees=invitees,
    )


def _refresh_event_ids(meeting):
    ids = sorted(meeting.event_links.values_list("event_id", flat=True))
    as_ints = [int(i) if i.isdigit() else i for i in ids]
    if meeting.source_event_ids != as_ints:
        meeting.source_event_ids = as_ints
        meeting.save(update_fields=["source_event_ids", "updated_at"])


def _release(meeting, client):
    """The meeting lost an event: drop its bot if no event wants it any more."""
    _refresh_event_ids(meeting)
    if not meeting.event_links.exists() and meeting.status in Meeting.ACTIVE_STATUSES:
        cancel_meeting(meeting, "Notetaker SABAH.OS-da söndürülüb və ya görüş dəyişib", client=client)


def _merge_invitees(existing, new):
    merged = {i["email"]: i for i in existing or [] if i.get("email")}
    for invitee in new or []:
        merged.setdefault(invitee["email"], invitee)
    return sorted(merged.values(), key=lambda i: i["email"])


def _meeting_for(client_name, state, now):
    """Find or create the meeting this event belongs on. None when it is already over."""
    key = make_dedup_key(state.meeting_url, state.start, client_name)
    meeting = Meeting.objects.filter(dedup_key=key).first()
    over = (state.end or state.start + timedelta(hours=1)) <= now
    if meeting is None:
        if over:
            return None
        return Meeting.objects.create(
            dedup_key=key,
            client=client_name,
            source=Meeting.Source.EVENT,
            meeting_url=state.meeting_url,
            start=state.start,
            end=state.end,
            title=state.title,
            description=state.description,
            organizer_email=state.organizer_email,
            organizer_name=state.organizer_name,
            invitees=state.invitees or [],
        )

    if meeting.status == Meeting.Status.CANCELLED and not over:
        # Switched off and on again before the meeting: bring it back with a fresh bot.
        meeting.status = Meeting.Status.PENDING
        meeting.status_detail = ""
        meeting.bot_id = None
        meeting.bot_state = ""
        meeting.bot_event = ""
        meeting.attempts = 0
        meeting.next_attempt_at = None
        meeting.last_error = ""
    if meeting.status not in Meeting.FINAL_STATUSES:
        meeting.title = state.title or meeting.title
        meeting.description = state.description
        meeting.end = state.end
        meeting.organizer_email = meeting.organizer_email or state.organizer_email
        meeting.organizer_name = meeting.organizer_name or state.organizer_name
        meeting.invitees = _merge_invitees(meeting.invitees, state.invitees)
    meeting.save()
    return meeting


def sync_events(client_name, items, window=None, attendee=None, now=None):
    """Apply a batch of event states for one client. Returns one result per event, in order."""
    now = now or timezone.now()
    lookahead = timedelta(minutes=settings.MEETBOT["LOOKAHEAD_MINUTES"])
    states = [parse_event(item) for item in items]
    results, touched = [], []

    for state in states:
        with transaction.atomic():
            link = EventLink.objects.select_for_update().filter(client=client_name, event_id=state.event_id).first()
            previous = link.meeting if link else None
            meeting = _meeting_for(client_name, state, now) if state.enabled else None

            if meeting is None:
                if link and not state.enabled:
                    link.delete()
                # An enabled event that is already over keeps whatever it had.
            elif link is None:
                EventLink.objects.create(client=client_name, event_id=state.event_id, meeting=meeting)
            elif link.meeting_id != meeting.pk:
                link.meeting = meeting
                link.save(update_fields=["meeting", "updated_at"])

        if previous is not None and (meeting is None or previous.pk != meeting.pk) and not (meeting is None and state.enabled):
            _release(previous, attendee)
        if meeting is not None:
            _refresh_event_ids(meeting)
            if meeting.status == Meeting.Status.PENDING and meeting.start <= now + lookahead:
                schedule_bot(meeting, client=attendee, now=now)  # starting soon (or now): don't wait for the worker
            touched.append(meeting.pk)
        results.append({"event_id": state.event_id, "meeting": meeting or (previous if state.enabled else None)})

    if window:
        _sweep_window(client_name, window, {s.event_id for s in states if s.enabled}, attendee)
    return results


def _sweep_window(client_name, window, wanted_ids, attendee):
    """Full-state reconcile: in this window, any linked event the backend no longer reports is off."""
    start, end = window
    stale = (
        EventLink.objects.filter(client=client_name, meeting__start__gte=start, meeting__start__lt=end)
        .exclude(event_id__in=wanted_ids)
        .select_related("meeting")
    )
    for link in list(stale):
        meeting = link.meeting
        link.delete()
        _release(meeting, attendee)


def meeting_for_event(client_name, event_id):
    link = EventLink.objects.filter(client=client_name, event_id=str(event_id)).select_related("meeting").first()
    return link.meeting if link else None
