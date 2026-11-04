"""Which warehouse (site) a request is scoped to, and the asset filter that implies.

The app was single-site (Cedarport Main / Annex, Station IDs tagged ``WH1|…``) and
expressed that as a hard-coded Location filter repeated in half a dozen views.
Those filters now all go through :func:`scoped_assets`, and the site comes from
the signed-in user: the WH2 user's profile says ``WH2``, everyone else's says
``WH1``.

``current_warehouse(request)`` resolves in this order:

1. a **staff** member's session override — the navbar warehouse picker, so one
   admin can look at either site without signing out,
2. the signed-in user's ``UserProfile.warehouse``,
3. the ``Warehouse`` row flagged ``is_default``,
4. the lowest-``position`` warehouse.

Steps 3-4 matter more than they look: reads are deliberately open to anonymous
visitors on the LAN (see ``middleware.WriteAccessMiddleware``), so an anonymous
reader has no profile at all and MUST still land somewhere sensible rather than
on an error. For the same reason every step degrades instead of raising — a
database with no Warehouse rows yet returns ``None``, and callers treat that as
"no scoping" rather than crashing every page.
"""

from .models import Asset, Warehouse

# Session key holding a staff member's picker choice.
SESSION_KEY = "warehouse_code"

# Cached on the request so the several context processors / views that ask for
# it during one render don't each re-query.
_REQUEST_ATTR = "_inventory_warehouse"


def all_warehouses():
    return list(Warehouse.objects.all())


def default_warehouse():
    """The site anonymous readers (and profile-less users) see."""
    return (
        Warehouse.objects.filter(is_default=True).first()
        or Warehouse.objects.first()
    )


def by_code(code):
    if not code:
        return None
    return Warehouse.objects.filter(code__iexact=code.strip()).first()


def current_warehouse(request):
    """The warehouse this request is scoped to (see module docstring)."""
    cached = getattr(request, _REQUEST_ATTR, None)
    if cached is not None:
        return cached

    wh = None
    user = getattr(request, "user", None)
    signed_in = bool(user and user.is_authenticated)

    # 1. Staff-only session override from the navbar picker. Checked first so an
    #    admin's choice beats their own profile, and ignored for everyone else so
    #    a stale session key can never widen a non-staff user's scope.
    if signed_in and user.is_staff:
        wh = by_code((getattr(request, "session", None) or {}).get(SESSION_KEY))

    # 2. The user's own assignment.
    if wh is None and signed_in:
        profile = getattr(user, "profile", None)
        wh = profile.warehouse if (profile and profile.warehouse_id) else None

    # 3./4. Fall back to the default site.
    if wh is None:
        wh = default_warehouse()

    setattr(request, _REQUEST_ATTR, wh)
    return wh


def scoped_assets(warehouse):
    """Every Asset belonging to ``warehouse``.

    A ``None`` warehouse (no rows configured yet) means "don't scope" rather
    than "nothing", so the app still renders on a fresh database.
    """
    if warehouse is None:
        return Asset.objects.all()
    return Asset.objects.filter(warehouse.asset_q())


def owns(warehouse, asset):
    """Whether ``asset`` belongs to ``warehouse``. True when unscoped."""
    if warehouse is None:
        return True
    return warehouse.owns(asset)
