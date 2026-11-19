from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from meetings import pipeline
from meetings.events import EventPayloadError, meeting_for_event, sync_events
from meetings.models import EventLink, Meeting

from .fakes import FakeAttendee

LINK = "https://meet.google.com/abc-defg-hij"
PROD, DEV = "prod-token-1111", "dev-token-2222"


def meetbot(**overrides):
    return override_settings(
        MEETBOT={**settings.MEETBOT, "API_TOKEN": "admin-token", "API_CLIENTS": {PROD: "prod", DEV: "dev"}, **overrides}
    )


def event(event_id, start, enabled=True, link=LINK, **kw):
    return {
        "event_id": event_id,
        "enabled": enabled,
        "meeting_url": link,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
        "title": kw.pop("title", "Sprint review"),
        "organizer": {"email": "karam@sabahhub.com", "name": "Karam"},
        "invitees": kw.pop("invitees", [{"email": "leyla@sabahhub.com", "name": "Leyla"}]),
        **kw,
    }


@meetbot()
class SyncEventsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.later = self.now + timedelta(days=2)  # beyond the 30-minute lookahead
        self.client_ = FakeAttendee()

    def sync(self, items, client="prod", **kw):
        return sync_events(client, items, attendee=self.client_, now=self.now, **kw)

    def test_future_meeting_waits_without_a_bot(self):
        (result,) = self.sync([event("41", self.later)])
        meeting = result["meeting"]
        self.assertEqual(meeting.status, Meeting.Status.PENDING)
        self.assertEqual(meeting.client, "prod")
        self.assertEqual(meeting.source, Meeting.Source.EVENT)
        self.assertEqual(meeting.source_event_ids, [41])
        self.assertEqual(self.client_.created, [])

        # The worker creates the bot once the meeting is within the lookahead.
        pipeline.schedule_due(now=self.later - timedelta(minutes=10), client=self.client_)
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.SCHEDULED)
        self.assertEqual(len(self.client_.created), 1)

    def test_meeting_starting_soon_gets_a_bot_immediately(self):
        (result,) = self.sync([event("41", self.now + timedelta(minutes=5))])
        self.assertEqual(result["meeting"].status, Meeting.Status.SCHEDULED)
        self.assertEqual(len(self.client_.created), 1)

    def test_two_members_copies_share_one_bot(self):
        start = self.now + timedelta(minutes=5)
        self.sync([event("41", start), event("77", start, link=LINK + "?authuser=1",
                                                invitees=[{"email": "rauf@sabahhub.com", "name": "Rauf"}])])
        self.assertEqual(Meeting.objects.count(), 1)
        meeting = Meeting.objects.get()
        self.assertEqual(meeting.source_event_ids, [41, 77])
        self.assertEqual([i["email"] for i in meeting.invitees], ["leyla@sabahhub.com", "rauf@sabahhub.com"])
        self.assertEqual(len(self.client_.created), 1)

        # One member turns it off — the other still wants the bot.
        self.sync([event("41", start, enabled=False)])
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.SCHEDULED)
        self.assertEqual(self.client_.deleted, [])
        # The last one off cancels it.
        self.sync([event("77", start, enabled=False)])
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, Meeting.Status.CANCELLED)
        self.assertEqual(self.client_.deleted, ["bot_1"])

    def test_moving_an_event_moves_the_bot(self):
        start = self.now + timedelta(minutes=10)
        self.sync([event("41", start)])
        self.sync([event("41", start + timedelta(hours=1))])
        old, new = Meeting.objects.order_by("start")
        self.assertEqual(old.status, Meeting.Status.CANCELLED)
        self.assertEqual(self.client_.deleted, ["bot_1"])
        self.assertEqual(new.status, Meeting.Status.PENDING)
        self.assertEqual(meeting_for_event("prod", "41"), new)

    def test_off_and_on_again_revives_the_meeting(self):
        start = self.now + timedelta(minutes=10)
        self.sync([event("41", start)])
        self.sync([event("41", start, enabled=False)])
        (result,) = self.sync([event("41", start)])
        self.assertEqual(Meeting.objects.count(), 1)
        self.assertEqual(result["meeting"].status, Meeting.Status.SCHEDULED)
        self.assertEqual(result["meeting"].bot_id, "bot_2")

    def test_dev_and_prod_never_share(self):
        start = self.now + timedelta(minutes=5)
        self.sync([event("41", start)], client="prod")
        self.sync([event("41", start)], client="dev")
        self.assertEqual(sorted(Meeting.objects.values_list("client", flat=True)), ["dev", "prod"])
        self.assertNotEqual(meeting_for_event("prod", "41"), meeting_for_event("dev", "41"))

    def test_window_sweep_turns_off_events_the_backend_no_longer_reports(self):
        self.sync([event("41", self.later), event("42", self.later + timedelta(hours=2))])
        window = (self.now, self.now + timedelta(days=7))
        self.sync([event("42", self.later + timedelta(hours=2))], window=window)
        self.assertFalse(EventLink.objects.filter(event_id="41").exists())
        self.assertEqual(
            sorted(Meeting.objects.values_list("status", flat=True)), [Meeting.Status.CANCELLED, Meeting.Status.PENDING]
        )

    def test_past_meetings_are_not_created(self):
        (result,) = self.sync([event("41", self.now - timedelta(hours=3))])
        self.assertIsNone(result["meeting"])
        self.assertEqual(Meeting.objects.count(), 0)

    def test_finished_meeting_is_left_alone(self):
        start = self.now - timedelta(minutes=30)
        self.sync([event("41", start)])
        Meeting.objects.update(status=Meeting.Status.DONE)
        (result,) = self.sync([event("41", start, title="Renamed")])
        self.assertEqual(result["meeting"].status, Meeting.Status.DONE)
        self.assertEqual(result["meeting"].title, "Sprint review")

    def test_invalid_payloads(self):
        with self.assertRaises(EventPayloadError):
            self.sync([event("41", self.later, link="https://zoom.us/j/1")])
        with self.assertRaises(EventPayloadError):
            self.sync([{"enabled": True}])
        # A disabled event needs nothing but its id.
        self.assertEqual(self.sync([{"event_id": "9", "enabled": False}]), [{"event_id": "9", "meeting": None}])


