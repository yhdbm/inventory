"""Data model for inventory reconciliation.

A *session* represents one reconciliation job: you upload the inventory
spreadsheet (the "expected" source of truth) and then record the devices you
physically find by manual entry / scanning ("scanned"). Items are matched on
their asset tag, and any differences are reported.

Because the spreadsheet columns are generic (they vary per inventory), the
per-item attributes are stored in a JSON ``data`` field rather than fixed
columns. The list/order of columns for a session is kept on the session.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


def normalize_tag(value) -> str:
    """Normalize an asset tag for matching (trim + uppercase)."""
    if value is None:
        return ""
    return str(value).strip().upper()


class Warehouse(models.Model):
    """One physical site the app can be scoped to (``WH1`` = Cedarport CPT, ``WH2``).

    An Asset belongs to a warehouse when EITHER
      * its Station ID starts with one of ``station_prefixes`` followed by "|"
        (``WH1|IT``, ``WH2|Production``) — the authoritative tag, or
      * its Location matches ``location_regex`` AND no OTHER warehouse's prefix
        claims it.

    The Location half is not redundant: it keeps assets that nobody has tagged
    yet (blank or free-form Station ID) inside the site that owns them, and
    those are exactly the rows an auditor needs to see. Matching the prefix is
    anchored at the START of the Station ID on purpose — a plain "contains"
    test would pull ``WH1|WH2…`` (and the real ``WH1|WH1|Art Department``
    typo) into the wrong site. There is a genuine ``WH1|HR`` station, which a
    naive contains-test on the old ``HR`` code would also have mis-filed.

    **An explicit tag outranks a location guess**, which is what the "AND no
    other warehouse's prefix" clause buys. Without it the two sites overlap and
    every total double-counts: there is a real asset tagged ``WH1|…`` whose
    Location is ``US_MS_Lakeside Rd``, and it would otherwise appear in WH1 (by
    prefix) and in WH2 (by location) at the same time. Whoever tagged it said
    WH1, so WH1 wins.

    ``snow_location_sys_ids`` holds the ServiceNow ``cmn_location`` sys_ids for
    the site, used to scope every SNOW deep link. Those GUIDs are not derivable
    from anything we store, so a site with none falls back to a Station-ID
    STARTSWITH query — correct, but blind to untagged assets. Paste the sys_ids
    in (admin → Warehouses) to make the links exact.
    """

    code = models.CharField(max_length=20, unique=True,
                            help_text='Short site code, e.g. "WH1" or "WH2".')
    name = models.CharField(max_length=100, blank=True,
                            help_text='Display name shown in page headings, e.g. "Lakeside Rd".')
    station_prefixes = models.CharField(
        max_length=200, blank=True,
        help_text='Comma-separated Station ID prefixes, WITHOUT the trailing "|". '
                  'Defaults to the code itself when left blank.',
    )
    location_regex = models.CharField(
        max_length=200, blank=True,
        help_text=r'Case-insensitive regex matched against Location, e.g. "\bCPT\b|Annex". '
                  'Leave blank to scope by Station ID only.',
    )
    snow_location_sys_ids = models.CharField(
        max_length=400, blank=True,
        help_text="Comma-separated ServiceNow location sys_ids for this site. "
                  "Blank falls back to a Station ID STARTSWITH query in SNOW links.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="What anonymous readers and users with no warehouse set will see. "
                  "Saving this on one warehouse clears it on the others.",
    )
    position = models.IntegerField(default=0, help_text="Order in the warehouse picker.")

    class Meta:
        ordering = ["position", "code"]

    def __str__(self):
        return f"{self.code} — {self.name}" if self.name else self.code

    @property
    def label(self):
        """Heading text: the site name if there is one, else the bare code."""
        return self.name or self.code

    @property
    def prefixes(self):
        """Station ID prefixes as a list, defaulting to the code itself."""
        parts = [p.strip() for p in (self.station_prefixes or "").split(",") if p.strip()]
        return parts or [self.code]

    @property
    def snow_sys_ids(self):
        return [s.strip() for s in (self.snow_location_sys_ids or "").split(",") if s.strip()]

    def other_prefixes(self):
        """Every OTHER warehouse's Station ID prefixes.

        A DB hit, but the table holds one row per site (two today), and it's
        what keeps the two scopes disjoint — see the class docstring.
        """
        return [
            p
            for other in Warehouse.objects.exclude(pk=self.pk)
            for p in other.prefixes
        ]

    def asset_q(self):
        """``Q`` selecting this site's assets.

        Station ID prefix, OR Location when no other site's prefix claims the
        row. The two warehouses' results are disjoint by construction.
        """
        mine = [models.Q(station_id__istartswith=f"{p}|") for p in self.prefixes]
        if not mine and not self.location_regex:   # no rule at all -> owns nothing
            return models.Q(pk__in=[])

        q = None
        for part in mine:
            q = part if q is None else (q | part)

        if self.location_regex:
            by_location = models.Q(location__iregex=self.location_regex)
            for p in self.other_prefixes():
                by_location &= ~models.Q(station_id__istartswith=f"{p}|")
            q = by_location if q is None else (q | by_location)
        return q

    def owns(self, asset):
        """Python-side equivalent of ``asset_q()`` for a single Asset.

        Used where the asset was fetched without the scope filter — the audit
        pages look up any scanned tag across all 63k rows on purpose, then warn
        when the result belongs to another site. Must stay in step with
        ``asset_q()``, including the "explicit tag beats location" rule.
        """
        import re

        sid = (getattr(asset, "station_id", "") or "").strip().lower()
        if any(sid.startswith(f"{p.lower()}|") for p in self.prefixes):
            return True
        if not self.location_regex:
            return False
        if any(sid.startswith(f"{p.lower()}|") for p in self.other_prefixes()):
            return False
        loc = getattr(asset, "location", "") or ""
        try:
            return bool(re.search(self.location_regex, loc, re.IGNORECASE))
        except re.error:
            return False

    def snow_scope_condition(self):
        """The ServiceNow encoded-query condition that scopes a list to this site."""
        ids = self.snow_sys_ids
        if ids:
            return "locationIN" + ",".join(ids)
        # No sys_ids on record — the Station ID prefix is the only scope we can
        # express. Multiple prefixes are OR'd, which per ServiceNow's grouping
        # rules means "(a OR b) AND <the rest>", so it stays safe to append to.
        return "^OR".join(f"u_station_idSTARTSWITH{p}|" for p in self.prefixes)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Exactly one default: setting it here clears every other row.
        if self.is_default:
            Warehouse.objects.exclude(pk=self.pk).filter(is_default=True).update(is_default=False)


class UserProfile(models.Model):
    """Per-user app settings — today just the warehouse their pages are scoped to.

    A user with no profile (or a profile with no warehouse) falls back to the
    default Warehouse, so an account created straight in /admin/ still works.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    warehouse = models.ForeignKey(
        Warehouse, null=True, blank=True, on_delete=models.SET_NULL, related_name="users"
    )

    def __str__(self):
        wh = self.warehouse.code if self.warehouse_id else "(default)"
        return f"{self.user.get_username()} → {wh}"


