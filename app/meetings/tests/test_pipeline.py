import email
import json
from email.header import decode_header, make_header
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from meetings import pipeline
from meetings.attendee import AttendeeError
from meetings.delivery import email_recipients
from meetings.models import Meeting, Utterance
from meetings.summarizer import SUMMARY_SCHEMA, transcript_text

from .fakes import SUMMARY, FakeAttendee, FakeClaude, transcript_entry


def meetbot(**overrides):
    return override_settings(MEETBOT={**settings.MEETBOT, **overrides})


TRANSCRIPT = [
    transcript_entry("Karam", "s1", 1_700_000_000_000, "Salam, bu həftə mobil buraxılışı danışaq."),
    transcript_entry("Leyla", "s2", 1_700_000_065_000, "Release notes-u cümə günü hazırlayaram."),
]


class FakeSMTP:
    """Stands in for smtplib.SMTP; records what the SES mailer would send."""

    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls = host, port, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self):
        self.calls.append("starttls")

    def has_extn(self, name):
        return name == "auth"

    def login(self, user, password):
        self.calls.append(("login", user))

    def sendmail(self, from_addr, to_addrs, message):
        FakeSMTP.sent.append({"host": self.host, "port": self.port, "from": from_addr, "to": to_addrs,
                              "msg": email.message_from_string(message), "calls": list(self.calls)})


@meetbot(EMAIL_TO="organizer", EMAIL_ALLOWED_DOMAINS=["sabah.az"], PUBLIC_URL="https://meetbot.example",
         SUMMARY_LANGUAGE="en", EMAIL_FROM="SABAH Meetbot <no-reply@sabahhub.com>")
@override_settings(SES_SMTP_HOST="email-smtp.eu-central-1.amazonaws.com", SES_SMTP_PORT=587, SES_SMTP_USE_TLS=True,
                   SES_KEY_ID="AKIA-test", SES_ACCESS_SECRET="secret-test")