@meetbot()
class EventApiTests(TestCase):
    def call(self, method, path, body=None, token=PROD):
        fn = getattr(self.client, method)
        kwargs = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if body is not None:
            return fn(path, data=body, content_type="application/json", **kwargs)
        return fn(path, **kwargs)

    def test_sync_and_read_back(self):
        start = timezone.now() + timedelta(days=1)
        response = self.call("post", "/api/events/sync/", {"events": [event(41, start)]})
        self.assertEqual(response.status_code, 200)
        (result,) = response.json()["results"]
        self.assertEqual(result["event_id"], "41")
        self.assertEqual(result["meeting"]["status"], "pending")
        self.assertEqual(result["meeting"]["client"], "prod")

        detail = self.call("get", "/api/events/41/")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("summary_markdown", detail.json())
        # dev cannot see prod's event, and the admin token is not an environment.
        self.assertEqual(self.call("get", "/api/events/41/", token=DEV).status_code, 404)
        self.assertEqual(self.call("get", "/api/events/41/", token="admin-token").status_code, 403)

    def test_meeting_list_is_scoped_per_client(self):
        start = timezone.now() + timedelta(days=1)
        self.call("post", "/api/events/sync/", {"events": [event(41, start)]}, token=PROD)
        self.call("post", "/api/events/sync/", {"events": [event(41, start)]}, token=DEV)
        self.assertEqual(len(self.call("get", "/api/meetings/", token=PROD).json()["results"]), 1)
        self.assertEqual(len(self.call("get", "/api/meetings/", token="admin-token").json()["results"]), 2)
        prod_uid = self.call("get", "/api/meetings/", token=PROD).json()["results"][0]["id"]
        self.assertEqual(self.call("get", f"/api/meetings/{prod_uid}/", token=DEV).status_code, 404)

    def test_bad_requests(self):
        self.assertEqual(self.call("post", "/api/events/sync/", {"events": "x"}).status_code, 400)
        self.assertEqual(self.call("post", "/api/events/sync/", {"events": [{"enabled": True}]}).status_code, 400)
        self.assertEqual(
            self.call("post", "/api/events/sync/", {"events": [], "window": {"start": "x", "end": "y"}}).status_code, 400
        )
        self.assertEqual(self.call("post", "/api/events/sync/", {"events": []}, token="nope").status_code, 401)

    def test_manual_meeting_with_client_token_belongs_to_that_client(self):
        with mock.patch("meetings.scheduling.AttendeeClient", return_value=FakeAttendee(bot_state="joining")):
            body = self.call("post", "/api/meetings/", {"meeting_url": LINK}, token=DEV).json()
        self.assertEqual(body["client"], "dev")
