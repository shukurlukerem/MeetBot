"""Small JSON API so SABAH.OS (or anyone holding MEETBOT_API_TOKEN) can drive the bot and read results.

    Authorization: Bearer <MEETBOT_API_TOKEN>
"""

import functools
import hmac
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from . import summarizer
from .attendee import AttendeeClient, AttendeeError
from .models import Meeting
from .pipeline import queue_resummarize
from .scheduling import cancel_meeting, create_manual_meeting
from .serializers import meeting_to_dict, utterance_to_dict

logger = logging.getLogger(__name__)


def error(message, status):
    return JsonResponse({"error": message}, status=status)


def api_view(methods):
    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            token = settings.MEETBOT["API_TOKEN"]
            if not token:
                return error("API is disabled (MEETBOT_API_TOKEN is not set)", 503)
            header = request.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else ""
            if not given or not hmac.compare_digest(given.encode(), token.encode()):
                return error("unauthorized", 401)
            if request.method not in methods:
                return error("method not allowed", 405)
            if request.method in ("POST", "PATCH") and request.body:
                try:
                    request.json = json.loads(request.body)
                except ValueError:
                    return error("invalid JSON body", 400)
                if not isinstance(request.json, dict):
                    return error("JSON body must be an object", 400)
            else:
                request.json = {}
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def _get_meeting(uid):
    try:
        return Meeting.objects.get(uid=uid)
    except (Meeting.DoesNotExist, ValueError):
        return None


@api_view(["GET", "POST"])
def meetings(request):
    if request.method == "POST":
        return _create_meeting(request)
    qs = Meeting.objects.all()
    if request.GET.get("status"):
        qs = qs.filter(status__in=request.GET["status"].split(","))
    if request.GET.get("from"):
        start = parse_datetime(request.GET["from"])
        if start is None:
            return error("`from` must be an ISO 8601 datetime", 400)
        qs = qs.filter(start__gte=start)
    if request.GET.get("to"):
        end = parse_datetime(request.GET["to"])
        if end is None:
            return error("`to` must be an ISO 8601 datetime", 400)
        qs = qs.filter(start__lte=end)
    if request.GET.get("sabah_event_id"):
        try:
            event_id = int(request.GET["sabah_event_id"])
        except ValueError:
            return error("`sabah_event_id` must be an integer", 400)
        # JSON containment is Postgres-only; a Python filter keeps the endpoint portable and the list is small.
        qs = [m for m in qs.order_by("-start")[:500] if event_id in (m.source_event_ids or [])]
    try:
        limit = max(1, min(int(request.GET.get("limit", 50)), 200))
    except ValueError:
        return error("`limit` must be an integer", 400)
    rows = list(qs[:limit])
    return JsonResponse({"results": [meeting_to_dict(m) for m in rows]})


def _create_meeting(request):
    body = request.json
    url = (body.get("meeting_url") or "").strip()
    if not url.startswith("https://meet.google.com/"):
        return error("meeting_url must be a https://meet.google.com/... link", 400)
    start = parse_datetime(body["start"]) if body.get("start") else None
    end = parse_datetime(body["end"]) if body.get("end") else None
    if (body.get("start") and start is None) or (body.get("end") and end is None):
        return error("start/end must be ISO 8601 datetimes with a timezone", 400)
    if (start and timezone.is_naive(start)) or (end and timezone.is_naive(end)):
        return error("start/end must include a timezone offset", 400)
    if start and start < timezone.now() - timedelta(hours=12):
        return error("start is in the past", 400)
    invitees = [
        {"email": (i.get("email") or "").lower(), "name": i.get("name") or ""}
        for i in body.get("invitees") or []
        if isinstance(i, dict)
    ]
    meeting, created = create_manual_meeting(
        url,
        title=body.get("title") or "",
        start=start,
        end=end,
        invitees=invitees,
        organizer_email=body.get("organizer_email") or "",
    )
    return JsonResponse(meeting_to_dict(meeting, include_summary=True), status=201 if created else 200)


@api_view(["GET", "DELETE"])
def meeting_detail(request, uid):
    meeting = _get_meeting(uid)
    if meeting is None:
        return error("not found", 404)
    if request.method == "DELETE":
        if meeting.status not in Meeting.ACTIVE_STATUSES:
            return error(f"meeting is {meeting.status}; nothing to stop", 409)
        cancel_meeting(meeting, "API ilə ləğv edilib")
        return JsonResponse(meeting_to_dict(meeting))
    return JsonResponse(meeting_to_dict(meeting, include_summary=True))


@api_view(["GET"])
def meeting_transcript(request, uid):
    meeting = _get_meeting(uid)
    if meeting is None:
        return error("not found", 404)
    utterances = list(meeting.utterances.all())
    if request.GET.get("format") == "text":
        return JsonResponse({"id": str(meeting.uid), "text": summarizer.transcript_text(utterances)})
    return JsonResponse({"id": str(meeting.uid), "utterances": [utterance_to_dict(u) for u in utterances]})


@api_view(["POST"])
def meeting_summarize(request, uid):
    meeting = _get_meeting(uid)
    if meeting is None:
        return error("not found", 404)
    instructions = request.json.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        return error("instructions must be a string", 400)
    try:
        queue_resummarize(meeting, instructions)
    except ValueError as exc:
        return error(str(exc), 409)
    return JsonResponse(meeting_to_dict(meeting), status=202)


@api_view(["POST"])
def meeting_ask(request, uid):
    meeting = _get_meeting(uid)
    if meeting is None:
        return error("not found", 404)
    question = request.json.get("question")
    if not isinstance(question, str) or not question.strip():
        return error("question is required", 400)
    utterances = list(meeting.utterances.all())
    if not utterances:
        return error("this meeting has no transcript yet", 409)
    try:
        result = summarizer.ask(meeting, utterances, question)
    except summarizer.SummaryRefused as exc:
        return error(str(exc), 422)
    except summarizer.SummaryError as exc:
        logger.warning("Ask failed for %s: %s", meeting.uid, exc)
        return error("Claude request failed, try again", 502)
    return JsonResponse({"answer": result.text, "model": result.model, "usage": result.usage})


@api_view(["POST"])
def meeting_leave(request, uid):
    meeting = _get_meeting(uid)
    if meeting is None:
        return error("not found", 404)
    if not meeting.bot_id or meeting.status != Meeting.Status.IN_MEETING:
        return error("the bot is not in this meeting", 409)
    try:
        AttendeeClient().leave_meeting(meeting.bot_id)
    except AttendeeError as exc:
        return error(f"Attendee refused: {exc}", 502)
    return JsonResponse(meeting_to_dict(meeting), status=202)


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001
        return JsonResponse({"ok": False}, status=503)
    return JsonResponse({"ok": True})