class ReconciliationSession(models.Model):
    """One inventory comparison job."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # The uploaded spreadsheet and how to interpret it.
    expected_file = models.FileField(upload_to="uploads/", blank=True, null=True)
    # Ordered list of column headers found in the spreadsheet.
    columns = models.JSONField(default=list, blank=True)
    # Which header holds the asset tag (the match key).
    asset_tag_column = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def is_imported(self) -> bool:
        return self.expected_items.exists()

    @property
    def value_columns(self) -> list:
        """Columns other than the asset-tag column (the ones we diff)."""
        return [c for c in self.columns if c != self.asset_tag_column]


class ExpectedItem(models.Model):
    """A row parsed from the uploaded spreadsheet."""

    session = models.ForeignKey(
        ReconciliationSession, on_delete=models.CASCADE, related_name="expected_items"
    )
    asset_tag = models.CharField(max_length=200, db_index=True)
    data = models.JSONField(default=dict)
    row_number = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["row_number"]

    def __str__(self):
        return f"Expected {self.asset_tag}"


class ScannedItem(models.Model):
    """A device recorded by manual entry / scanning."""

    session = models.ForeignKey(
        ReconciliationSession, on_delete=models.CASCADE, related_name="scanned_items"
    )
    asset_tag = models.CharField(max_length=200, db_index=True)
    data = models.JSONField(default=dict)
    scanned_by = models.CharField(max_length=200, blank=True)
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scanned_at"]

    def __str__(self):
        return f"Scanned {self.asset_tag}"


class Asset(models.Model):
    """The imported inventory source (ServiceNow ``alm_asset`` export).

    Each spreadsheet column is a real, typed database column. The ``db_column``
    on every field is the exact spreadsheet header, so the column name in the
    database literally matches the header in the .xlsx. The verbose_name is also
    the original header for nice labels in the admin.

    The export is paginated across multiple tabs (Page 1/2/3) that share the
    same headers; all tabs are unioned into this single table. ``source_tab``
    records which tab a row came from.
    """

    asset_tag = models.TextField("Asset tag", db_column="Asset tag", null=True, blank=True, db_index=True)
    serial_number = models.TextField("Serial number", db_column="Serial number", null=True, blank=True)
    gl_account = models.TextField("GL account", db_column="GL account", null=True, blank=True)
    installed = models.DateTimeField("Installed", db_column="Installed", null=True, blank=True)
    depreciation_effective_date = models.DateTimeField("Depreciation effective date", db_column="Depreciation effective date", null=True, blank=True)
    scheduled_retirement = models.DateTimeField("Scheduled retirement", db_column="Scheduled retirement", null=True, blank=True)
    mac_address = models.TextField("MAC address", db_column="MAC address", null=True, blank=True)
    wired_mac_address = models.TextField("Wired MAC Address", db_column="Wired MAC Address", null=True, blank=True)
    substate = models.TextField("Substate", db_column="Substate", null=True, blank=True)
    disposal_reason = models.TextField("Disposal reason", db_column="Disposal reason", null=True, blank=True)
    station_id = models.TextField("Station ID", db_column="Station ID", null=True, blank=True)
    po_number = models.TextField("PO number", db_column="PO number", null=True, blank=True)
    location = models.TextField("Location", db_column="Location", null=True, blank=True)
    name = models.TextField("Name", db_column="Name", null=True, blank=True)
    stockroom = models.TextField("Stockroom", db_column="Stockroom", null=True, blank=True)
    department = models.TextField("Department", db_column="Department", null=True, blank=True)
    state = models.TextField("State", db_column="State", null=True, blank=True)
    comments = models.TextField("Comments", db_column="Comments", null=True, blank=True)
    model_category = models.TextField("Model category", db_column="Model category", null=True, blank=True)
    display_name = models.TextField("Display name", db_column="Display name", null=True, blank=True)
    manufacturer = models.TextField("Manufacturer", db_column="Manufacturer", null=True, blank=True)
    managed_by = models.TextField("Managed by", db_column="Managed by", null=True, blank=True)
    assigned_to = models.TextField("Assigned to", db_column="Assigned to", null=True, blank=True)
    company = models.TextField("Company", db_column="Company", null=True, blank=True)
    cost = models.FloatField("Cost", db_column="Cost", null=True, blank=True)
    wireless_mac_address = models.TextField("Wireless MAC Address", db_column="Wireless MAC Address", null=True, blank=True)
    os_version = models.TextField("OS Version", db_column="OS Version", null=True, blank=True)
    updated = models.DateTimeField("Updated", db_column="Updated", null=True, blank=True)
    updated_by = models.TextField("Updated by", db_column="Updated by", null=True, blank=True)
    model = models.TextField("Model", db_column="Model", null=True, blank=True)
    work_notes = models.TextField("Work notes", db_column="Work notes", null=True, blank=True)
    last_audit_date = models.DateTimeField("Last audit date", db_column="Last audit date", null=True, blank=True)

    # Provenance.
    source_tab = models.CharField(max_length=200, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.asset_tag or f"Asset #{self.pk}"


class AssetImport(models.Model):
    """Metadata about the currently-loaded asset source xlsx.

    Only the latest import is kept (the previous one is removed on each upload),
    so this effectively records the single active version.
    """

    filename = models.CharField(max_length=255)
    # Creation date embedded in the xlsx file itself (workbook properties).
    file_created = models.DateTimeField(null=True, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self):
        return f"{self.filename} ({self.row_count} rows)"


# Maps spreadsheet header -> model field name. Single source of truth used by
# the importer. Keep in sync with the fields declared on Asset above.
ASSET_HEADER_MAP = {
    "Asset tag": "asset_tag",
    "Serial number": "serial_number",
    "GL account": "gl_account",
    "Installed": "installed",
    "Depreciation effective date": "depreciation_effective_date",
    "Scheduled retirement": "scheduled_retirement",
    "MAC address": "mac_address",
    "Wired MAC Address": "wired_mac_address",
    "Substate": "substate",
    "Disposal reason": "disposal_reason",
    "Station ID": "station_id",
    "PO number": "po_number",
    "Location": "location",
    "Name": "name",
    "Stockroom": "stockroom",
    "Department": "department",
    "State": "state",
    "Comments": "comments",
    "Model category": "model_category",
    "Display name": "display_name",
    "Manufacturer": "manufacturer",
    "Managed by": "managed_by",
    "Assigned to": "assigned_to",
    "Company": "company",
    "Cost": "cost",
    "Wireless MAC Address": "wireless_mac_address",
    "OS Version": "os_version",
    "Updated": "updated",
    "Updated by": "updated_by",
    "Model": "model",
    "Work notes": "work_notes",
    "Last audit date": "last_audit_date",
}

ASSET_DATETIME_FIELDS = {
    "installed",
    "depreciation_effective_date",
    "scheduled_retirement",
    "updated",
    "last_audit_date",
}
ASSET_FLOAT_FIELDS = {"cost"}


# Fields shown read-only at the top of the audit left panel (device identity).
ASSET_IDENTITY_FIELDS = ["asset_tag", "serial_number", "display_name"]

# Fields that are comparable/editable in the audit workstation. The expected
# value for each is entered on the right panel; a mismatch turns the left-panel
# row red and is recorded as a Correction.
ASSET_AUDIT_FIELDS = [
    "state",
    "substate",
    "disposal_reason",
    "location",
    "department",
    "gl_account",
    "assigned_to",
    "managed_by",
    "stockroom",
    "station_id",
    "name",
    "display_name",
    "model_category",
    "manufacturer",
    "model",
    "comments",
]


def audit_field_label(field_name: str) -> str:
    return Asset._meta.get_field(field_name).verbose_name


class Correction(models.Model):
    """A recorded difference between an asset's current values and the expected
    (correct) values entered during an audit.

    One per asset row. ``expected`` holds the corrected values the user entered
    (so the panel is sticky per asset); ``changes`` holds only the fields that
    differ from ``current``. Exported as JSON/CSV for a Chrome extension to push
    the right values back to the source web page.
    """

    STATUS_PENDING = "pending"
    STATUS_APPLIED = "applied"
    STATUS_CHOICES = [(STATUS_PENDING, "Pending"), (STATUS_APPLIED, "Applied")]

    asset = models.OneToOneField(
        Asset, on_delete=models.CASCADE, related_name="correction"
    )
    asset_tag = models.CharField(max_length=200, blank=True, db_index=True)
    current = models.JSONField(default=dict)   # {field: current value at save time}
    expected = models.JSONField(default=dict)  # {field: corrected value entered}
    # Differences as parallel dicts:
    #   {"current": {"state": "In Use"}, "expected": {"state": "In stock"}}
    # Stored as "" when nothing differs. A re-scan overwrites this record.
    changes = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20, default=STATUS_PENDING, choices=STATUS_CHOICES, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Who last touched this row. Named updated_by (not created_by) because both
    # write paths use update_or_create — a re-scan overwrites the record, so the
    # honest meaning is "last edited by", pairing with updated_at above.
    # Nullable: every row that predates login has no user.
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="corrections",
    )

    class Meta:
        ordering = ["-updated_at"]
        # The app-wide "may change data" permission, replacing the old shared
        # unlock code. Hung on this model because Correction is what edits
        # produce; it gates every write, not just corrections.
        permissions = [("can_edit_inventory", "Can make inventory edits")]

    def __str__(self):
        return f"Correction for {self.asset_tag} ({len(self.diff_rows())} changes)"

    def diff_rows(self):
        """Flatten ``changes`` into [{field, label, current, expected}] rows,
        handling both the parallel-dict shape and any legacy per-field shape."""
        ch = self.changes
        if not isinstance(ch, dict) or not ch:
            return []

        def label_for(field):
            try:
                return audit_field_label(field)
            except Exception:
                return field

        rows = []
        if "current" in ch or "expected" in ch:  # parallel-dict shape
            cur = ch.get("current", {}) or {}
            exp = ch.get("expected", {}) or {}
            for field in (exp.keys() or cur.keys()):
                rows.append({
                    "field": field, "label": label_for(field),
                    "current": cur.get(field, ""), "expected": exp.get(field, ""),
                })
        else:  # legacy {field: {label, current, expected}}
            for field, d in ch.items():
                if isinstance(d, dict):
                    rows.append({
                        "field": field, "label": d.get("label", field),
                        "current": d.get("current", ""), "expected": d.get("expected", ""),
                    })
        return rows


class SavedFilter(models.Model):
    """A filter value the user has previously selected on the asset browser.

    Persisting selections lets us pin previously-used options to the top of
    each filter list. ``field`` is the logical filter key ("state",
    "category", "location"); ``value`` is the chosen option text.
    """

    field = models.CharField(max_length=50, db_index=True)
    value = models.TextField()
    use_count = models.PositiveIntegerField(default=0)
    last_used = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("field", "value")
        ordering = ["-last_used"]

    def __str__(self):
        return f"{self.field}={self.value}"


class EditStrategy(models.Model):
    """A way to set a field's value in ServiceNow's inline edit (a column in
    the strategy matrix). Names are editable; the A–E ones are placeholders to
    be renamed as new strategies are discovered while re-valuing cells."""

    name = models.CharField(max_length=100)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class FieldStrategyMark(models.Model):
    """A checkmark in the matrix: ``field`` uses ``strategy``. Presence of a
    row means the box is checked."""

    field = models.CharField(max_length=100, db_index=True)  # audit field key
    strategy = models.ForeignKey(
        EditStrategy, on_delete=models.CASCADE, related_name="marks"
    )

    class Meta:
        unique_together = ("field", "strategy")

    def __str__(self):
        return f"{self.field} → {self.strategy}"


class AuditNote(models.Model):
    """A free-text note recorded during auditing (e.g. "asset tags missing in
    this area"). Shown on the audit workstation and the corrections page, and
    kept separate from corrections so the bulk "delete all pending" never
    removes them."""

    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.text[:60]


class RfMobileCheck(models.Model):
    """Marks an RF-mobile asset as physically found during an audit, for a given
    month (``period`` = "YYYY-MM"), recording when it was checked. A row's absence
    for a period means it's "Missing at Audit" that month."""

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="rf_checks")
    period = models.CharField(max_length=7, db_index=True, default="")  # "YYYY-MM"
    checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("asset", "period")

    def __str__(self):
        return f"RF check {self.asset_id} {self.period} @ {self.checked_at:%Y-%m-%d %H:%M}"


