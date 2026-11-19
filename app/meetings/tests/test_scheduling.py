from datetime import timedelta

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from meetings.attendee import AttendeeError
from meetings.calendar_source import CalendarRow, FetchResult, make_dedup_key
from meetings.models import Meeting
from meetings.scheduling import (
    ConfigurationError,
    apply_bot_state,
    build_bot_payload,
    schedule_bot,
    sync_calendar,
    transcription_settings,
)

from .fakes import FakeAttendee

LINK = "https://meet.google.com/abc-defg-hij"


def meetbot(**overrides):
    return override_settings(MEETBOT={**settings.MEETBOT, "SABAH_DATABASE_URL": "postgresql://x", **overrides})


class FakeSource:
    enabled = True

    def __init__(self, rows, complete=True):
        self.rows = rows
        self.complete = complete

    def fetch(self, now, until):
        return FetchResult(rows=[r for r in self.rows if r.start < until and r.end > now], complete=self.complete)


def make_meeting(start, **kw):
    defaults = {"dedup_key": make_dedup_key(LINK, start), "meeting_url": LINK, "start": start, "end": start + timedelta(hours=1)}
    defaults.update(kw)
    return Meeting.objects.create(**defaults)


@meetbot()
class BotPayloadTests(TestCase):
    def test_defaults_use_gladia_with_code_switching(self):
        meeting = make_meeting(timezone.now() + timedelta(hours=1))
        join_at = meeting.start - timedelta(seconds=60)
        payload = build_bot_payload(meeting, join_at)

        self.assertEqual(payload["transcription_settings"], {
            "gladia": {"model": "solaria-1", "language_config": {"languages": ["az", "ru", "en"], "code_switching": True}}
        })
        self.assertEqual(payload["recording_settings"], {"format": "none"})
        self.assertEqual(payload["metadata"], {"meetbot_meeting": str(meeting.uid)})
        self.assertEqual(payload["deduplication_key"], meeting.dedup_key)
        self.assertTrue(payload["join_at"].endswith("Z"))
        self.assertEqual(payload["webhooks"][0]["url"], settings.MEETBOT["WEBHOOK_URL"])
        self.assertEqual(payload["automatic_leave_settings"]["max_uptime_seconds"], 240 * 60)
        self.assertNotIn("google_meet_settings", payload)

    @meetbot(TRANSCRIPTION_SETTINGS={"deepgram": {"language": "ru"}}, AUTOMATIC_LEAVE_SETTINGS={"silence_timeout_seconds": 60},
             GOOGLE_MEET_USE_LOGIN=True, JOIN_MESSAGE="")
    def test_overrides(self):
        payload = build_bot_payload(make_meeting(timezone.now()))
        self.assertEqual(payload["transcription_settings"], {"deepgram": {"language": "ru"}})
        self.assertEqual(payload["automatic_leave_settings"]["silence_timeout_seconds"], 60)
        self.assertEqual(payload["google_meet_settings"]["use_login"], True)
        self.assertNotIn("bot_chat_message", payload)
        self.assertNotIn("join_at", payload)


class TranscriptionPresetTests(SimpleTestCase):
    def preset(self, provider, languages=("az", "ru", "en")):
        conf = {**settings.MEETBOT, "TRANSCRIPTION_PROVIDER": provider, "LANGUAGES": list(languages), "TRANSCRIPTION_SETTINGS": None}
        return transcription_settings(conf)

    def test_presets(self):
        self.assertEqual(self.preset("elevenlabs"), {"elevenlabs": {"model_id": "scribe_v2"}})
        self.assertEqual(self.preset("elevenlabs", ["ru"]), {"elevenlabs": {"model_id": "scribe_v2", "language_code": "rus"}})
        openai = self.preset("openai")["openai"]
        self.assertEqual(openai["model"], "gpt-4o-transcribe")
        self.assertNotIn("language", openai)
        self.assertIn("Azerbaijani, Russian, English", openai["prompt"])
        self.assertEqual(self.preset("deepgram"), {"deepgram": {"model": "nova-3", "language": "multi"}})
        self.assertEqual(self.preset("gladia", ["az"]), {"gladia": {"model": "solaria-1", "language_config": {"languages": ["az"]}}})
        self.assertIn("meeting_closed_captions", self.preset("captions"))

    def test_unknown_provider_is_a_clear_error(self):
        with self.assertRaises(ConfigurationError):
            self.preset("whisperx")


@meetbot()
class ScheduleBotTests(TestCase):
    def test_future_meeting_gets_scheduled_bot(self):
        now = timezone.now()
        meeting = make_meeting(now + timedelta(minutes=20))
        client = FakeAttendee()
        schedule_bot(meeting, client=client, now=now)
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.SCHEDULED)
        self.assertEqual(meeting.bot_id, "bot_1")
        self.assertIn("join_at", client.created[0])

    def test_meeting_starting_now_joins_immediately(self):
        now = timezone.now()
        meeting = make_meeting(now - timedelta(minutes=5))
        client = FakeAttendee(bot_state="joining")
        schedule_bot(meeting, client=client, now=now)
        self.assertNotIn("join_at", client.created[0])

    def test_finished_meeting_is_not_joined(self):
        now = timezone.now()
        meeting = make_meeting(now - timedelta(hours=2))
        client = FakeAttendee()
        schedule_bot(meeting, client=client, now=now)
        self.assertEqual(meeting.status, Meeting.Status.CANCELLED)
        self.assertEqual(client.created, [])

    @meetbot(MAX_ATTEMPTS=2)
    def test_failure_backs_off_then_gives_up(self):
        now = timezone.now()
        meeting = make_meeting(now + timedelta(minutes=20))
        client = FakeAttendee(fail_create=AttendeeError("boom", 500, "boom"))
        schedule_bot(meeting, client=client, now=now)
        self.assertEqual(meeting.status, Meeting.Status.PENDING)
        self.assertEqual(meeting.attempts, 1)
        self.assertGreater(meeting.next_attempt_at, now)
        schedule_bot(meeting, client=client, now=now)
        self.assertEqual(meeting.status, Meeting.Status.FAILED)

    def test_recovers_bot_created_by_an_earlier_attempt(self):
        now = timezone.now()
        meeting = make_meeting(now + timedelta(minutes=20))
        client = FakeAttendee(fail_create=AttendeeError("dup", 400, '{"error": "Deduplication key already in use"}'))
        client.dedup_results = [{"id": "bot_old", "state": "ended"}, {"id": "bot_live", "state": "scheduled"}]
        schedule_bot(meeting, client=client, now=now)
        self.assertEqual(meeting.bot_id, "bot_live")
        self.assertEqual(meeting.status, Meeting.Status.SCHEDULED)


