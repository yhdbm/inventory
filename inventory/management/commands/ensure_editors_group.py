"""Create the "Editors" group holding the inventory edit permission.

Membership of this group is what replaced the old shared unlock code: a user in
it may save corrections, upload sources, save snapshots, etc. Superusers bypass
the check automatically (Django grants them every permission).

    python manage.py ensure_editors_group
    python manage.py ensure_editors_group --add demo

Idempotent — safe to re-run.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

GROUP_NAME = "Editors"
PERM_CODENAME = "can_edit_inventory"


class Command(BaseCommand):
    help = 'Create the "Editors" group with the inventory edit permission.'

    def add_arguments(self, parser):
        parser.add_argument(
            "--add",
            metavar="USERNAME",
            help="Also add this existing user to the group.",
        )

    def handle(self, *args, **options):
        try:
            perm = Permission.objects.get(codename=PERM_CODENAME)
        except Permission.DoesNotExist:
            raise CommandError(
                f"Permission '{PERM_CODENAME}' doesn't exist yet — run "
                "'manage.py migrate' first."
            )

        group, created = Group.objects.get_or_create(name=GROUP_NAME)
        self.stdout.write(
            self.style.SUCCESS(f"Created group '{GROUP_NAME}'.")
            if created
            else f"Group '{GROUP_NAME}' already exists."
        )

        if not group.permissions.filter(pk=perm.pk).exists():
            group.permissions.add(perm)
            self.stdout.write(self.style.SUCCESS(f"Granted '{PERM_CODENAME}'."))
        else:
            self.stdout.write(f"Group already holds '{PERM_CODENAME}'.")

        username = options.get("add")
        if username:
            User = get_user_model()
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(
                    f"No user named '{username}'. Create one first with "
                    "'manage.py createsuperuser'."
                )
            user.groups.add(group)
            self.stdout.write(
                self.style.SUCCESS(f"Added '{username}' to '{GROUP_NAME}'.")
            )

        self.stdout.write(
            f"\nMembers of '{GROUP_NAME}': "
            + (", ".join(group.user_set.values_list("username", flat=True)) or "(none yet)")
        )
