"""Gate every database change behind a signed-in user who holds the edit permission.

Reads (GET/HEAD/OPTIONS) are always allowed — browsing and the reports stay open
to anyone on the LAN. Any unsafe request (POST/PUT/PATCH/DELETE) is blocked
unless the user is authenticated AND has ``inventory.can_edit_inventory``,
except:

* the auth views themselves (see ``AUTH_PATH_NAMES`` — signing in is a POST, so
  they MUST be allowed through or nobody could ever log in),
* the Django admin, which has its own auth,
* ``WRITE_GATE_EXEMPT``, a small allowlist of low-stakes writes that any
  signed-in user may perform without the edit permission.

Blocked browser requests are redirected to the login page (carrying ``?next=``);
blocked AJAX/JSON requests get a 403 so client code can react.

This replaced a shared unlock code held in ``request.session["can_write"]``. The
session used to be a signed cookie, which meant anyone who could read
SECRET_KEY off the network share could forge that flag — see the SECRET_KEY note
in ``config/settings.py``.
"""

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

# The permission that means "may change data".
EDIT_PERM = "inventory.can_edit_inventory"

# URL names any SIGNED-IN user can POST to without the edit permission — the
# Intune page's "Fix" picker is a lightweight who's-on-it tag, not a data
# correction. (Pre-login these needed no unlock code at all; the equivalent now
# is "any authenticated user".)
#
# ``warehouse_switch`` is here because it changes nothing in the database — it
# only writes which site to display into the caller's own session, and the view
# itself refuses anyone who isn't staff. Requiring the EDIT permission to look
# at the other warehouse would lock out a read-only admin for no gain.
WRITE_GATE_EXEMPT = {
    "intune_fix_save", "intune_fix_person_add", "intune_fix_mark", "warehouse_switch",
}

# Auth views must accept POST while still anonymous, or login is impossible.
AUTH_PATH_NAMES = ("login", "logout", "password_change", "password_change_done")


class WriteAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in SAFE_METHODS and not self._allowed(request):
            return self._block(request)
        return self.get_response(request)

    def _auth_paths(self):
        """Resolved paths of the auth views, skipping any not wired up."""
        paths = set()
        for name in AUTH_PATH_NAMES:
            try:
                paths.add(reverse(name))
            except Exception:
                pass
        return paths

    def _allowed(self, request):
        path = request.path
        # Signing in / out / changing a password happens by POST, and the first
        # of those is necessarily anonymous. Never gate these.
        if path in self._auth_paths() or path.startswith("/admin/"):
            return True

        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False

        if user.has_perm(EDIT_PERM):
            return True
        # Authenticated but without edit rights: still allow the low-stakes ones.
        return path in (reverse(name) for name in WRITE_GATE_EXEMPT)

    def _block(self, request):
        wants_json = (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
            or "application/json" in request.content_type
        )
        anon = not getattr(getattr(request, "user", None), "is_authenticated", False)
        if wants_json:
            return JsonResponse(
                {
                    "error": "locked",
                    "detail": (
                        "Sign in to make changes."
                        if anon
                        else "Your account doesn't have edit rights."
                    ),
                },
                status=403,
            )
        if anon:
            # Send them to the login page and back again afterwards.
            # redirect_to_login resolves settings.LOGIN_URL itself.
            return redirect_to_login(request.get_full_path())
        messages.error(request, "Your account doesn't have edit rights.")
        return redirect(request.META.get("HTTP_REFERER") or "/")


# URL names that should never be logged as a "page view".
_ACCESS_LOG_SKIP = {"access_logs", "login", "logout", "password_change"}


def client_ip(request):
    """Best-effort source IP: first hop of X-Forwarded-For, else REMOTE_ADDR."""
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or ""


class AccessLogMiddleware:
    """Record each plain HTML page view (source IP, user, page, path, time).

    Logs only GET requests that resolved to a named view and returned a 200
    HTML response — so AJAX/JSON endpoints, POST save/export actions, static
    files and /admin/ are all naturally excluded. The write is wrapped in a
    broad try/except so a locked SQLite file on the network share can never
    break page rendering.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if self._should_log(request, response):
                from .models import AccessLog

                # Runs after AuthenticationMiddleware, so request.user is set;
                # None for anonymous readers, since reads don't require login.
                user = getattr(request, "user", None)
                AccessLog.objects.create(
                    ip=client_ip(request),
                    path=request.path[:300],
                    page=(request.resolver_match.url_name or "")[:100],
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
                    user=user if (user and user.is_authenticated) else None,
                )
        except Exception:
            pass
        return response

    def _should_log(self, request, response):
        if request.method != "GET" or response.status_code != 200:
            return False
        match = getattr(request, "resolver_match", None)
        if not match or match.url_name in _ACCESS_LOG_SKIP:
            return False
        return response.get("Content-Type", "").startswith("text/html")