class RfMobileComment(models.Model):
    """A note for an RF-mobile asset, per month (``period`` = "YYYY-MM"),
    linked to the asset (and thus its asset tag)."""

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="rf_comments")
    period = models.CharField(max_length=7, db_index=True)
    text = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("asset", "period")

    def __str__(self):
        return f"RF comment {self.asset_id} {self.period}"


class RfSnapshot(models.Model):
    """A point-in-time record of the RF-mobile audit for a month: how many were
    checked vs missing, with the Station-ID / State breakdowns."""

    taken_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Which site the snapshot covers. Null on every row predating warehouses
    # (those were all WH1); the data migration backfills them.
    warehouse = models.ForeignKey(Warehouse, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="rf_snapshots")
    period = models.CharField(max_length=7, db_index=True)
    total = models.IntegerField(default=0)
    checked = models.IntegerField(default=0)
    missing = models.IntegerField(default=0)
    checked_pct = models.IntegerField(default=0)
    stations = models.JSONField(default=list)  # [{label, count}] (checked) — donut data
    states = models.JSONField(default=list)
    rows = models.JSONField(default=list)      # the full table at snapshot time

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"RF snapshot {self.period} — {self.checked}/{self.total}"


class AuditScan(models.Model):
    """A quick (mobile) audit of one asset: the fields the auditor flagged as a
    mistake are saved as a {field: value} dictionary, with the date."""

    asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="audit_scans")
    asset_tag = models.CharField(max_length=200, blank=True, db_index=True)
    serial_number = models.CharField(max_length=200, blank=True)
    flagged = models.JSONField(default=dict)  # {field: value} marked as a mistake
    comment = models.TextField(blank=True)  # optional note captured at submit time
    created_at = models.DateTimeField(auto_now_add=True)
    handled = models.BooleanField(default=False)  # reviewer marked this row done (voided)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"AuditScan {self.asset_tag} ({len(self.flagged)} flagged) {self.created_at:%Y-%m-%d}"


