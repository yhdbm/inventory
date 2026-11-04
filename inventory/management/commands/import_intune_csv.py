"""Import a Microsoft Intune "All devices" CSV export into the IntuneDevice table.

Usage:
    python manage.py import_intune_csv media/uploads/Intune.csv
    python manage.py import_intune_csv <file> --truncate
"""

import csv
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import INTUNE_HEADER_MAP, IntuneDevice

BATCH_SIZE = 1000


def _clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class Command(BaseCommand):
    help = 'Import a Microsoft Intune "All devices" CSV export into the IntuneDevice table.'

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the Intune .csv export")
        parser.add_argument(
            "--truncate",
            action="store_true",
            help="Delete all existing IntuneDevice rows before importing.",
        )

    def handle(self, *args, **options):
        path = options["path"]
        try:
            f = open(path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Could not open {path}: {exc}")

        with f:
            reader = csv.reader(f)
            try:
                header_row = next(reader)
            except StopIteration:
                raise CommandError("CSV file is empty.")

            index_to_field = {}
            unknown = []
            for i, header in enumerate(header_row):
                key = (header or "").strip()
                field = INTUNE_HEADER_MAP.get(key)
                if field:
                    index_to_field[i] = field
                elif key:
                    unknown.append(key)
            if unknown:
                self.stderr.write(f"Ignoring unmapped columns: {unknown}")
            if not index_to_field:
                raise CommandError("No recognized Intune columns found in the header row.")

            if options["truncate"]:
                deleted = IntuneDevice.objects.count()
                IntuneDevice.objects.all().delete()
                self.stdout.write(f"Truncated {deleted} existing rows.")

            filename = os.path.basename(path)
            batch = []
            count = 0
            with transaction.atomic():
                for row in reader:
                    if not any(c.strip() for c in row):
                        continue  # blank row
                    kwargs = {"source_filename": filename}
                    for i, field in index_to_field.items():
                        value = row[i] if i < len(row) else None
                        kwargs[field] = _clean_text(value)
                    batch.append(IntuneDevice(**kwargs))
                    if len(batch) >= BATCH_SIZE:
                        IntuneDevice.objects.bulk_create(batch)
                        count += len(batch)
                        batch = []
                if batch:
                    IntuneDevice.objects.bulk_create(batch)
                    count += len(batch)

        self.stdout.write(self.style.SUCCESS(f"Imported {count} Intune devices."))