class ProcessMeetingTests(TestCase):
    def setUp(self):
        FakeSMTP.sent = []
        patcher = mock.patch("meetings.mailer.smtplib.SMTP", FakeSMTP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.meeting = Meeting.objects.create(
            dedup_key="k1",
            title="Planlama",
            meeting_url="https://meet.google.com/abc-defg-hij",
            start=timezone.now() - timedelta(hours=1),
            end=timezone.now(),
            organizer_email="karam@sabah.az",
            organizer_name="Karam",
            invitees=[{"email": "leyla@sabah.az", "name": "Leyla"}, {"email": "guest@client.com", "name": "Guest"}],
            status=Meeting.Status.PROCESSING,
            bot_id="bot_1",
            bot_state="ended",
        )

    def test_happy_path(self):
        claude = FakeClaude()
        done = pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=claude)

        self.assertEqual([m.pk for m in done], [self.meeting.pk])
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        self.assertEqual(self.meeting.summary, SUMMARY)
        self.assertIn("## Action items", self.meeting.summary_markdown)
        self.assertIn("- [ ] Write the release notes — **Leyla** (Friday)", self.meeting.summary_markdown)
        self.assertIn("**no owner named**", self.meeting.summary_markdown)
        self.assertEqual(self.meeting.summary_usage["input_tokens"], 1200)
        self.assertEqual(self.meeting.utterances.count(), 2)

        call = claude.calls[0]
        self.assertEqual(call["model"], settings.MEETBOT["ANTHROPIC_MODEL"])
        self.assertEqual(call["output_config"]["format"], {"type": "json_schema", "schema": SUMMARY_SCHEMA})
        self.assertEqual(call["fallbacks"], "default")
        prompt = call["messages"][0]["content"]
        self.assertIn("[00:01:05] Leyla: Release notes-u cümə günü hazırlayaram.", prompt)
        self.assertIn("<transcript>", prompt)
        self.assertIn("Write every field in English", call["system"])
        self.assertIn("Azerbaijani, Russian and", call["system"])

        (sent,) = FakeSMTP.sent
        self.assertEqual((sent["host"], sent["port"]), ("email-smtp.eu-central-1.amazonaws.com", 587))
        self.assertEqual(sent["calls"], ["ehlo", "starttls", "ehlo", ("login", "AKIA-test")])
        self.assertEqual(sent["from"], "no-reply@sabahhub.com")  # bare SES-verified envelope sender
        self.assertEqual(sent["to"], ["karam@sabah.az"])
        msg = sent["msg"]
        self.assertEqual(msg["From"], "SABAH Meetbot <no-reply@sabahhub.com>")
        self.assertTrue(str(make_header(decode_header(msg["Subject"]))).startswith("Meeting notes: Weekly planning ("))
        text, html = [part.get_payload(decode=True).decode() for part in msg.get_payload()]
        self.assertIn("https://meetbot.example/admin/meetings/meeting/", text)
        self.assertIn("<b>Leyla</b>", html)
        self.assertIn("Is the Android build &lt;ready&gt;?", html)  # model output is escaped
        self.assertNotIn("<ready>", html)
        self.assertIsNotNone(self.meeting.delivered_at)
        self.assertEqual(self.meeting.last_error, "")

    def test_non_ascii_title_is_encoded(self):
        claude = FakeClaude(text=json.dumps({**SUMMARY, "title": "Büdcə planlaması — Финансы"}, ensure_ascii=False))
        pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=claude)
        (sent,) = FakeSMTP.sent
        raw = sent["msg"]["Subject"]
        self.assertTrue(raw.isascii())  # RFC 2047-encoded on the wire
        self.assertIn("Büdcə planlaması — Финансы", str(make_header(decode_header(raw))))

    @override_settings(SES_KEY_ID="", SES_ACCESS_SECRET="")
    def test_missing_mail_credentials_do_not_fail_the_summary(self):
        pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=FakeClaude())
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)
        self.assertIn("SES_KEY_ID", self.meeting.last_error)
        self.assertEqual(FakeSMTP.sent, [])

    def test_uses_webhook_lines_when_final_fetch_fails(self):
        Utterance.objects.create(meeting=self.meeting, speaker_name="Karam", speaker_uuid="s1", timestamp_ms=1, text="Salam")
        pipeline.process_due_meetings(client=FakeAttendee(transcript=AttendeeError("down", 502)), claude=FakeClaude())
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.DONE)

    def test_no_speech(self):
        self.meeting.bot_event = "could_not_join_meeting/bot_not_admitted"
        self.meeting.save()
        claude = FakeClaude()
        pipeline.process_due_meetings(client=FakeAttendee(transcript=[]), claude=claude)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.NO_TRANSCRIPT)
        self.assertIn("bot_not_admitted", self.meeting.status_detail)
        self.assertEqual(claude.calls, [])

    @meetbot(MAX_ATTEMPTS=2)
    def test_claude_failure_retries_then_fails(self):
        from meetings.summarizer import SummaryError

        broken = FakeClaude(text="not json")
        pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=broken)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.PROCESSING)
        self.assertEqual(self.meeting.attempts, 1)
        self.assertIn("invalid JSON", self.meeting.last_error)
        # Not due yet → nothing happens.
        self.assertEqual(pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=broken), [])

        later = timezone.now() + timedelta(hours=1)
        pipeline.process_due_meetings(now=later, client=FakeAttendee(transcript=TRANSCRIPT), claude=FakeClaude(error=SummaryError("x")))
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.FAILED)

    def test_refusal_is_final(self):
        pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=FakeClaude(stop_reason="refusal"))
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.FAILED)
        self.assertIn("category=cyber", self.meeting.last_error)

    def test_claim_prevents_double_processing(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.SUMMARIZING)
        self.assertEqual(pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=FakeClaude()), [])

    def test_stale_claim_is_recovered(self):
        Meeting.objects.filter(pk=self.meeting.pk).update(status=Meeting.Status.SUMMARIZING)
        self.assertEqual(pipeline.recover_stale(now=timezone.now() + timedelta(hours=1)), 1)
        self.meeting.refresh_from_db()
        self.assertEqual(self.meeting.status, Meeting.Status.PROCESSING)

    def test_resummarize_with_instructions_does_not_email_again(self):
        pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=FakeClaude())
        self.meeting.refresh_from_db()
        pipeline.queue_resummarize(self.meeting, "Yalnız tapşırıqlara fokuslan")
        claude = FakeClaude()
        pipeline.process_due_meetings(client=FakeAttendee(transcript=TRANSCRIPT), claude=claude)
        self.assertIn("Yalnız tapşırıqlara fokuslan", claude.calls[0]["messages"][0]["content"])
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_resummarize_refused_while_in_call(self):
        self.meeting.status = Meeting.Status.IN_MEETING
        with self.assertRaises(ValueError):
            pipeline.queue_resummarize(self.meeting)

    def test_email_recipients_respect_domains(self):
        self.assertEqual(email_recipients(self.meeting), ["karam@sabah.az"])
        conf = {**settings.MEETBOT, "EMAIL_TO": "invitees", "EMAIL_ALLOWED_DOMAINS": ["sabah.az"]}
        self.assertEqual(email_recipients(self.meeting, conf), ["karam@sabah.az", "leyla@sabah.az"])
        conf["EMAIL_ALLOWED_DOMAINS"] = []
        self.assertIn("guest@client.com", email_recipients(self.meeting, conf))
        conf["EMAIL_TO"] = "none"
        self.assertEqual(email_recipients(self.meeting, conf), [])