class PlannedAudit(models.Model):
    """A planned audit: a random sample of one site's inventory, drawn at the
    percentage the requester asked for, which the auditor then marks pass/fail.

    Private to ``created_by`` and to administrators (staff) — see
    ``views._can_view_planned``. The rows are snapshotted into
    ``PlannedAuditItem`` so the report stays what was audited even after an
    xlsx re-import replaces the Asset table."""

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="planned_audits")
    warehouse = models.ForeignKey(Warehouse, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="planned_audits")
    name = models.CharField(max_length=20, blank=True)  # "20261008", then "20261008-1", "-2"… same day
    percent = models.PositiveSmallIntegerField()        # % of the site's inventory requested
    population = models.IntegerField(default=0)         # assets in scope when drawn
    sample_size = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Planned audit {self.name or self.pk} {self.percent}% ({self.sample_size}/{self.population})"


class PlannedAuditItem(models.Model):
    """One sampled asset in a PlannedAudit, with the values as they were drawn."""

    RESULT_PASS = "pass"
    RESULT_FAIL = "fail"
    RESULT_CHOICES = [(RESULT_PASS, "Pass"), (RESULT_FAIL, "Fail")]

    audit = models.ForeignKey(PlannedAudit, on_delete=models.CASCADE, related_name="items")
    asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="planned_audit_items")
    asset_tag = models.CharField(max_length=200, blank=True)
    serial_number = models.CharField(max_length=200, blank=True)
    model_category = models.CharField(max_length=200, blank=True)
    display_name = models.CharField(max_length=300, blank=True)
    name = models.CharField(max_length=300, blank=True)  # for In stock: where the device sits
    station_id = models.CharField(max_length=300, blank=True)
    gl_account = models.CharField(max_length=300, blank=True)
    location = models.CharField(max_length=300, blank=True)
    assigned_to = models.CharField(max_length=300, blank=True)
    state = models.CharField(max_length=100, blank=True)
    result = models.CharField(max_length=4, choices=RESULT_CHOICES, blank=True)  # "" = not yet audited
    comment = models.TextField(blank=True)  # why it failed; only kept while result == fail
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["state", "asset_tag"]

    def __str__(self):
        return f"{self.asset_tag} — {self.result or 'pending'}"