class ApplyBotStateTests(TestCase):
    def test_lifecycle(self):
        meeting = make_meeting(timezone.now(), status=Meeting.Status.SCHEDULED, bot_id="bot_1")
        apply_bot_state(meeting, "waiting_room")
        self.assertEqual(meeting.status, Meeting.Status.SCHEDULED)
        self.assertIn("gözləmə", meeting.status_detail)
        apply_bot_state(meeting, "joined_recording")
        self.assertEqual(meeting.status, Meeting.Status.IN_MEETING)
        self.assertIsNotNone(meeting.joined_at)
        apply_bot_state(meeting, "post_processing")
        self.assertEqual(meeting.status, Meeting.Status.IN_MEETING)
        apply_bot_state(meeting, "ended", "post_processing_completed")
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.PROCESSING)
        self.assertEqual(meeting.bot_event, "post_processing_completed")
        self.assertIsNotNone(meeting.next_attempt_at)

    def test_never_moves_backwards(self):
        meeting = make_meeting(timezone.now(), status=Meeting.Status.DONE, bot_id="bot_1")
        apply_bot_state(meeting, "joined_recording")
        self.assertEqual(meeting.status, Meeting.Status.DONE)


@meetbot()
class SyncCalendarTests(TestCase):
    def row(self, id, start, **kw):
        return CalendarRow(id=id, title=kw.pop("title", "Sprint"), description="", start=start, end=start + timedelta(hours=1),
                           meet_link=kw.pop("link", LINK), organizer_email="karam@sabah.az", organizer_name="Karam", **kw)

    def test_creates_one_bot_per_meeting_and_skips_opt_outs(self):
        now = timezone.now()
        start = now + timedelta(minutes=15)
        source = FakeSource([
            self.row(1, start),
            self.row(2, start, link=LINK + "?authuser=1", source="google"),  # member's imported copy
            self.row(3, start, link="https://meet.google.com/xyz-abcd-efg", title="1:1 #nobot"),
            self.row(4, now + timedelta(hours=5), link="https://meet.google.com/far-away-mtg"),  # beyond lookahead
        ])
        client = FakeAttendee()

        result = sync_calendar(now=now, source=source, client=client)

        self.assertEqual(len(client.created), 1)
        self.assertEqual(Meeting.objects.count(), 1)
        self.assertEqual(Meeting.objects.get().source_event_ids, [1, 2])
        self.assertEqual(len(result.skipped), 1)

        # Second pass is a no-op.
        sync_calendar(now=now + timedelta(minutes=1), source=source, client=client)
        self.assertEqual(len(client.created), 1)

    def test_removed_or_moved_event_cancels_its_scheduled_bot(self):
        now = timezone.now()
        start = now + timedelta(minutes=15)
        client = FakeAttendee()
        sync_calendar(now=now, source=FakeSource([self.row(1, start)]), client=client)

        moved = FakeSource([self.row(1, start + timedelta(minutes=10))])
        result = sync_calendar(now=now + timedelta(minutes=1), source=moved, client=client)

        self.assertEqual(client.deleted, ["bot_1"])
        self.assertEqual(len(result.cancelled), 1)
        self.assertEqual(Meeting.objects.filter(status=Meeting.Status.CANCELLED).count(), 1)
        self.assertEqual(Meeting.objects.filter(status=Meeting.Status.SCHEDULED).count(), 1)

    def test_partial_fetch_never_cancels(self):
        now = timezone.now()
        client = FakeAttendee()
        sync_calendar(now=now, source=FakeSource([self.row(1, now + timedelta(minutes=15))]), client=client)
        sync_calendar(now=now, source=FakeSource([], complete=False), client=client)
        self.assertEqual(client.deleted, [])
        self.assertEqual(Meeting.objects.get().status, Meeting.Status.SCHEDULED)

    def test_bot_already_joining_is_left_alone(self):
        now = timezone.now()
        client = FakeAttendee()
        sync_calendar(now=now, source=FakeSource([self.row(1, now + timedelta(minutes=15))]), client=client)
        Meeting.objects.update(bot_state="joining")
        sync_calendar(now=now, source=FakeSource([]), client=client)
        self.assertEqual(client.deleted, [])

    @meetbot(SABAH_DATABASE_URL="")
    def test_disabled_without_database_url(self):
        from meetings.calendar_source import SabahCalendarSource

        result = sync_calendar(source=SabahCalendarSource(), client=FakeAttendee())
        self.assertEqual(result.fetched, 0)