class ReconcileTests(TestCase):
    def make(self, **kw):
        defaults = {"dedup_key": kw.pop("key", "k"), "meeting_url": "https://meet.google.com/abc-defg-hij",
                    "start": timezone.now() - timedelta(minutes=30), "end": timezone.now() + timedelta(minutes=30),
                    "status": Meeting.Status.IN_MEETING, "bot_id": "bot_1", "bot_state": "joined_recording"}
        defaults.update(kw)
        meeting = Meeting.objects.create(**defaults)
        Meeting.objects.filter(pk=meeting.pk).update(updated_at=timezone.now() - timedelta(minutes=10))
        return meeting

    def test_missed_end_webhook_is_caught_by_polling(self):
        meeting = self.make()
        client = FakeAttendee(bots={"bot_1": {"id": "bot_1", "state": "ended", "events": [{"type": "post_processing_completed"}]}})
        self.assertEqual(pipeline.reconcile_bots(client=client), 1)
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.PROCESSING)
        self.assertEqual(meeting.bot_event, "post_processing_completed")

    def test_recently_heard_bots_are_not_polled(self):
        meeting = self.make()
        Meeting.objects.filter(pk=meeting.pk).update(updated_at=timezone.now())
        client = FakeAttendee(bots={})
        self.assertEqual(pipeline.reconcile_bots(client=client), 0)

    def test_stuck_bot_is_finished_after_hard_stop(self):
        meeting = self.make(start=timezone.now() - timedelta(hours=8), end=timezone.now() - timedelta(hours=7))
        client = FakeAttendee(bots={"bot_1": {"id": "bot_1", "state": "joined_recording", "events": []}})
        pipeline.reconcile_bots(client=client)
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.PROCESSING)


class TranscriptTextTests(TestCase):
    def test_offsets_are_relative_to_first_line(self):
        meeting = Meeting.objects.create(dedup_key="t", meeting_url="https://meet.google.com/a", start=timezone.now())
        Utterance.objects.create(meeting=meeting, speaker_name="A", speaker_uuid="1", timestamp_ms=10_000, text="bir")
        Utterance.objects.create(meeting=meeting, speaker_name="", speaker_uuid="2", timestamp_ms=3_733_000, text="iki")
        self.assertEqual(transcript_text(meeting.utterances.all()), "[00:00:00] A: bir\n[01:02:03] Unknown speaker: iki")


class SchemaTests(TestCase):
    def test_fake_summary_matches_schema_keys(self):
        self.assertEqual(sorted(SUMMARY), sorted(SUMMARY_SCHEMA["required"]))
        json.dumps(SUMMARY_SCHEMA)  # serialisable for the API
