"""Seed sample RF-mobile audit checks for a given month, to preview the
month dropdown / donuts.

Usage:
    python manage.py seed_rf_checks                 # ~60% checked for last month
    python manage.py seed_rf_checks --period 2026-05 --pct 60
    python manage.py seed_rf_checks --period 2026-05 --warehouse WH2
    python manage.py seed_rf_checks --period 2026-05 --clear   # remove that month
"""

from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from inventory import warehouse as warehouse_mod
from inventory.models import RfMobileCheck, Warehouse


def _rf_qs(wh):
    return warehouse_mod.scoped_assets(wh).filter(
        Q(model__icontains="MC930B")
    ).exclude(state__iexact="Retired")


class Command(BaseCommand):
    help = "Seed sample RF-mobile checks for a month."

    def add_arguments(self, parser):
        parser.add_argument("--period", default="2026-05", help="Month as YYYY-MM.")
        parser.add_argument("--pct", type=int, default=60, help="Percent to mark checked.")
        parser.add_argument("--warehouse", help='Site code to seed (default: the default site).')
        parser.add_argument("--clear", action="store_true", help="Delete that month's checks and exit.")

    def handle(self, *args, **opts):
        period = opts["period"]
        if opts["clear"]:
            n, _ = RfMobileCheck.objects.filter(period=period).delete()
            self.stdout.write(self.style.SUCCESS(f"Removed {n} checks for {period}."))
            return

        code = opts.get("warehouse")
        if code:
            wh = Warehouse.objects.filter(code__iexact=code.strip()).first()
            if wh is None:
                raise CommandError(f"No warehouse with code '{code}'.")
        else:
            wh = warehouse_mod.default_warehouse()

        assets = list(_rf_qs(wh))
        if not assets:
            site = wh.code if wh else "any site"
            self.stdout.write(self.style.WARNING(f"No RF mobiles (MC930B at {site}) found."))
            return

        # A mid-month timestamp within the period.
        y, m = int(period[:4]), int(period[5:])
        when = timezone.make_aware(datetime(y, m, 15, 10, 30))

        cutoff = max(1, len(assets) * opts["pct"] // 100)
        made = 0
        for a in assets[:cutoff]:
            RfMobileCheck.objects.update_or_create(
                asset=a, period=period, defaults={"checked_at": when}
            )
            made += 1
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {made}/{len(assets)} RF checks for {period}."
        ))
