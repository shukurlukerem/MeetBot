import base64
import json
from datetime import timedelta

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from meetings.attendee import sign_payload, verify_signature
from meetings.models import Meeting

SECRET_BYTES = b"0123456789abcdef0123456789abcdef"
SECRET_B64 = base64.b64encode(SECRET_BYTES).decode()


def meetbot(**overrides):
    return override_settings(MEETBOT={**settings.MEETBOT, "ATTENDEE_WEBHOOK_SECRET": SECRET_B64, **overrides})


@meetbot()
class WebhookTests(TestCase):
    def setUp(self):
        self.meeting = Meeting.objects.create(
            dedup_key="k1",
            meeting_url="https://meet.google.com/abc-defg-hij",
            start=timezone.now(),
            status=Meeting.Status.SCHEDULED,
            bot_id="bot_abc",
        )

    def post(self, trigger, data, bot_id="bot_abc", metadata=None, signature=None):
        payload = {
            "idempotency_key": "00000000-0000-0000-0000-000000000000",
            "bot_id": bot_id,
            "bot_metadata": metadata or {},
            "trigger": trigger,
            "data": data,
        }
        sig = signature if signature is not None else sign_payload(payload, SECRET_BYTES)
        # Attendee sends requests.post(json=...) — default separators, not the canonical form it signs.
        return self.client.post(
            "/webhooks/attendee/", data=json.dumps(payload), content_type="application/json", HTTP_X_WEBHOOK_SIGNATURE=sig
        )

    def test_rejects_bad_signature(self):
        response = self.post("bot.state_change", {"new_state": "ended"}, signature="nope")
        self.assertEqual(response.status_code, 403)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.SCHEDULED)

    @meetbot(ATTENDEE_WEBHOOK_SECRET="")
    def test_refuses_when_secret_missing(self):
        self.assertEqual(self.post("bot.state_change", {"new_state": "ended"}).status_code, 503)

    def test_signature_matches_attendee_algorithm(self):
        payload = {"b": 1, "a": "ə"}
        self.assertTrue(verify_signature(payload, sign_payload(payload, SECRET_BYTES), SECRET_B64))
        self.assertFalse(verify_signature(payload, sign_payload({"a": "x"}, SECRET_BYTES), SECRET_B64))

    def test_state_changes_drive_the_pipeline(self):
        self.post("bot.state_change", {"new_state": "joined_recording", "old_state": "joining", "event_type": "bot_joined_meeting"})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.IN_MEETING)

        self.post("bot.state_change", {"new_state": "ended", "old_state": "post_processing", "event_type": "post_processing_completed",
                                       "created_at": "2026-11-20T07:01:00Z"})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.PROCESSING)
        self.assertEqual(self.meeting.left_at.isoformat(), "2026-11-20T07:01:00+00:00")

    def test_transcript_lines_are_upserted(self):
        line = {"speaker_name": "Karam", "speaker_uuid": "spk1", "timestamp_ms": 1000, "duration_ms": 900,
                "transcription": {"transcript": "Salam hamıya"}}
        self.post("transcript.update", line)
        line["transcription"] = {"transcript": "Salam hamıya, başlayaq"}
        self.post("transcript.update", line)
        self.post("transcript.update", {**line, "timestamp_ms": 5000, "transcription": {"transcript": "  "}})

        texts = list(self.meeting.utterances.values_list("text", flat=True))
        self.assertEqual(texts, ["Salam hamıya, başlayaq"])
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.IN_MEETING)  # speech proves the bot is in

    def test_unknown_bot_found_through_metadata(self):
        self.meeting.bot_id = None
        self.meeting.save()
        response = self.post("bot.state_change", {"new_state": "joining"}, bot_id="bot_new",
                             metadata={"meetbot_meeting": str(self.meeting.uid)})
        self.assertEqual(response.status_code, 200)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.bot_id, "bot_new")

    def test_participants_tracked(self):
        self.post("participant_events.join_leave", {"participant_name": "Leyla", "participant_uuid": "p1",
                                                     "participant_is_host": True, "event_type": "join"})
        self.post("participant_events.join_leave", {"participant_name": "Leyla", "participant_uuid": "p1", "event_type": "leave"})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.participants, [{"uuid": "p1", "name": "Leyla", "is_host": True, "last_event": "leave"}])

    def test_other_triggers_and_bots_are_ignored(self):
        self.assertEqual(self.post("chat_messages.update", {}).content, b"ignored")
        self.assertEqual(self.post("bot.state_change", {"new_state": "ended"}, bot_id="bot_other").content, b"unknown bot")

    def test_finished_meeting_ignores_late_states(self):
        self.meeting.status = Meeting.Status.DONE
        self.meeting.save()
        self.post("bot.state_change", {"new_state": "fatal_error", "event_type": "fatal_error"})
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        self.assertEqual(self.meeting.bot_state, "fatal_error")
        self.assertLess(timezone.now() - self.meeting.updated_at, timedelta(minutes=1))
