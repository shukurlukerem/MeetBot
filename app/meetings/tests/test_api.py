from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from meetings.models import Meeting, Utterance
from meetings.summarizer import ClaudeResult

from .fakes import FakeAttendee

TOKEN = "test-token-123"


@override_settings(MEETBOT={**settings.MEETBOT, "API_TOKEN": TOKEN})
class ApiTests(TestCase):
    def call(self, method, path, body=None, token=TOKEN):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        fn = getattr(self.client, method)
        if body is not None:
            return fn(path, data=body, content_type="application/json", **headers)
        return fn(path, **headers)

    def make_meeting(self, **kw):
        defaults = {"dedup_key": kw.pop("key", "k1"), "title": "Sprint", "meeting_url": "https://meet.google.com/abc-defg-hij",
                    "start": timezone.now() - timedelta(hours=1), "status": Meeting.Status.DONE, "bot_id": "bot_1",
                    "bot_state": "ended", "summary_markdown": "# Sprint\n", "source_event_ids": [41, 42]}
        defaults.update(kw)
        return Meeting.objects.create(**defaults)

    def test_requires_token(self):
        self.assertEqual(self.call("get", "/api/meetings/", token=None).status_code, 401)
        self.assertEqual(self.call("get", "/api/meetings/", token="wrong").status_code, 401)

    @override_settings(MEETBOT={**settings.MEETBOT, "API_TOKEN": ""})
    def test_disabled_without_token(self):
        self.assertEqual(self.call("get", "/api/meetings/").status_code, 503)

    def test_list_and_filter(self):
        self.make_meeting()
        self.make_meeting(key="k2", bot_id="bot_2", status=Meeting.Status.SCHEDULED, source_event_ids=[7])
        data = self.call("get", "/api/meetings/?status=done").json()
        self.assertEqual([m["status"] for m in data["results"]], ["done"])
        data = self.call("get", "/api/meetings/?sabah_event_id=7").json()
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(self.call("get", "/api/meetings/?from=yesterday").status_code, 400)

    def test_create_manual_bot(self):
        client = FakeAttendee(bot_state="joining")
        with mock.patch("meetings.scheduling.AttendeeClient", return_value=client):
            response = self.call("post", "/api/meetings/", {"meeting_url": "https://meet.google.com/new-mtng-abc?authuser=0", "title": "Ad-hoc"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["meeting_url"], "https://meet.google.com/new-mtng-abc")
        self.assertEqual(body["status"], "scheduled")
        self.assertEqual(body["bot"]["id"], "bot_1")
        self.assertNotIn("join_at", client.created[0])

    def test_create_validates(self):
        self.assertEqual(self.call("post", "/api/meetings/", {"meeting_url": "https://zoom.us/j/1"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/meetings/", {"meeting_url": "https://meet.google.com/a", "start": "2026-11-20T10:00:00"}).status_code, 400)

    def test_detail_transcript_and_summarize(self):
        meeting = self.make_meeting()
        Utterance.objects.create(meeting=meeting, speaker_name="Karam", speaker_uuid="1", timestamp_ms=0, text="Salam")
        detail = self.call("get", f"/api/meetings/{meeting.uid}/").json()
        self.assertEqual(detail["summary_markdown"], "# Sprint\n")
        text = self.call("get", f"/api/meetings/{meeting.uid}/transcript/?format=text").json()["text"]
        self.assertEqual(text, "[00:00:00] Karam: Salam")

        response = self.call("post", f"/api/meetings/{meeting.uid}/summarize/", {"instructions": "Qısa yaz"})
        self.assertEqual(response.status_code, 202)
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.PROCESSING)
        self.assertEqual(meeting.summary_instructions, "Qısa yaz")

    def test_ask(self):
        meeting = self.make_meeting()
        self.assertEqual(self.call("post", f"/api/meetings/{meeting.uid}/ask/", {"question": "Nə qərar verildi?"}).status_code, 409)
        Utterance.objects.create(meeting=meeting, speaker_name="Karam", speaker_uuid="1", timestamp_ms=0, text="Cümə buraxırıq")
        fake = ClaudeResult(text="Cümə günü buraxılış [00:00:00]", model="claude-opus-5-5", usage={})
        with mock.patch("meetings.api.summarizer.ask", return_value=fake) as ask:
            response = self.call("post", f"/api/meetings/{meeting.uid}/ask/", {"question": "Nə qərar verildi?"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], fake.text)
        self.assertEqual(ask.call_args.args[2], "Nə qərar verildi?")

    def test_cancel(self):
        meeting = self.make_meeting(status=Meeting.Status.SCHEDULED, bot_state="scheduled")
        client = FakeAttendee()
        with mock.patch("meetings.scheduling.AttendeeClient", return_value=client):
            response = self.call("delete", f"/api/meetings/{meeting.uid}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.deleted, ["bot_1"])
        self.assertEqual(self.call("delete", f"/api/meetings/{meeting.uid}/").status_code, 409)

    def test_unknown_meeting(self):
        self.assertEqual(self.call("get", "/api/meetings/00000000-0000-0000-0000-000000000000/").status_code, 404)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})
