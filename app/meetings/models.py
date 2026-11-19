import uuid

from django.db import models


class Meeting(models.Model):
    """One Google Meet session the bot attends: calendar link → bot → transcript → summary."""

    class Status(models.TextChoices):
        PENDING = "pending", "Bot hələ yaradılmayıb"
        SCHEDULED = "scheduled", "Bot planlaşdırılıb"
        IN_MEETING = "in_meeting", "Bot görüşdədir"
        PROCESSING = "processing", "Xülasə növbədədir"
        SUMMARIZING = "summarizing", "Xülasə hazırlanır"
        DONE = "done", "Hazırdır"
        NO_TRANSCRIPT = "no_transcript", "Transkript yoxdur"
        FAILED = "failed", "Xəta"
        CANCELLED = "cancelled", "Ləğv edilib"

    class Source(models.TextChoices):
        CALENDAR = "calendar", "SABAH.OS calendar"
        MANUAL = "manual", "API ilə əl ilə"

    # States in which the bot can still be stopped / its meeting is not over.
    ACTIVE_STATUSES = (Status.PENDING, Status.SCHEDULED, Status.IN_MEETING)
    FINAL_STATUSES = (Status.DONE, Status.NO_TRANSCRIPT, Status.FAILED, Status.CANCELLED)

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # sha256(normalised meet url | start) — two members' copies of the same
    # Google meeting collapse onto one row, so only one bot joins.
    dedup_key = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.CALENDAR)
    source_event_ids = models.JSONField(default=list, blank=True)

    title = models.CharField(max_length=300, blank=True, default="")
    description = models.TextField(blank=True, default="")
    meeting_url = models.URLField(max_length=500)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True)
    organizer_email = models.EmailField(blank=True, default="")
    organizer_name = models.CharField(max_length=200, blank=True, default="")
    invitees = models.JSONField(default=list, blank=True)  # [{"name", "email"}]
    participants = models.JSONField(default=list, blank=True)  # seen in the call: [{"name", "uuid", "is_host"}]

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    status_detail = models.CharField(max_length=300, blank=True, default="")

    bot_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    bot_state = models.CharField(max_length=60, blank=True, default="")
    bot_event = models.CharField(max_length=160, blank=True, default="")
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)

    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True, default="")

    # Optional extra guidance for Claude, set when someone re-runs the summary.
    summary_instructions = models.TextField(blank=True, default="")
    summary = models.JSONField(null=True, blank=True)
    summary_markdown = models.TextField(blank=True, default="")
    summary_model = models.CharField(max_length=80, blank=True, default="")
    summary_usage = models.JSONField(null=True, blank=True)
    summarized_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="meeting_status_due_idx")]

    def __str__(self):
        return f"{self.title or self.meeting_url} — {self.start:%Y-%m-%d %H:%M}"


class Utterance(models.Model):
    """One line of what someone said, as Attendee transcribed it."""

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="utterances")
    speaker_name = models.CharField(max_length=200, blank=True, default="")
    speaker_uuid = models.CharField(max_length=200, blank=True, default="")
    timestamp_ms = models.BigIntegerField()
    duration_ms = models.IntegerField(default=0)
    text = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["timestamp_ms", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "speaker_uuid", "timestamp_ms"], name="utterance_unique_per_speaker_time"
            )
        ]

    def __str__(self):
        return f"{self.speaker_name}: {self.text[:60]}"