class ReportSnapshot(models.Model):
    """A point-in-time record of the Station report totals (donut) and the
    per-model-category / Station ID / state breakdowns shown as range bars.

    ``taken_at`` is the date/time the system held these values. The three
    breakdown lists are stored as JSON: each item is
    ``{"<dimension>": name, "inventoried": int, "total": int}``.
    """

    taken_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Which site the snapshot covers. Null on every row predating warehouses
    # (those were all WH1); the data migration backfills them.
    warehouse = models.ForeignKey(Warehouse, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="report_snapshots")
    source_filename = models.CharField(max_length=255, blank=True)
    total_assets = models.IntegerField(default=0)
    inventoried = models.IntegerField(default=0)
    pending = models.IntegerField(default=0)
    marked_pct = models.IntegerField(default=0)
    categories = models.JSONField(default=list)  # [{category, inventoried, total}]
    stations = models.JSONField(default=list)    # [{station, inventoried, total}]
    states = models.JSONField(default=list)      # [{state, inventoried, total}]

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"Snapshot {self.taken_at:%Y-%m-%d %H:%M} — {self.inventoried}/{self.total_assets}"


class ImpressionSnapshot(models.Model):
    """A saved set of printer total-impression (page count) readings, captured
    on demand from the Impressions page. ``readings`` is the per-printer list
    ``[{asset_tag, area, model, ip, impressions}]``; ``period`` is the "YYYY-MM"
    the readings were taken in (shown as the month on the emailed report)."""

    taken_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Which site the readings cover. Null on every row predating warehouses —
    # those pre-date the split and MIX Main/Annex with WH2 printers, which is
    # why the migration leaves them null rather than claiming they are WH1.
    warehouse = models.ForeignKey(Warehouse, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="impression_snapshots")
    period = models.CharField(max_length=7, blank=True, db_index=True)  # "YYYY-MM"
    total_impressions = models.BigIntegerField(default=0)
    printer_count = models.IntegerField(default=0)
    readings = models.JSONField(default=list)

    class Meta:
        ordering = ["-taken_at"]

    def __str__(self):
        return f"Impressions {self.taken_at:%Y-%m-%d %H:%M} — {self.total_impressions:,}"


