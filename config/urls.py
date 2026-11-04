"""URL configuration for config project."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.staticfiles.views import serve as serve_static
from django.urls import include, path, re_path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("inventory.urls")),
]

# --- Static files -----------------------------------------------------------
# Serve /static/ ourselves, unconditionally.
#
# DEBUG is False (deliberately — see settings.py), and with DEBUG off runserver
# refuses to serve static files unless started with `--insecure`. The whole app
# has only TWO static assets (inventory/snow.jpg and inventory/soti.svg): the
# CSS lives in a <style> block in base.html and Bootstrap comes from a CDN. So
# forgetting the flag doesn't produce an obviously-broken page that makes you go
# look for the cause — it produces a page that looks completely normal except the
# ServiceNow and SOTI icons silently 404. That cost real debugging time once
# already, hence this route.
#
# `insecure=True` is what `--insecure` sets internally, so this grants no access
# the documented run command didn't already grant. The staticfiles view resolves
# paths through the staticfiles FINDERS, so it can only ever serve files inside
# an app's static/ dir — it cannot reach db.sqlite3, settings.py or anything else
# on the share. Putting a real web server in front of this is still the right
# long-term answer (§9).
urlpatterns += [
    re_path(r"^static/(?P<path>.*)$", serve_static, {"insecure": True}),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
