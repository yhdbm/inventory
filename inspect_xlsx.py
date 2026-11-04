"""Inspect an xlsx: list tabs, their headers, and real (non-empty) row counts."""
import sys

from openpyxl import load_workbook

path = sys.argv[1] if len(sys.argv) > 1 else "media/uploads/asset_export.xlsx"
wb = load_workbook(path, data_only=True)  # not read_only -> reliable dims
print(f"File: {path}")
print(f"Sheets ({len(wb.sheetnames)}): {wb.sheetnames}\n")
for ws in wb.worksheets:
    rows = list(ws.iter_rows(values_only=True))
    headers = list(rows[0]) if rows else []
    data_rows = [r for r in rows[1:] if any(c is not None and str(c).strip() for c in r)]
    print(f"=== Tab {ws.title!r}: {len(headers)} cols, {len(data_rows)} data rows")
    print(f"    headers: {headers}")
    for r in data_rows[:5]:
        print(f"    row: {r}")
    print()
wb.close()