class IntuneDevice(models.Model):
    """One row from a Microsoft Intune "All devices" CSV export.

    Same convention as ``Asset``: every column is a real DB column whose
    ``db_column``/``verbose_name`` is the exact CSV header. Values are kept as
    raw text (dates included) rather than parsed — the export uses .NET
    sentinel dates (``0001-01-01...`` / ``9999-12-31...``) and 7-digit
    fractional seconds that aren't meaningfully "a date" without per-field
    interpretation, so this table is a faithful mirror of the export rather
    than a curated one.
    """

    device_id = models.TextField("Device ID", db_column="Device ID", null=True, blank=True, db_index=True)
    device_name = models.TextField("Device name", db_column="Device name", null=True, blank=True)
    enrollment_date = models.TextField("Enrollment date", db_column="Enrollment date", null=True, blank=True)
    last_check_in = models.TextField("Last check-in", db_column="Last check-in", null=True, blank=True)
    azure_ad_device_id = models.TextField("Azure AD Device ID", db_column="Azure AD Device ID", null=True, blank=True)
    os_version = models.TextField("OS version", db_column="OS version", null=True, blank=True)
    azure_ad_registered = models.TextField("Azure AD registered", db_column="Azure AD registered", null=True, blank=True)
    eas_activation_id = models.TextField("EAS activation ID", db_column="EAS activation ID", null=True, blank=True)
    serial_number = models.TextField("Serial number", db_column="Serial number", null=True, blank=True, db_index=True)
    manufacturer = models.TextField("Manufacturer", db_column="Manufacturer", null=True, blank=True)
    model_name = models.TextField("Model", db_column="Model", null=True, blank=True)
    eas_activated = models.TextField("EAS activated", db_column="EAS activated", null=True, blank=True)
    imei = models.TextField("IMEI", db_column="IMEI", null=True, blank=True)
    last_eas_sync_time = models.TextField("Last EAS sync time", db_column="Last EAS sync time", null=True, blank=True)
    eas_reason = models.TextField("EAS reason", db_column="EAS reason", null=True, blank=True)
    eas_status = models.TextField("EAS status", db_column="EAS status", null=True, blank=True)
    compliance_grace_period_expiration = models.TextField("Compliance grace period expiration", db_column="Compliance grace period expiration", null=True, blank=True)
    security_patch_level = models.TextField("Security patch level", db_column="Security patch level", null=True, blank=True)
    wifi_mac = models.TextField("Wi-Fi MAC", db_column="Wi-Fi MAC", null=True, blank=True)
    meid = models.TextField("MEID", db_column="MEID", null=True, blank=True)
    subscriber_carrier = models.TextField("Subscriber carrier", db_column="Subscriber carrier", null=True, blank=True)
    total_storage = models.TextField("Total storage", db_column="Total storage", null=True, blank=True)
    free_storage = models.TextField("Free storage", db_column="Free storage", null=True, blank=True)
    management_name = models.TextField("Management name", db_column="Management name", null=True, blank=True)
    category = models.TextField("Category", db_column="Category", null=True, blank=True)
    user_id = models.TextField("UserId", db_column="UserId", null=True, blank=True)
    primary_user_upn = models.TextField("Primary user UPN", db_column="Primary user UPN", null=True, blank=True)
    primary_user_email_address = models.TextField("Primary user email address", db_column="Primary user email address", null=True, blank=True)
    primary_user_display_name = models.TextField("Primary user display name", db_column="Primary user display name", null=True, blank=True, db_index=True)
    wifi_ipv4_address = models.TextField("WiFiIPv4Address", db_column="WiFiIPv4Address", null=True, blank=True)
    wifi_subnet_id = models.TextField("WiFiSubnetID", db_column="WiFiSubnetID", null=True, blank=True)
    compliance = models.TextField("Compliance", db_column="Compliance", null=True, blank=True)
    managed_by = models.TextField("Managed by", db_column="Managed by", null=True, blank=True)
    ownership = models.TextField("Ownership", db_column="Ownership", null=True, blank=True)
    device_state = models.TextField("Device state", db_column="Device state", null=True, blank=True)
    intune_registered = models.TextField("Intune registered", db_column="Intune registered", null=True, blank=True)
    supervised = models.TextField("Supervised", db_column="Supervised", null=True, blank=True)
    encrypted = models.TextField("Encrypted", db_column="Encrypted", null=True, blank=True)
    os = models.TextField("OS", db_column="OS", null=True, blank=True)
    sku_family = models.TextField("SkuFamily", db_column="SkuFamily", null=True, blank=True)
    join_type = models.TextField("JoinType", db_column="JoinType", null=True, blank=True)
    phone_number = models.TextField("Phone number", db_column="Phone number", null=True, blank=True)
    jailbroken = models.TextField("Jailbroken", db_column="Jailbroken", null=True, blank=True)
    iccid = models.TextField("ICCID", db_column="ICCID", null=True, blank=True)
    ethernet_mac = models.TextField("EthernetMAC", db_column="EthernetMAC", null=True, blank=True)
    cellular_technology = models.TextField("CellularTechnology", db_column="CellularTechnology", null=True, blank=True)
    processor_architecture = models.TextField("ProcessorArchitecture", db_column="ProcessorArchitecture", null=True, blank=True)
    eid = models.TextField("EID", db_column="EID", null=True, blank=True)
    system_management_bios_version = models.TextField("SystemManagementBIOSVersion", db_column="SystemManagementBIOSVersion", null=True, blank=True)
    tpm_manufacturer_id = models.TextField("TPMManufacturerId", db_column="TPMManufacturerId", null=True, blank=True)
    tpm_manufacturer_version = models.TextField("TPMManufacturerVersion", db_column="TPMManufacturerVersion", null=True, blank=True)
    product_name = models.TextField("ProductName", db_column="ProductName", null=True, blank=True)
    management_certificate_expiration_date = models.TextField("Management certificate expiration date", db_column="Management certificate expiration date", null=True, blank=True)

    # Provenance.
    source_filename = models.CharField(max_length=255, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.device_name or f"IntuneDevice #{self.pk}"


# Maps CSV header -> model field name. Single source of truth used by the
# importer. Keep in sync with the fields declared on IntuneDevice above.
INTUNE_HEADER_MAP = {
    "Device ID": "device_id",
    "Device name": "device_name",
    "Enrollment date": "enrollment_date",
    "Last check-in": "last_check_in",
    "Azure AD Device ID": "azure_ad_device_id",
    "OS version": "os_version",
    "Azure AD registered": "azure_ad_registered",
    "EAS activation ID": "eas_activation_id",
    "Serial number": "serial_number",
    "Manufacturer": "manufacturer",
    "Model": "model_name",
    "EAS activated": "eas_activated",
    "IMEI": "imei",
    "Last EAS sync time": "last_eas_sync_time",
    "EAS reason": "eas_reason",
    "EAS status": "eas_status",
    "Compliance grace period expiration": "compliance_grace_period_expiration",
    "Security patch level": "security_patch_level",
    "Wi-Fi MAC": "wifi_mac",
    "MEID": "meid",
    "Subscriber carrier": "subscriber_carrier",
    "Total storage": "total_storage",
    "Free storage": "free_storage",
    "Management name": "management_name",
    "Category": "category",
    "UserId": "user_id",
    "Primary user UPN": "primary_user_upn",
    "Primary user email address": "primary_user_email_address",
    "Primary user display name": "primary_user_display_name",
    "WiFiIPv4Address": "wifi_ipv4_address",
    "WiFiSubnetID": "wifi_subnet_id",
    "Compliance": "compliance",
    "Managed by": "managed_by",
    "Ownership": "ownership",
    "Device state": "device_state",
    "Intune registered": "intune_registered",
    "Supervised": "supervised",
    "Encrypted": "encrypted",
    "OS": "os",
    "SkuFamily": "sku_family",
    "JoinType": "join_type",
    "Phone number": "phone_number",
    "Jailbroken": "jailbroken",
    "ICCID": "iccid",
    "EthernetMAC": "ethernet_mac",
    "CellularTechnology": "cellular_technology",
    "ProcessorArchitecture": "processor_architecture",
    "EID": "eid",
    "SystemManagementBIOSVersion": "system_management_bios_version",
    "TPMManufacturerId": "tpm_manufacturer_id",
    "TPMManufacturerVersion": "tpm_manufacturer_version",
    "ProductName": "product_name",
    "Management certificate expiration date": "management_certificate_expiration_date",
}


class IntuneImport(models.Model):
    """Metadata about the currently-loaded Intune device export.

    Same "only the latest is kept" convention as ``AssetImport``.
    """

    filename = models.CharField(max_length=255)
    row_count = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-imported_at"]


class FixPerson(models.Model):
    """A person selectable in the Intune page's "Fix" picker — a small, shared
    roster grown from the picker modal's own "add" section (name + initials)."""

    name = models.CharField(max_length=100)
    initials = models.CharField(max_length=8)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.initials})"

    # Validated 8-slot categorical palette (light mode) — fixed hue order, not
    # cycled/re-picked. Keyed by ``id`` (creation order) rather than list
    # position so a person's color never shifts when the roster is re-sorted
    # or someone new is added ahead of them alphabetically. Beyond 8 people
    # the slots repeat — acceptable for a small internal roster.
    _PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

    @property
    def color(self):
        return self._PALETTE[(self.id or 0) % len(self._PALETTE)]

    @property
    def text_color(self):
        # The yellow slot is light enough to need dark text; the rest read fine in white.
        return "#1f2933" if self.color == "#eda100" else "#fff"


