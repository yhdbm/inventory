from django.test import TestCase
from django.urls import reverse

import json

from . import services
from .models import (
    Asset,
    Correction,
    ExpectedItem,
    ReconciliationSession,
    SavedFilter,
    ScannedItem,
    normalize_tag,
)


class NormalizeTagTests(TestCase):
    def test_trim_and_upper(self):
        self.assertEqual(normalize_tag("  abc123 "), "ABC123")
        self.assertEqual(normalize_tag(None), "")
        self.assertEqual(normalize_tag(42), "42")


class CompareTests(TestCase):
    def setUp(self):
        self.session = ReconciliationSession.objects.create(
            name="Test",
            columns=["Asset Tag", "Location", "Status"],
            asset_tag_column="Asset Tag",
        )

    def _expected(self, tag, **data):
        ExpectedItem.objects.create(
            session=self.session, asset_tag=normalize_tag(tag),
            data={"Asset Tag": tag, **data},
        )

    def _scanned(self, tag, **data):
        ScannedItem.objects.create(
            session=self.session, asset_tag=normalize_tag(tag),
            data={"Asset Tag": tag, **data},
        )

    def test_match(self):
        self._expected("A1", Location="Room 1", Status="Active")
        self._scanned("a1", Location="Room 1", Status="Active")
        report = services.compare(self.session)
        self.assertEqual(report["counts"][services.MATCH], 1)
        self.assertEqual(report["counts"][services.MISMATCH], 0)

    def test_field_mismatch(self):
        self._expected("A1", Location="Room 1", Status="Active")
        self._scanned("A1", Location="Room 2", Status="Active")
        report = services.compare(self.session)
        self.assertEqual(report["counts"][services.MISMATCH], 1)
        row = report["rows"][0]
        diffs = [d for d in row.field_diffs if d.differs]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0].column, "Location")

    def test_missing_and_unexpected(self):
        self._expected("IN_SHEET")
        self._scanned("ON_FLOOR")
        report = services.compare(self.session)
        self.assertEqual(report["counts"][services.MISSING], 1)
        self.assertEqual(report["counts"][services.UNEXPECTED], 1)


class AssetFilterTests(TestCase):
    def setUp(self):
        rows = [
            ("A1", "Phone", "In use", "Cedarport"),
            ("A2", "Phone", "Retired", "Cedarport"),
            ("A3", "Monitor", "In use", "Miami"),
            ("A4", "Laptop", "In stock", "Miami"),
            ("A5", "Monitor", "Retired", "Orlando"),
        ]
        for tag, cat, state, loc in rows:
            Asset.objects.create(
                asset_tag=tag, model_category=cat, state=state, location=loc
            )
        self.url = reverse("asset_list")

    def _count(self, response):
        return response.context["total"]

    def test_multiselect_state_is_or(self):
        r = self.client.get(self.url, {"state": ["In use", "Retired"]})
        self.assertEqual(self._count(r), 4)  # A1,A2,A3,A5

    def test_filters_combine_as_and(self):
        r = self.client.get(self.url, {"state": ["Retired"], "category": ["Monitor"]})
        self.assertEqual(self._count(r), 1)  # only A5

    def test_multiple_filters_multi_values(self):
        r = self.client.get(
            self.url, {"category": ["Phone", "Monitor"], "location": ["Cedarport", "Miami"]}
        )
        # Phone/Monitor AND Cedarport/Miami -> A1, A2, A3
        self.assertEqual(self._count(r), 3)

    def test_selections_persisted(self):
        self.client.get(self.url, {"state": ["In use", "Retired"]})
        saved = set(
            SavedFilter.objects.filter(field="state").values_list("value", flat=True)
        )
        self.assertEqual(saved, {"In use", "Retired"})

    def test_use_count_increments(self):
        self.client.get(self.url, {"category": ["Phone"]})
        self.client.get(self.url, {"category": ["Phone"]})
        self.assertEqual(
            SavedFilter.objects.get(field="category", value="Phone").use_count, 2
        )

    def test_saved_selection_pinned_to_top(self):
        # Select once so it gets saved, then load again.
        self.client.get(self.url, {"state": ["Retired"]})
        r = self.client.get(self.url)
        state_filter = next(f for f in r.context["filters"] if f["key"] == "state")
        self.assertIn("Retired", state_filter["pinned"])
        self.assertNotIn("Retired", state_filter["rest"])
        # Pinned values still appear in the overall available count.
        self.assertEqual(
            state_filter["available"],
            len(state_filter["pinned"]) + len(state_filter["rest"]),
        )

    def test_pinned_ordered_by_recency(self):
        self.client.get(self.url, {"state": ["In use"]})
        self.client.get(self.url, {"state": ["Retired"]})  # more recent
        r = self.client.get(self.url)
        state_filter = next(f for f in r.context["filters"] if f["key"] == "state")
        self.assertEqual(state_filter["pinned"][0], "Retired")


class AuditWorkstationTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(
            asset_tag="AST-100", serial_number="SN1", display_name="Dell Laptop",
            state="In stock", location="Cedarport", model_category="Computer",
        )

    def test_lookup_returns_current_values(self):
        r = self.client.get(reverse("audit_lookup"), {"tag": "AST-100"})
        data = r.json()
        self.assertTrue(data["found"])
        self.assertEqual(data["asset_id"], self.asset.pk)
        states = {f["field"]: f["current"] for f in data["fields"]}
        self.assertEqual(states["state"], "In stock")
        self.assertEqual(states["location"], "Cedarport")

    def test_lookup_not_found(self):
        r = self.client.get(reverse("audit_lookup"), {"tag": "NOPE"})
        self.assertFalse(r.json()["found"])

    def test_save_records_only_differences(self):
        payload = {
            "asset_id": self.asset.pk,
            "expected": {
                "state": "In use",        # differs from "In stock"
                "location": "Cedarport",      # same -> not a change
                "department": "IT",       # current blank -> change
            },
        }
        r = self.client.post(
            reverse("audit_save"), data=json.dumps(payload),
            content_type="application/json",
        )
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["change_count"], 2)
        c = Correction.objects.get(asset=self.asset)
        self.assertIn("state", c.changes)
        self.assertIn("department", c.changes)
        self.assertNotIn("location", c.changes)
        self.assertEqual(c.changes["state"]["current"], "In stock")
        self.assertEqual(c.changes["state"]["expected"], "In use")

    def test_saved_expected_reloads_on_lookup(self):
        self.client.post(
            reverse("audit_save"),
            data=json.dumps({"asset_id": self.asset.pk, "expected": {"state": "In use"}}),
            content_type="application/json",
        )
        r = self.client.get(reverse("audit_lookup"), {"tag": "AST-100"})
        data = r.json()
        self.assertTrue(data["has_saved"])
        expected = {f["field"]: f["expected"] for f in data["fields"]}
        self.assertEqual(expected["state"], "In use")

    def test_save_is_upsert(self):
        for st in ("In use", "Retired"):
            self.client.post(
                reverse("audit_save"),
                data=json.dumps({"asset_id": self.asset.pk, "expected": {"state": st}}),
                content_type="application/json",
            )
        self.assertEqual(Correction.objects.filter(asset=self.asset).count(), 1)
        self.assertEqual(Correction.objects.get(asset=self.asset).expected["state"], "Retired")

    def test_clear_deletes_correction(self):
        self.client.post(
            reverse("audit_save"),
            data=json.dumps({"asset_id": self.asset.pk, "expected": {"state": "In use"}}),
            content_type="application/json",
        )
        self.client.post(
            reverse("audit_clear"),
            data=json.dumps({"asset_id": self.asset.pk}),
            content_type="application/json",
        )
        self.assertFalse(Correction.objects.filter(asset=self.asset).exists())

    def test_export_json_and_csv(self):
        self.client.post(
            reverse("audit_save"),
            data=json.dumps({"asset_id": self.asset.pk, "expected": {"state": "In use"}}),
            content_type="application/json",
        )
        rj = self.client.get(reverse("corrections_export"), {"format": "json"})
        body = rj.json()
        self.assertEqual(len(body["corrections"]), 1)
        self.assertEqual(body["corrections"][0]["asset_tag"], "AST-100")

        rc = self.client.get(reverse("corrections_export"), {"format": "csv"})
        self.assertEqual(rc["Content-Type"], "text/csv")
        self.assertIn(b"AST-100", rc.content)
        self.assertIn(b"In use", rc.content)
