from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "SABAH Meetbot"
admin.site.site_title = "SABAH Meetbot"
admin.site.index_title = "Görüşlər"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("meetings.urls")),
]
