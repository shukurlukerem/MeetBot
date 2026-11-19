"""Thin client for the self-hosted Attendee REST API (https://docs.attendee.dev)."""

import base64
import binascii
import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Bot states as Attendee's API reports them.
PRE_JOIN_STATES = {"ready", "scheduled", "staged", "joining", "waiting_room"}
IN_MEETING_STATES = {
    "joined_not_recording",
    "joined_recording",
    "joined_recording_paused",
    "joined_recording_permission_denied",
    "joining_breakout_room",
    "leaving_breakout_room",
    "leaving",
    "post_processing",
}
FINISHED_STATES = {"ended", "fatal_error"}
TERMINAL_STATES = FINISHED_STATES | {"data_deleted"}


class AttendeeError(Exception):
    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AttendeeClient:
    def __init__(self, base_url=None, api_key=None, timeout=20):
        conf = settings.MEETBOT
        self.base_url = (base_url or conf["ATTENDEE_API_URL"]).rstrip("/")
        self.api_key = api_key or conf["ATTENDEE_API_KEY"]
        self.timeout = timeout
        self.session = requests.Session()

    def _request(self, method, path, **kwargs):
        if not self.api_key:
            raise AttendeeError("ATTENDEE_API_KEY is not configured")
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        headers = {"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"}
        try:
            response = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise AttendeeError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            body = response.text[:2000]
            raise AttendeeError(f"{method} {path} -> {response.status_code}: {body}", response.status_code, body)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def create_bot(self, payload):
        return self._request("POST", "bots", json=payload)

    def get_bot(self, bot_id):
        return self._request("GET", f"bots/{bot_id}")

    def find_bots_by_dedup_key(self, key):
        data = self._request("GET", "bots", params={"deduplication_key": key}) or {}
        return data.get("results", data if isinstance(data, list) else [])

    def delete_bot(self, bot_id):
        """Unschedule a bot. Attendee only allows this while it is still `scheduled`."""
        return self._request("DELETE", f"bots/{bot_id}")

    def leave_meeting(self, bot_id):
        return self._request("POST", f"bots/{bot_id}/leave")

    def get_transcript(self, bot_id):
        return self._request("GET", f"bots/{bot_id}/transcript") or []


def sign_payload(payload, secret_bytes):
    """Attendee's webhook signature: base64(HMAC-SHA256(secret, canonical JSON))."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hmac.new(secret_bytes, canonical.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def verify_signature(payload, signature, secret_b64):
    """`secret_b64` is the value shown on Attendee's Settings → Webhooks page."""
    if not signature or not secret_b64:
        return False
    try:
        secret = base64.b64decode(secret_b64)
    except (binascii.Error, ValueError):
        logger.error("ATTENDEE_WEBHOOK_SECRET is not valid base64")
        return False
    return hmac.compare_digest(sign_payload(payload, secret), signature)
