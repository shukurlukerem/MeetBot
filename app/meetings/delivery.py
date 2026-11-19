"""Hands a finished summary to people (email) and systems (signed webhook)."""

import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

from . import mailer
from .serializers import meeting_to_dict
from .summarizer import render_html

logger = logging.getLogger(__name__)


def email_recipients(meeting, conf=None):
    conf = conf or settings.MEETBOT
    mode = conf["EMAIL_TO"]
    if mode == "organizer":
        candidates = [meeting.organizer_email]
    elif mode == "invitees":
        candidates = [meeting.organizer_email] + [i.get("email", "") for i in meeting.invitees]
    else:
        return []
    allowed = conf["EMAIL_ALLOWED_DOMAINS"]
    seen, result = set(), []
    for email in candidates:
        email = (email or "").strip().lower()
        if not email or email in seen:
            continue
        # Notes from internal meetings must not leak to outside guests.
        if allowed and email.rsplit("@", 1)[-1] not in allowed:
            continue
        seen.add(email)
        result.append(email)
    return result


def meeting_link(meeting):
    base = settings.MEETBOT["PUBLIC_URL"]
    return f"{base}/admin/meetings/meeting/{meeting.pk}/change/" if base else ""


def send_summary_email(meeting):
    recipients = email_recipients(meeting)
    if not recipients:
        return 0
    title = (meeting.summary or {}).get("title") or meeting.title or "Meeting"
    date = timezone.localtime(meeting.start).strftime("%d %b %Y, %H:%M")
    link = meeting_link(meeting)
    text = meeting.summary_markdown
    if link:
        text += f"\n---\nFull transcript: {link}\n"
    mailer.send_message(
        subject=f"Meeting notes: {title} ({date})",
        to_addresses=recipients,
        text_body=text,
        html_body=render_html(meeting, meeting.summary or {}, link),
    )
    return len(recipients)


def post_result_webhook(meeting):
    conf = settings.MEETBOT
    url = conf["RESULT_WEBHOOK_URL"]
    if not url:
        return None
    body = json.dumps(
        {"event": "meeting.summarized", "meeting": meeting_to_dict(meeting, include_summary=True)},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "SABAH-Meetbot/1.0"}
    if conf["RESULT_WEBHOOK_SECRET"]:
        digest = hmac.new(conf["RESULT_WEBHOOK_SECRET"].encode(), body, hashlib.sha256).hexdigest()
        headers["X-Meetbot-Signature"] = f"sha256={digest}"
    response = requests.post(url, data=body, headers=headers, timeout=15)
    response.raise_for_status()
    return response.status_code


def deliver(meeting):
    """Best effort: a delivery failure is logged on the meeting, the summary itself stays DONE."""
    errors = []
    for name, fn in (("email", send_summary_email), ("webhook", post_result_webhook)):
        try:
            fn(meeting)
        except Exception as exc:  # noqa: BLE001 — any transport error is reported, none is fatal
            logger.exception("Summary %s delivery failed for meeting %s", name, meeting.uid)
            errors.append(f"{name}: {exc}")
    meeting.delivered_at = timezone.now()
    if errors:
        meeting.last_error = "; ".join(errors)[:4000]
    meeting.save(update_fields=["delivered_at", "last_error", "updated_at"])
    return errors
