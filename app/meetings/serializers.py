def _iso(value):
    return value.isoformat() if value else None


def meeting_to_dict(meeting, include_summary=False):
    data = {
        "id": str(meeting.uid),
        "source": meeting.source,
        "client": meeting.client,
        "title": meeting.title,
        "meeting_url": meeting.meeting_url,
        "start": _iso(meeting.start),
        "end": _iso(meeting.end),
        "organizer": {"email": meeting.organizer_email, "name": meeting.organizer_name},
        "invitees": meeting.invitees,
        "participants": meeting.participants,
        "sabah_event_ids": meeting.source_event_ids,
        "status": meeting.status,
        "status_detail": meeting.status_detail,
        "bot": {
            "id": meeting.bot_id,
            "state": meeting.bot_state,
            "last_event": meeting.bot_event,
            "joined_at": _iso(meeting.joined_at),
            "left_at": _iso(meeting.left_at),
        },
        "summarized_at": _iso(meeting.summarized_at),
        "updated_at": _iso(meeting.updated_at),
    }
    if include_summary:
        data["summary"] = meeting.summary
        data["summary_markdown"] = meeting.summary_markdown
        data["summary_model"] = meeting.summary_model
    return data


def utterance_to_dict(utterance):
    return {
        "speaker_name": utterance.speaker_name,
        "speaker_uuid": utterance.speaker_uuid,
        "timestamp_ms": utterance.timestamp_ms,
        "duration_ms": utterance.duration_ms,
        "text": utterance.text,
    }
