"""Reads upcoming Google Meet meetings out of the SABAH.OS calendar.

The bridge never writes to SABAH.OS: it opens a read-only session against the
SABAH.OS Postgres database (`SABAH_DATABASE_URL`) and selects from three
tables — `calendars_calendarevent`, `calendars_eventattendee` and `users`.
Give it a dedicated role with SELECT on just those (see README).

SABAH.OS materialises recurring events (one row per occurrence) and imports a
copy of a Google meeting into *each* member's calendar, so the same Meet link
can appear on several rows at the same start time. Rows are grouped by
(normalised link, start) and each group becomes one meeting → one bot.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from django.conf import settings

logger = logging.getLogger(__name__)

MAX_ROWS = 500

EVENTS_SQL = """
SELECT e.id, e.title, e.description, e.start, e."end", e.meet_link, e.visibility, e.source,
       u.email AS organizer_email,
       TRIM(CONCAT(u.first_name, ' ', u.last_name)) AS organizer_name
FROM calendars_calendarevent e
JOIN users u ON u.id = e.organizer_id
WHERE e.meet_link <> ''
  AND e.status = 'confirmed'
  AND e.kind = 'event'
  AND e.all_day = FALSE
  AND e.start < %(until)s
  AND e."end" > %(now)s
ORDER BY e.start, e.id
LIMIT %(limit)s
"""

ATTENDEES_SQL = """
SELECT a.event_id, a.response, a.is_organizer,
       COALESCE(NULLIF(u.email, ''), a.email) AS email,
       TRIM(CONCAT(u.first_name, ' ', u.last_name)) AS name
FROM calendars_eventattendee a
LEFT JOIN users u ON u.id = a.user_id
WHERE a.event_id = ANY(%(ids)s)
ORDER BY a.event_id, a.is_organizer DESC, a.id
"""


@dataclass
class CalendarRow:
    id: int
    title: str
    description: str
    start: datetime
    end: datetime
    meet_link: str
    visibility: str = "default"
    source: str = "sabah"
    organizer_email: str = ""
    organizer_name: str = ""
    attendees: list = field(default_factory=list)  # {"email", "name", "response", "is_organizer"}


@dataclass
class CandidateMeeting:
    dedup_key: str
    meeting_url: str
    start: datetime
    end: datetime
    title: str
    description: str
    organizer_email: str
    organizer_name: str
    invitees: list
    event_ids: list
    rows: list


@dataclass
class FetchResult:
    rows: list
    complete: bool  # False when MAX_ROWS truncated the result — never cancel on a partial view


def normalize_meet_url(url):
    """`https://meet.google.com/abc-defg-hij?authuser=1` → `https://meet.google.com/abc-defg-hij`."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    if host == "meet.google.com":
        path = path.lower()
    return f"https://{host}{path}"


def make_dedup_key(meeting_url, start, client=""):
    raw = f"{normalize_meet_url(meeting_url)}|{start.isoformat()}"
    if client:
        raw = f"{client}|{raw}"  # dev and prod each get their own bot
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _has_tag(rows, tag):
    if not tag:
        return False
    tag = tag.lower()
    return any(tag in f"{r.title}\n{r.description}".lower() for r in rows)


def group_rows(rows):
    groups = {}
    for row in rows:
        key = make_dedup_key(row.meet_link, row.start)
        groups.setdefault(key, []).append(row)

    candidates = []
    for key, members in groups.items():
        # A row SABAH.OS created itself is the original; imported copies come second.
        members.sort(key=lambda r: (r.source != "sabah", r.id))
        primary = members[0]

        organizer_email, organizer_name = primary.organizer_email, primary.organizer_name
        invitees = {}
        for row in members:
            for att in row.attendees:
                email = (att.get("email") or "").strip().lower()
                if not email:
                    continue
                if att.get("is_organizer"):
                    organizer_email, organizer_name = email, att.get("name") or organizer_name
                if att.get("response") == "declined":
                    continue
                invitees.setdefault(email, {"email": email, "name": att.get("name") or ""})
            # The member whose calendar holds the copy is in the meeting too.
            if row.organizer_email:
                email = row.organizer_email.lower()
                invitees.setdefault(email, {"email": email, "name": row.organizer_name})

        candidates.append(
            CandidateMeeting(
                dedup_key=key,
                meeting_url=normalize_meet_url(primary.meet_link),
                start=primary.start,
                end=max(r.end for r in members),
                title=primary.title,
                description=primary.description,
                organizer_email=(organizer_email or "").lower(),
                organizer_name=organizer_name or "",
                invitees=sorted(invitees.values(), key=lambda i: i["email"]),
                event_ids=sorted(r.id for r in members),
                rows=members,
            )
        )
    candidates.sort(key=lambda c: c.start)
    return candidates


def should_join(candidate, conf=None):
    """Returns (join?, reason). Pure — the policy is unit-tested on its own."""
    conf = conf or settings.MEETBOT
    rows = candidate.rows
    if _has_tag(rows, conf["SKIP_TAG"]):
        return False, f"opt-out tag {conf['SKIP_TAG']}"
    if conf["SKIP_PRIVATE"] and all(r.visibility == "private" for r in rows):
        return False, "private event"
    if conf["JOIN_POLICY"] == "tagged" and not _has_tag(rows, conf["JOIN_TAG"]):
        return False, f"not tagged {conf['JOIN_TAG']}"
    allow = conf["ORGANIZER_ALLOWLIST"]
    if allow:
        organizers = {candidate.organizer_email} | {r.organizer_email.lower() for r in rows}
        if not organizers & set(allow):
            return False, "organizer not in allowlist"
    return True, ""


class SabahCalendarSource:
    """Read-only window onto the SABAH.OS calendar tables."""

    def __init__(self, database_url=None):
        self.database_url = database_url or settings.MEETBOT["SABAH_DATABASE_URL"]

    @property
    def enabled(self):
        return bool(self.database_url)

    def fetch(self, now, until):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(
            self.database_url,
            connect_timeout=10,
            options="-c default_transaction_read_only=on -c statement_timeout=15000",
            row_factory=dict_row,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(EVENTS_SQL, {"now": now, "until": until, "limit": MAX_ROWS})
                event_rows = cur.fetchall()
                attendees = {}
                if event_rows:
                    cur.execute(ATTENDEES_SQL, {"ids": [r["id"] for r in event_rows]})
                    for att in cur.fetchall():
                        attendees.setdefault(att["event_id"], []).append(att)

        rows = [
            CalendarRow(
                id=r["id"],
                title=r["title"] or "",
                description=r["description"] or "",
                start=r["start"],
                end=r["end"],
                meet_link=r["meet_link"],
                visibility=r["visibility"],
                source=r["source"],
                organizer_email=(r["organizer_email"] or "").lower(),
                organizer_name=r["organizer_name"] or "",
                attendees=attendees.get(r["id"], []),
            )
            for r in event_rows
            if urlsplit(r["meet_link"]).netloc.lower() == "meet.google.com"
        ]
        return FetchResult(rows=rows, complete=len(event_rows) < MAX_ROWS)
