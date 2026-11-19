from django.urls import path

from . import api, webhooks

urlpatterns = [
    path("healthz", api.healthz),
    path("webhooks/attendee/", webhooks.attendee_webhook),
    path("api/meetings/", api.meetings),
    path("api/meetings/<uuid:uid>/", api.meeting_detail),
    path("api/meetings/<uuid:uid>/transcript/", api.meeting_transcript),
    path("api/meetings/<uuid:uid>/summarize/", api.meeting_summarize),
    path("api/meetings/<uuid:uid>/ask/", api.meeting_ask),
    path("api/meetings/<uuid:uid>/leave/", api.meeting_leave),
]
