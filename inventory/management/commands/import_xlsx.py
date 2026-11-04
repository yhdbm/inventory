"""Import a ServiceNow alm_asset xlsx export into the Asset table.

Each spreadsheet header maps to a real DB column. All worksheets (tabs) are
read and unioned, since the export paginates rows across tabs that share the
same headers.

Usage:
    python manage.py import_xlsx media/uploads/asset_export.xlsx
    python manage.py import_xlsx <file> --truncate
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from openpyxl import load_workbook

from inventory.models import (
    ASSET_DATETIME_FIELDS,
    ASSET_FLOAT_FIELDS,
    ASSET_HEADER_MAP,
    Asset,
)

BATCH_SIZE = 1000


def _clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_datetime(value):
    if value is None or value == "":
        return None
    # openpyxl returns datetime objects for date cells (data_only=True).
    if hasattr(value, "year"):
        if timezone.is_naive(value):
            return timezone.make_aware(value, timezone.get_default_timezone())
        return value
    # Fallback: leave a string-ish date as-is is not valid for a DateTimeField,
    # so return None rather than crash.
    return None


def _clean_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _convert(field_name, value):
    if field_name in ASSET_DATETIME_FIELDS:
        return _clean_datetime(value)
    if field_name in ASSET_FLOAT_FIELDS:
        return _clean_float(value)
    return _clean_text(value)


class Command(BaseCommand):
    help = "Import a ServiceNow alm_asset xlsx export into the Asset table."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the .xlsx file")
        parser.add_argument(
            "--truncate",
            action="store_true",
            help="Delete all existing Asset rows before importing.",
        )

    def handle(self, *args, **options):
        path = options["path"]
        try:
            # NOT read_only: some exporters (e.g. ServiceNow) write incorrect
            # worksheet dimensions, and read_only mode then streams only the
            # header row. Loading normally reads every row reliably.
            wb = load_workbook(path, data_only=True)
        except Exception as exc:
            raise CommandError(f"Could not open {path}: {exc}")

        if options["truncate"]:
            deleted = Asset.objects.count()
            # Delete in chunks: a single all().delete() cascades to the related
            # tables with one huge "WHERE asset_id IN (...)" that exceeds SQLite's
            # 32766-variable limit once there are tens of thousands of assets.
            while True:
                ids = list(Asset.objects.values_list("pk", flat=True)[:2000])
                if not ids:
                    break
                Asset.objects.filter(pk__in=ids).delete()
            self.stdout.write(f"Truncated {deleted} existing rows.")

        total = 0
        self._skipped_no_tag = 0
        with transaction.atomic():
            for ws in wb.worksheets:
                tab_total = self._import_sheet(ws)
                total += tab_total
                self.stdout.write(
                    f"  Tab {ws.title!r}: imported {tab_total} rows."
                )
        wb.close()
        if self._skipped_no_tag:
            self.stdout.write(
                f"Skipped {self._skipped_no_tag} row(s) with no asset tag."
            )
        self.stdout.write(self.style.SUCCESS(f"Imported {total} assets total."))

    def _import_sheet(self, ws):
        rows = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            return 0  # empty sheet

        # Map each column index -> model field name (skip unknown headers).
        index_to_field = {}
        unknown = []
        for i, header in enumerate(header_row):
            key = ("" if header is None else str(header).strip())
            field = ASSET_HEADER_MAP.get(key)
            if field:
                index_to_field[i] = field
            elif key:
                unknown.append(key)
        if unknown:
            self.stderr.write(
                f"  Tab {ws.title!r}: ignoring unmapped columns: {unknown}"
            )
        if not index_to_field:
            self.stderr.write(f"  Tab {ws.title!r}: no recognized columns, skipped.")
            return 0

        # Only enforce the "must have an asset tag" rule when this sheet actually
        # has an Asset tag column (otherwise we'd wrongly drop every row).
        has_tag_col = "asset_tag" in index_to_field.values()

        batch = []
        count = 0
        for row in rows:
            if not any(c is not None and str(c).strip() for c in row):
                continue  # blank row
            kwargs = {"source_tab": ws.title}
            for i, field in index_to_field.items():
                value = row[i] if i < len(row) else None
                kwargs[field] = _convert(field, value)
            # Skip devices with no asset tag (_clean_text turns blanks into None).
            if has_tag_col and not kwargs.get("asset_tag"):
                self._skipped_no_tag += 1
                continue
            batch.append(Asset(**kwargs))
            if len(batch) >= BATCH_SIZE:
                Asset.objects.bulk_create(batch)
                count += len(batch)
                batch = []
        if batch:
            Asset.objects.bulk_create(batch)
            count += len(batch)
        return count
