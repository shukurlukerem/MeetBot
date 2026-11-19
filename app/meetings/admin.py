from django.contrib import admin, messages
from django.db.models import Count
from django.utils.html import format_html

from .models import Meeting
from .pipeline import queue_resummarize
from .scheduling import cancel_meeting
from .summarizer import transcript_text


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("start", "title", "client", "organizer_email", "status", "bot_state", "utterance_count")
    list_filter = ("client", "status", "source", "bot_state")
    search_fields = ("title", "organizer_email", "meeting_url", "bot_id", "summary_markdown")
    date_hierarchy = "start"
    ordering = ("-start",)
    actions = ("resummarize", "cancel_bots")
    readonly_fields = (
        "uid",
        "source",
        "client",
        "dedup_key",
        "source_event_ids",
        "meeting_url",
        "start",
        "end",
        "organizer_email",
        "organizer_name",
        "invitees",
        "participants",
        "status",
        "status_detail",
        "bot_id",
        "bot_state",
        "bot_event",
        "joined_at",
        "left_at",
        "attempts",
        "next_attempt_at",
        "last_error",
        "summary_view",
        "summary_model",
        "summary_usage",
        "summarized_at",
        "delivered_at",
        "transcript_view",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("title", "status", "status_detail", "start", "end", "meeting_url", "organizer_name", "organizer_email")}),
        ("Xülasə", {"fields": ("summary_view", "summary_instructions", "summary_model", "summarized_at", "delivered_at")}),
        ("Transkript", {"fields": ("transcript_view",)}),
        ("İştirakçılar", {"fields": ("invitees", "participants"), "classes": ("collapse",)}),
        (
            "Bot",
            {
                "fields": ("bot_id", "bot_state", "bot_event", "joined_at", "left_at", "attempts", "next_attempt_at", "last_error"),
                "classes": ("collapse",),
            },
        ),
        ("Texniki", {"fields": ("uid", "source", "client", "dedup_key", "source_event_ids", "summary_usage", "description", "created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def has_add_permission(self, request):
        return False  # meetings come from the calendar or the API

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_utterances=Count("utterances"))

    @admin.display(description="Sətir", ordering="_utterances")
    def utterance_count(self, obj):
        return obj._utterances

    @admin.display(description="Xülasə")
    def summary_view(self, obj):
        if not obj.summary_markdown:
            return "—"
        return format_html('<pre style="white-space:pre-wrap;max-width:900px">{}</pre>', obj.summary_markdown)

    @admin.display(description="Transkript")
    def transcript_view(self, obj):
        text = transcript_text(obj.utterances.all())
        if not text:
            return "—"
        return format_html('<pre style="white-space:pre-wrap;max-height:600px;overflow:auto;max-width:900px">{}</pre>', text)

    @admin.action(description="Xülasəni yenidən hazırla")
    def resummarize(self, request, queryset):
        queued = 0
        for meeting in queryset:
            try:
                queue_resummarize(meeting)
                queued += 1
            except ValueError as exc:
                self.message_user(request, f"{meeting}: {exc}", messages.WARNING)
        if queued:
            self.message_user(request, f"{queued} görüş növbəyə əlavə edildi.")

    @admin.action(description="Botu dayandır / ləğv et")
    def cancel_bots(self, request, queryset):
        stopped = 0
        for meeting in queryset.filter(status__in=Meeting.ACTIVE_STATUSES):
            cancel_meeting(meeting, f"{request.user} tərəfindən ləğv edilib")
            stopped += 1
        self.message_user(request, f"{stopped} bot dayandırıldı.")
