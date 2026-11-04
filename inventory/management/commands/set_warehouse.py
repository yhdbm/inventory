"""Assign a user to a warehouse (and optionally create the account).

    python manage.py set_warehouse editor WH2
    python manage.py set_warehouse editor WH2 --create --editor
    python manage.py set_warehouse --list

``--create`` makes the account with a random temporary password, printed once —
there is no email backend configured, so a reset link can't be sent and the
password has to be handed over out of band. ``--editor`` also puts them in the
"Editors" group, which is what grants the edit permission (see
``ensure_editors_group``); without it they get read-only access.

Idempotent — re-running just re-points the existing profile.
"""

import secrets
import string

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from inventory.models import UserProfile, Warehouse

EDITORS_GROUP = "Editors"
# Password alphabet without look-alike characters (O/0, l/1, I) — this gets read
# off a screen and typed by hand.
_ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
)


class Command(BaseCommand):
    help = "Assign a user to a warehouse, optionally creating the account."

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", help="The account to assign.")
        parser.add_argument("code", nargs="?", help='Warehouse code, e.g. "WH1" or "WH2".')
        parser.add_argument("--create", action="store_true",
                            help="Create the user if they don't exist (random temp password).")
        parser.add_argument("--editor", action="store_true",
                            help='Also add them to the "Editors" group (grants edit rights).')
        parser.add_argument("--staff", action="store_true",
                            help="Also mark them staff (access log, extension page, "
                                 "/admin/, and the warehouse picker).")
        parser.add_argument("--list", action="store_true",
                            help="List the warehouses and every user's assignment, then exit.")

    def handle(self, *args, **options):
        if options["list"]:
            return self._list()

        username, code = options["username"], options["code"]
        if not username or not code:
            raise CommandError(
                "Give a username and a warehouse code, e.g.\n"
                "    manage.py set_warehouse editor WH2\n"
                "or use --list to see what exists."
            )

        wh = Warehouse.objects.filter(code__iexact=code.strip()).first()
        if wh is None:
            known = ", ".join(Warehouse.objects.values_list("code", flat=True)) or "(none)"
            raise CommandError(f"No warehouse with code '{code}'. Known codes: {known}")

        User = get_user_model()
        user = User.objects.filter(username=username).first()
        temp_password = None
        if user is None:
            if not options["create"]:
                raise CommandError(
                    f"No user named '{username}'. Re-run with --create to make the "
                    "account, or create it in /admin/ first."
                )
            temp_password = "".join(secrets.choice(_ALPHABET) for _ in range(14))
            user = User.objects.create_user(username=username, password=temp_password)
            self.stdout.write(self.style.SUCCESS(f"Created user '{username}'."))

        if options["staff"] and not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
            self.stdout.write(self.style.SUCCESS(f"Marked '{username}' as staff."))

        if options["editor"]:
            group = Group.objects.filter(name=EDITORS_GROUP).first()
            if group is None:
                raise CommandError(
                    f"The '{EDITORS_GROUP}' group doesn't exist yet — run "
                    "'manage.py ensure_editors_group' first."
                )
            user.groups.add(group)
            self.stdout.write(self.style.SUCCESS(f"Added '{username}' to '{EDITORS_GROUP}'."))

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.warehouse = wh
        profile.save()
        self.stdout.write(self.style.SUCCESS(f"'{username}' is now scoped to {wh}."))

        if temp_password:
            self.stdout.write(
                "\n" + self.style.WARNING("TEMPORARY PASSWORD (shown once): ") + temp_password
            )
            self.stdout.write(
                f"Hand it over directly and have them change it at /password-change/ — "
                "there is no email backend, so there is no reset link."
            )

    def _list(self):
        self.stdout.write("Warehouses:")
        for wh in Warehouse.objects.all():
            flags = []
            if wh.is_default:
                flags.append("DEFAULT")
            if not wh.snow_sys_ids:
                flags.append("no SNOW location ids")
            suffix = f"   [{', '.join(flags)}]" if flags else ""
            prefixes = ", ".join(f"{p}|" for p in wh.prefixes)
            self.stdout.write(
                f"  {wh.code:<8} {wh.name:<24} station {prefixes:<14} "
                f"location /{wh.location_regex or '-'}/{suffix}"
            )

        self.stdout.write("\nUsers:")
        User = get_user_model()
        for user in User.objects.order_by("username"):
            profile = getattr(user, "profile", None)
            wh = profile.warehouse.code if (profile and profile.warehouse_id) else "(default)"
            tags = []
            if user.is_superuser:
                tags.append("superuser")
            elif user.is_staff:
                tags.append("staff")
            if user.has_perm("inventory.can_edit_inventory"):
                tags.append("can edit")
            self.stdout.write(
                f"  {user.get_username():<16} → {wh:<10} {', '.join(tags)}"
            )
