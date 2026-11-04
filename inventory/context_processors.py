"""Template context shared by every page.

``can_write`` used to be ``request.session["can_write"]`` — a flag set by
entering a shared unlock code. It's now derived from the signed-in user's
permission, but the template-facing NAME is deliberately unchanged so the ~40
existing ``{% if can_write %}`` conditions across the templates keep their
meaning: "may this person change data?"
"""

from . import warehouse as warehouse_mod

EDIT_PERM = "inventory.can_edit_inventory"


def write_access(request):
    user = getattr(request, "user", None)
    return {
        "can_write": bool(user and user.is_authenticated and user.has_perm(EDIT_PERM)),
    }


def warehouse(request):
    """The site every page is scoped to, plus the staff-only picker's options.

    ``warehouse`` drives the navbar brand and each page's heading, so the person
    looking at the screen can always tell WHICH site's numbers these are — the
    single most important thing to get right once the app stopped being
    single-site. ``warehouse_options`` is only populated for staff (the picker
    is staff-only), which keeps the extra query off every anonymous page view.
    """
    user = getattr(request, "user", None)
    is_staff = bool(user and user.is_authenticated and user.is_staff)
    return {
        "warehouse": warehouse_mod.current_warehouse(request),
        "warehouse_options": warehouse_mod.all_warehouses() if is_staff else [],
    }
