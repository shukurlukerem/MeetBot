"""Receives Attendee's bot webhooks: state changes, live transcript lines, join/leave."""

import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .attendee import verify_signature
from .models import Meeting, Utterance
from .scheduling import apply_bot_state

logger = logging.getLogger(__name__)


def _find_meeting(payload):
    bot_id = payload.get("bot_id")
    meeting = Meeting.objects.filter(bot_id=bot_id).first() if bot_id else None
    if meeting is None:
        # An ad-hoc bot can report "joining" before we stored its id.
        uid = (payload.get("bot_metadata") or {}).get("meetbot_meeting")
        if uid:
            meeting = Meeting.objects.filter(uid=uid).first()
            if meeting and bot_id and not meeting.bot_id:
                meeting.bot_id = bot_id
                meeting.save(update_fields=["bot_id", "updated_at"])
    return meeting


def handle_state_change(meeting, data):
    at = parse_datetime(data.get("created_at") or "") or timezone.now()
    apply_bot_state(meeting, data.get("new_state", ""), data.get("event_type") or "", data.get("event_sub_type") or "", at=at)


def handle_transcript_update(meeting, data):
    text = ((data.get("transcription") or {}).get("transcript") or "").strip()
    if not text:
        return
    speaker_uuid = (data.get("speaker_uuid") or "")[:200]
    timestamp_ms = data.get("timestamp_ms") or 0
    defaults = {
        "speaker_name": (data.get("speaker_name") or "")[:200],
        "duration_ms": data.get("duration_ms") or 0,
        "text": text,
    }
    try:
        with transaction.atomic():
            # Captions for the same line can arrive more than once as Meet refines them.
            Utterance.objects.update_or_create(
                meeting=meeting, speaker_uuid=speaker_uuid, timestamp_ms=timestamp_ms, defaults=defaults
            )
    except IntegrityError:
        Utterance.objects.filter(meeting=meeting, speaker_uuid=speaker_uuid, timestamp_ms=timestamp_ms).update(**defaults)
    if meeting.status == Meeting.Status.SCHEDULED:
        apply_bot_state(meeting, "joined_recording")  # speech arriving means the bot is in, even if we missed that webhook
    elif meeting.status == Meeting.Status.IN_MEETING:
        Meeting.objects.filter(pk=meeting.pk).update(updated_at=timezone.now())


def handle_join_leave(meeting, data):
    uuid = data.get("participant_uuid") or ""
    name = data.get("participant_name") or ""
    if not uuid and not name:
        return
    with transaction.atomic():
        locked = Meeting.objects.select_for_update().get(pk=meeting.pk)
        people = list(locked.participants or [])
        entry = next((p for p in people if p.get("uuid") == uuid), None)
        if entry is None:
            entry = {"uuid": uuid, "name": name, "is_host": bool(data.get("participant_is_host"))}
            people.append(entry)
        entry["last_event"] = data.get("event_type") or ""
        locked.participants = people
        locked.save(update_fields=["participants", "updated_at"])


HANDLERS = {
    "bot.state_change": handle_state_change,
    "transcript.update": handle_transcript_update,
    "participant_events.join_leave": handle_join_leave,
}


@csrf_exempt
@require_POST
def attendee_webhook(request):
    secret = settings.MEETBOT["ATTENDEE_WEBHOOK_SECRET"]
    if not secret:
        logger.error("ATTENDEE_WEBHOOK_SECRET is not set; refusing webhook")
        return HttpResponse("webhook secret not configured", status=503)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest("invalid json")
    if not isinstance(payload, dict):
        return HttpResponseBadRequest("invalid payload")
    if not verify_signature(payload, request.headers.get("X-Webhook-Signature", ""), secret):
        logger.warning("Rejected Attendee webhook with a bad signature")
        return HttpResponseForbidden("invalid signature")

    handler = HANDLERS.get(payload.get("trigger"))
    if handler is None:
        return HttpResponse("ignored")
    meeting = _find_meeting(payload)
    if meeting is None:
        return HttpResponse("unknown bot")
    handler(meeting, payload.get("data") or {})
    return HttpResponse("ok")
