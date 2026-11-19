import json
from types import SimpleNamespace

from meetings.attendee import AttendeeError


class FakeAttendee:
    """Records calls; returns canned bots/transcripts."""

    def __init__(self, transcript=None, fail_create=None, bot_state="scheduled", bots=None):
        self.created = []
        self.deleted = []
        self.left = []
        self.transcript = transcript or []
        self.fail_create = fail_create
        self.bot_state = bot_state
        self.bots = bots or {}
        self.dedup_results = []
        self._n = 0

    def create_bot(self, payload):
        if self.fail_create:
            raise self.fail_create
        self._n += 1
        self.created.append(payload)
        return {"id": f"bot_{self._n}", "state": self.bot_state}

    def find_bots_by_dedup_key(self, key):
        return self.dedup_results

    def get_bot(self, bot_id):
        if bot_id not in self.bots:
            raise AttendeeError("not found", 404, "")
        return self.bots[bot_id]

    def delete_bot(self, bot_id):
        self.deleted.append(bot_id)

    def leave_meeting(self, bot_id):
        self.left.append(bot_id)

    def get_transcript(self, bot_id):
        if isinstance(self.transcript, Exception):
            raise self.transcript
        return self.transcript


SUMMARY = {
    "title": "Weekly planning",
    "overview": "The team agreed the scope of next week's mobile release.",
    "key_points": ["The mobile release ships on Friday"],
    "decisions": ["Release on 22 November"],
    "action_items": [{"task": "Write the release notes", "owner": "Leyla", "due": "Friday"}, {"task": "QA pass", "owner": "", "due": ""}],
    "open_questions": ["Is the Android build <ready>?"],
    "speakers": [{"name": "Karam", "contribution": "Walked through the plan"}],
}


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        if isinstance(self.message, Exception):
            raise self.message
        return self.message


class FakeClaude:
    def __init__(self, text=None, stop_reason="end_turn", error=None):
        self.calls = []
        usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.message = error or SimpleNamespace(
            stop_reason=stop_reason,
            stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text if text is not None else json.dumps(SUMMARY, ensure_ascii=False))],
            model="claude-opus-5-5",
            usage=usage,
        )
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.message)


def transcript_entry(name, uuid, ts, text):
    return {
        "speaker_name": name,
        "speaker_uuid": uuid,
        "speaker_user_uuid": None,
        "speaker_is_host": False,
        "timestamp_ms": ts,
        "duration_ms": 1500,
        "transcription": {"transcript": text},
    }
