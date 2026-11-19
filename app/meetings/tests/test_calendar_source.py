from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.test import SimpleTestCase

from meetings.calendar_source import CalendarRow, group_rows, make_dedup_key, normalize_meet_url, should_join

T0 = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)


def row(id, link="https://meet.google.com/abc-defg-hij", start=T0, title="Planlama", **kw):
    return CalendarRow(
        id=id,
        title=title,
        description=kw.pop("description", ""),
        start=start,
        end=start + timedelta(hours=1),
        meet_link=link,
        **kw,
    )


def conf(**overrides):
    return {**settings.MEETBOT, **overrides}


class NormalizeTests(SimpleTestCase):
    def test_strips_query_and_case(self):
        self.assertEqual(
            normalize_meet_url("https://Meet.Google.com/ABC-defg-HIJ/?authuser=1&hs=122"),
            "https://meet.google.com/abc-defg-hij",
        )

    def test_same_meeting_same_key(self):
        a = make_dedup_key("https://meet.google.com/abc-defg-hij?authuser=0", T0)
        b = make_dedup_key("https://meet.google.com/ABC-DEFG-HIJ", T0)
        self.assertEqual(a, b)
        self.assertNotEqual(a, make_dedup_key("https://meet.google.com/abc-defg-hij", T0 + timedelta(days=7)))


class GroupRowsTests(SimpleTestCase):
    def test_copies_on_several_calendars_become_one_meeting(self):
        original = row(1, source="sabah", organizer_email="karam@sabah.az", organizer_name="Karam",
                       attendees=[{"email": "leyla@sabah.az", "name": "Leyla", "response": "accepted", "is_organizer": False},
                                  {"email": "out@client.com", "name": "", "response": "declined", "is_organizer": False}])
        copy = row(7, link="https://meet.google.com/abc-defg-hij?authuser=1", source="google",
                   organizer_email="leyla@sabah.az", organizer_name="Leyla", title="Planlama (copy)")
        other_week = row(9, start=T0 + timedelta(days=7))

        groups = group_rows([copy, original, other_week])

        self.assertEqual(len(groups), 2)
        first = groups[0]
        self.assertEqual(first.event_ids, [1, 7])
        self.assertEqual(first.title, "Planlama")  # the SABAH-created row wins over the imported copy
        self.assertEqual(first.organizer_email, "karam@sabah.az")
        self.assertEqual([i["email"] for i in first.invitees], ["karam@sabah.az", "leyla@sabah.az"])  # declined guest dropped

    def test_attendee_flagged_organizer_wins(self):
        r = row(1, organizer_email="owner@sabah.az",
                attendees=[{"email": "Boss@Sabah.az", "name": "Boss", "response": "accepted", "is_organizer": True}])
        (group,) = group_rows([r])
        self.assertEqual(group.organizer_email, "boss@sabah.az")
        self.assertEqual(group.organizer_name, "Boss")


class ShouldJoinTests(SimpleTestCase):
    def _cand(self, **kw):
        return group_rows([row(1, organizer_email="karam@sabah.az", **kw)])[0]

    def test_default_joins_everything(self):
        self.assertEqual(should_join(self._cand(), conf(JOIN_POLICY="all")), (True, ""))

    def test_opt_out_tag(self):
        join, reason = should_join(self._cand(description="Gizli #NoBot"), conf())
        self.assertFalse(join)
        self.assertIn("#nobot", reason)

    def test_private_event_skipped_unless_disabled(self):
        cand = self._cand(visibility="private")
        self.assertFalse(should_join(cand, conf(SKIP_PRIVATE=True))[0])
        self.assertTrue(should_join(cand, conf(SKIP_PRIVATE=False))[0])

    def test_tagged_policy(self):
        self.assertFalse(should_join(self._cand(), conf(JOIN_POLICY="tagged"))[0])
        self.assertTrue(should_join(self._cand(title="Sprint #meetbot"), conf(JOIN_POLICY="tagged"))[0])

    def test_organizer_allowlist(self):
        self.assertFalse(should_join(self._cand(), conf(ORGANIZER_ALLOWLIST=["ceo@sabah.az"]))[0])
        self.assertTrue(should_join(self._cand(), conf(ORGANIZER_ALLOWLIST=["karam@sabah.az"]))[0])