class IntuneFixAssignment(models.Model):
    """Which ``FixPerson`` is assigned to fix a given asset on the Intune page.
    One per asset (keyed by normalized asset tag) — picking a different person
    overwrites it.

    Keyed by ``asset_tag`` rather than a foreign key to ``Asset``: the
    ServiceNow xlsx re-import truncates and re-creates every ``Asset`` row
    (new PKs each time), which would cascade-delete an FK-based assignment
    the moment that asset's row is replaced — even though it's still "the
    same" asset from the user's point of view. Storing the tag directly
    keeps the assignment alive across re-imports and re-attaches
    automatically by tag match; ``serial_number`` is kept alongside purely
    for reference/display, not as a lookup key.
    """

    asset_tag = models.CharField(max_length=200, unique=True, db_index=True)
    serial_number = models.CharField(max_length=200, blank=True)
    person = models.ForeignKey(FixPerson, on_delete=models.CASCADE, related_name="assignments")
    assigned_at = models.DateTimeField(auto_now=True)
    # Marking/unmarking is gated client+server-side by typing the assigned
    # person's initials as a lightweight confirmation (not real auth) — see
    # views.intune_fix_mark.
    fixed = models.BooleanField(default=False)
    fixed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-assigned_at"]

    def __str__(self):
        return f"{self.asset_tag} -> {self.person}"


class AccessLog(models.Model):
    """One page view: the source IP a user connected from, the page (URL name)
    and path they visited, and when. Written by ``AccessLogMiddleware`` for plain
    HTML GET navigations only (AJAX/JSON endpoints and POST actions are skipped).
    Surfaced read-only on the unlocked-only ``/access-logs/`` page."""

    ip = models.CharField(max_length=45, db_index=True)   # 45 covers IPv6
    path = models.CharField(max_length=300)
    page = models.CharField(max_length=100, blank=True)   # resolved url_name label
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Null for anonymous views (reads don't require login) and for every row
    # written before accounts existed. The IP is still recorded either way.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="access_logs",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.ip} {self.path} {self.created_at:%Y-%m-%d %H:%M}"
