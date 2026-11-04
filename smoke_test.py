"""End-to-end smoke test of the full HTTP flow via Django's test client.

Run with:  python smoke_test.py
Creates a temp session in the real DB, exercises upload -> map -> scan ->
report -> export, prints the outcome, then cleans up.
"""
import io
import os

import django
from openpyxl import Workbook

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.test import Client  # noqa: E402

from inventory.models import ReconciliationSession  # noqa: E402


def build_xlsx_bytes():
    wb = Workbook()
    ws = wb.active
    ws.append(["Asset Tag", "Location", "Status"])
    ws.append(["AST-001", "Warehouse A", "Active"])
    ws.append(["AST-002", "Warehouse A", "Active"])
    ws.append(["AST-003", "Dock 3", "Active"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main():
    c = Client()

    # 1. Upload
    upload = SimpleUploadedFile(
        "inv.xlsx", build_xlsx_bytes(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    r = c.post("/sessions/new/", {
        "name": "SMOKE TEST", "description": "", "expected_file": upload,
    })
    assert r.status_code == 302, f"upload failed: {r.status_code}"
    session = ReconciliationSession.objects.get(name="SMOKE TEST")
    print("columns detected:", session.columns)
    assert session.columns == ["Asset Tag", "Location", "Status"]

    # 2. Map asset-tag column + import
    r = c.post(f"/sessions/{session.pk}/map/", {"asset_tag_column": "Asset Tag"})
    assert r.status_code == 302, f"map failed: {r.status_code}"
    session.refresh_from_db()
    print("expected imported:", session.expected_items.count())
    assert session.expected_items.count() == 3

    # 3. Scan: one exact match, one field mismatch, one not-in-sheet
    c.post(f"/sessions/{session.pk}/", {
        "asset_tag": "AST-001", "scanned_by": "tester",
        "col__Location": "Warehouse A", "col__Status": "Active"})
    c.post(f"/sessions/{session.pk}/", {
        "asset_tag": "AST-002", "scanned_by": "tester",
        "col__Location": "Dock 9", "col__Status": "Active"})  # location differs
    c.post(f"/sessions/{session.pk}/", {
        "asset_tag": "AST-999", "scanned_by": "tester",
        "col__Location": "Dock 3", "col__Status": "Active"})  # not in sheet

    from inventory import services
    report = services.compare(session)
    print("counts:", report["counts"])
    assert report["counts"][services.MATCH] == 1
    assert report["counts"][services.MISMATCH] == 1
    assert report["counts"][services.MISSING] == 1     # AST-003 never scanned
    assert report["counts"][services.UNEXPECTED] == 1  # AST-999

    # 4. Detail page renders
    r = c.get(f"/sessions/{session.pk}/")
    assert r.status_code == 200 and b"Comparison report" in r.content

    # 5. CSV export
    r = c.get(f"/sessions/{session.pk}/export/")
    assert r.status_code == 200 and r["Content-Type"] == "text/csv"
    print("csv first line:", r.content.splitlines()[0].decode())

    # cleanup
    session.delete()
    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
