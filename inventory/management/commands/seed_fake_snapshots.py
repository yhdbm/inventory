"""Insert sample ReportSnapshot rows to preview the % inventoried-over-time chart.

Dates are backdated across recent weeks with a rising inventoried %, so the
Snapshots chart shows a realistic trend.

Usage:
    python manage.py seed_fake_snapshots          # add the sample series
    python manage.py seed_fake_snapshots --clear  # delete all snapshots first
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from inventory.models import ReportSnapshot


class Command(BaseCommand):
    help = "Insert sample ReportSnapshot rows to preview the time chart."

    SAMPLE_FILE = "sample_data.xlsx"

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true",
                            help="Remove only the sample snapshots (and don't re-insert).")

    def handle(self, *args, **options):
        if options["clear"]:
            n, _ = ReportSnapshot.objects.filter(source_filename=self.SAMPLE_FILE).delete()
            self.stdout.write(self.style.SUCCESS(f"Removed {n} sample snapshot(s)."))
            return

        total = 2850
        series = [(35, 11), (28, 23), (21, 38), (14, 52), (7, 67), (1, 78)]  # (days_ago, pct)
        now = timezone.now()
        for days, pct in series:
            inv = round(total * pct / 100)
            cats = [
                {"category": "Computer", "inventoried": round(inv * 0.45), "total": round(total * 0.40)},
                {"category": "Monitor", "inventoried": round(inv * 0.30), "total": round(total * 0.32)},
                {"category": "Printer", "inventoried": round(inv * 0.15), "total": round(total * 0.16)},
                {"category": "Phone", "inventoried": round(inv * 0.10), "total": round(total * 0.12)},
            ]
            stations = [
                {"station": "WH1-01", "inventoried": round(inv * 0.5), "total": round(total * 0.5)},
                {"station": "WH1-02", "inventoried": round(inv * 0.3), "total": round(total * 0.3)},
                {"station": "(no Station ID)", "inventoried": round(inv * 0.2), "total": round(total * 0.2)},
            ]
            states = [
                {"state": "In use", "inventoried": round(inv * 0.7), "total": round(total * 0.65)},
                {"state": "In stock", "inventoried": round(inv * 0.3), "total": round(total * 0.35)},
            ]
            snap = ReportSnapshot.objects.create(
                source_filename="sample_data.xlsx",
                total_assets=total, inventoried=inv, pending=total - inv, marked_pct=pct,
                categories=cats, stations=stations, states=states,
            )
            # taken_at is auto_now_add; .update() bypasses it to backdate the row.
            ReportSnapshot.objects.filter(pk=snap.pk).update(taken_at=now - timedelta(days=days))

        self.stdout.write(self.style.SUCCESS(f"Inserted {len(series)} sample snapshots."))
