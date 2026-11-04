"""Spreadsheet parsing and reconciliation logic.

Kept separate from views so it can be unit-tested in isolation.
"""

import re
from dataclasses import dataclass, field

from openpyxl import load_workbook

from . import warehouse as warehouse_mod
from .models import ExpectedItem, ReconciliationSession, ScannedItem, normalize_tag


# --- Printer Comments structured fields ------------------------------------

# Order of the structured sub-fields packed into the Comments text.
PRINTER_COMMENT_KEYS = ["IP", "FMS", "FMS 2", "DMS", "Service ID", "FEDEX", "UPS", "Windows Queue"]

# Map the keywords seen in real Comments to canonical column keys. Longer/more
# specific aliases must come before shorter ones (e.g. "FMS 2" before "FMS").
_FIELD_ALIASES = [
    ("FMS 2", "FMS 2"),
    ("FMS", "FMS"),
    ("IP", "IP"),
    ("DMS", "DMS"),
    ("FEDEX", "FEDEX"), ("FED EX", "FEDEX"), ("FED-EX", "FEDEX"),
    ("UPS", "UPS"),
    ("WINDOWS QUEUE", "Windows Queue"), ("WINDOWS", "Windows Queue"),
    ("SERVICE ID", "Service ID"), ("SERVICE", "Service ID"), ("ID", "Service ID"),
]


def parse_comment_fields(text):
    """Parse Comments into ordered (key, value) pairs.

    Handles both the compiled form ``"IP: x / FMS: y"`` and the legacy free-form
    ``"IP 10.0.0.1 | DMS foo | Windows host"`` (pipe or slash separators; the key
    and value split on a colon or whitespace). Known keywords are mapped to their
    canonical column key; unknown keys are kept as-is so nothing is lost.
    """
    pairs = []
    for seg in re.split(r"[|/]", text or ""):
        seg = seg.strip()
        if not seg:
            continue
        up = seg.upper()
        canonical, alen = None, 0
        for alias, canon in _FIELD_ALIASES:
            if up.startswith(alias):
                after = seg[len(alias):len(alias) + 1]
                if after == "" or not after.isalpha():  # allow space/colon/digit, not letters
                    canonical, alen = canon, len(alias)
                    break
        if canonical:
            value = seg[alen:].lstrip(" \t:=-").strip()
            pairs.append((canonical, value))
        elif ":" in seg:
            key, value = seg.split(":", 1)
            if key.strip():
                pairs.append((key.strip(), value.strip()))
        else:
            parts = seg.split(None, 1)
            pairs.append((parts[0].strip(), parts[1].strip() if len(parts) == 2 else ""))
    return pairs


def comment_fields_map(text):
    """Lower-cased {key: value} lookup of the structured Comments fields."""
    return {k.lower(): v for k, v in parse_comment_fields(text)}


def compile_comment_fields(pairs):
    """Serialize (key, value) pairs back to ``"KEY: value / KEY2: value2"``,
    dropping any with an empty value."""
    return " / ".join(f"{k}: {v}" for k, v in pairs if k and v)


# Digit lookarounds (not \b) so an IP glued to letters still matches, e.g. "|IP10.0.0.1".
_IPV4_RE = re.compile(r"(?<!\d)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d)")


def extract_ip(text):
    """Return the first valid IPv4 address found anywhere in ``text``.

    The IP is packed into Comments very inconsistently (``IP:10.0.0.1``,
    ``IP 10.0.0.1``, ``|IP10.0.0.1``), so a structured parse misses many. Here we
    just scan for the first dotted-quad whose octets are all 0-255, or ``""``.
    """
    for m in _IPV4_RE.finditer(text or ""):
        if all(0 <= int(o) <= 255 for o in m.groups()):
            return m.group(0)
    return ""


# --- Station report inventory aggregates -----------------------------------

def warehouse_marked_assets(wh):
    """One warehouse's assets, each tagged with ``inventory_marked`` (GL 202606).

    Single source of truth for the site filter and the 202606 marker rule,
    shared by the Station report view and the snapshot save. This used to be
    ``main_annex_marked_assets()`` with the Main/Annex locations hard-coded;
    the scope now comes from the ``Warehouse`` row (Station ID prefix OR
    Location) so the same report serves WH1 and WH2 — see ``warehouse.py``.
    """
    assets = list(warehouse_mod.scoped_assets(wh))
    for a in assets:
        a.inventory_marked = "202606" in (a.gl_account or "").strip()
    return assets


def inventory_breakdowns(assets):
    """Totals + per-category / Station ID / state breakdowns from marked assets.

    ``assets`` must already have ``inventory_marked`` set. Returns a dict used
    both for the report panels and for snapshotting.
    """
    total = len(assets)
    inventoried = sum(1 for a in assets if getattr(a, "inventory_marked", False))
    pending = total - inventoried
    pct = round(inventoried / total * 100) if total else 0

    def grouped(key_fn, label):
        buckets = {}
        for a in assets:
            key = key_fn(a)
            row = buckets.setdefault(key, {label: key, "inventoried": 0, "total": 0})
            row["total"] += 1
            if getattr(a, "inventory_marked", False):
                row["inventoried"] += 1
        # Sort by completion (inventoried / total), highest first; then by count.
        return sorted(
            buckets.values(),
            key=lambda r: (-(r["inventoried"] / r["total"]), -r["inventoried"], r[label].lower()),
        )

    return {
        "total_assets": total,
        "inventoried": inventoried,
        "pending": pending,
        "marked_pct": pct,
        "categories": grouped(lambda a: (a.model_category or "").strip() or "(no category)", "category"),
        "stations": grouped(lambda a: (a.station_id or "").strip() or "(no Station ID)", "station"),
        "states": grouped(lambda a: (a.state or "").strip() or "(no state)", "state"),
    }


def substate_breakdown(assets, state_label):
    """Per-Substate inventoried/total for the assets in one State.

    ``assets`` must already have ``inventory_marked`` set (as ``inventory_breakdowns``
    expects). Filters to assets whose ``state`` matches ``state_label`` (case-insensitive)
    and groups by ``substate`` — blanks fall into ``"(no substate)"``. Same sort key as
    the report's other breakdowns: highest completion first, then count, then name.
    """
    target = (state_label or "").strip().lower()
    buckets = {}
    for a in assets:
        if (a.state or "").strip().lower() != target:
            continue
        key = (a.substate or "").strip() or "(no substate)"
        row = buckets.setdefault(key, {"substate": key, "inventoried": 0, "total": 0})
        row["total"] += 1
        if getattr(a, "inventory_marked", False):
            row["inventoried"] += 1
    return sorted(
        buckets.values(),
        key=lambda r: (-(r["inventoried"] / r["total"]), -r["inventoried"], r["substate"].lower()),
    )


def category_state_matrix(assets):
    """Model category × State pivot, each cell split into marked/unmarked by the
    202606 GL-account marker (``inventory_marked``). The Retired column is also
    broken out by Substate. Built from the same asset list as the report.

    Every cell is ``{"m": <has 202606>, "u": <hasn't>}``; the view later adds
    ``marked_url`` / ``unmarked_url`` (ServiceNow links). Returns column names
    (Retired last, if present), the Retired substate columns, per-category rows
    (state cells + retired substate cells + row total cell), column totals, and a
    grand-total cell.
    """
    RET = "retired"

    def blank():
        return {"m": 0, "u": 0}

    def add(cell, marked):
        cell["m" if marked else "u"] += 1

    states, substates, cats = set(), set(), {}
    for a in assets:
        cat = (a.model_category or "").strip() or "(no category)"
        state = (a.state or "").strip() or "(no state)"
        marked = bool(getattr(a, "inventory_marked", False))
        states.add(state)
        row = cats.setdefault(cat, {"st": {}, "sub": {}, "total": blank()})
        add(row["st"].setdefault(state, blank()), marked)
        add(row["total"], marked)
        if state.lower() == RET:
            sub = (a.substate or "").strip() or "(no substate)"
            substates.add(sub)
            add(row["sub"].setdefault(sub, blank()), marked)

    retired_present = any(s.lower() == RET for s in states)
    columns = sorted((s for s in states if s.lower() != RET), key=str.lower)
    if retired_present:
        columns.append("Retired")
    subcols = sorted(substates, key=str.lower)

    def get(d, key):
        c = d.get(key)
        return {"m": c["m"], "u": c["u"]} if c else blank()

    rows = []
    for cat in sorted(cats, key=str.lower):
        r = cats[cat]
        rows.append({
            "category": cat,
            "cells": [{**get(r["st"], c), "state": c} for c in columns],
            "retired_cells": [{**get(r["sub"], s), "substate": s} for s in subcols],
            "total_cell": {"m": r["total"]["m"], "u": r["total"]["u"]},
        })

    def col_total(get_cell):
        return {"m": sum(get_cell(row)["m"] for row in rows),
                "u": sum(get_cell(row)["u"] for row in rows)}

    col_totals = []
    for i, c in enumerate(columns):
        t = col_total(lambda row, i=i: row["cells"][i])
        t["state"] = c
        col_totals.append(t)
    retired_col_totals = []
    for i, s in enumerate(subcols):
        t = col_total(lambda row, i=i: row["retired_cells"][i])
        t["substate"] = s
        retired_col_totals.append(t)

    grand_cell = {"m": sum(r["total_cell"]["m"] for r in rows),
                  "u": sum(r["total_cell"]["u"] for r in rows)}
    return {
        "columns": columns,
        "retired_present": retired_present,
        "retired_subcols": subcols,
        "rows": rows,
        "col_totals": col_totals,
        "retired_col_totals": retired_col_totals,
        "grand_cell": grand_cell,
    }


# --- Status constants ------------------------------------------------------

MATCH = "match"          # asset tag present on both sides, all fields agree
MISMATCH = "mismatch"    # asset tag present on both sides, some field differs
MISSING = "missing"      # in the spreadsheet, never scanned
UNEXPECTED = "unexpected"  # scanned, but not in the spreadsheet

STATUS_LABELS = {
    MATCH: "Match",
    MISMATCH: "Field mismatch",
    MISSING: "Missing (not scanned)",
    UNEXPECTED: "Unexpected (not in sheet)",
}


# --- Spreadsheet parsing ---------------------------------------------------

def read_headers(file_obj) -> list:
    """Return the header row (first row) of the first worksheet."""
    wb = load_workbook(file_obj, data_only=True)
    ws = wb.active
    headers = []
    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
        headers = [("" if c is None else str(c).strip()) for c in row]
        break
    wb.close()
    # Drop trailing empty headers.
    while headers and headers[-1] == "":
        headers.pop()
    return headers


def _cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def import_expected_items(session: ReconciliationSession) -> int:
    """Parse the session's uploaded file into ExpectedItem rows.

    Replaces any previously imported expected items. Returns the count
    imported. ``session.columns`` and ``session.asset_tag_column`` must
    already be set.
    """
    session.expected_items.all().delete()

    # NOT read_only: some exporters (e.g. ServiceNow) write incorrect worksheet
    # dimensions, and read_only mode then streams only the header row.
    wb = load_workbook(session.expected_file.path, data_only=True)
    ws = wb.active

    headers = session.columns
    tag_col = session.asset_tag_column
    try:
        tag_index = headers.index(tag_col)
    except ValueError:
        wb.close()
        raise ValueError(f"Asset-tag column {tag_col!r} not found in spreadsheet.")

    items = []
    for row_number, row in enumerate(
        ws.iter_rows(min_row=2, values_only=True), start=2
    ):
        values = [_cell_to_str(c) for c in row]
        # Skip fully blank rows.
        if not any(values):
            continue
        data = {}
        for i, header in enumerate(headers):
            data[header] = values[i] if i < len(values) else ""
        raw_tag = values[tag_index] if tag_index < len(values) else ""
        # Skip rows with no asset tag at all.
        if not raw_tag:
            continue
        items.append(
            ExpectedItem(
                session=session,
                asset_tag=normalize_tag(raw_tag),
                data=data,
                row_number=row_number,
            )
        )
    wb.close()
    ExpectedItem.objects.bulk_create(items)
    return len(items)


# --- Reconciliation --------------------------------------------------------

@dataclass
class FieldDiff:
    column: str
    expected: str
    scanned: str

    @property
    def differs(self) -> bool:
        return (self.expected or "").strip() != (self.scanned or "").strip()


@dataclass
class ComparisonRow:
    asset_tag: str
    status: str
    expected: object = None      # ExpectedItem or None
    scanned: object = None       # ScannedItem or None
    field_diffs: list = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


def compare(session: ReconciliationSession) -> dict:
    """Compare expected vs. scanned items for a session.

    Returns a dict with the per-asset rows and summary counts.
    """
    expected = {e.asset_tag: e for e in session.expected_items.all()}
    scanned = {s.asset_tag: s for s in session.scanned_items.all()}
    value_columns = session.value_columns

    rows = []
    for tag in sorted(set(expected) | set(scanned)):
        exp = expected.get(tag)
        scn = scanned.get(tag)

        if exp and not scn:
            rows.append(ComparisonRow(tag, MISSING, expected=exp))
            continue
        if scn and not exp:
            rows.append(ComparisonRow(tag, UNEXPECTED, scanned=scn))
            continue

        # Present on both sides: compare the value columns.
        diffs = []
        for col in value_columns:
            diffs.append(
                FieldDiff(
                    column=col,
                    expected=exp.data.get(col, ""),
                    scanned=scn.data.get(col, ""),
                )
            )
        any_differs = any(d.differs for d in diffs)
        rows.append(
            ComparisonRow(
                tag,
                MISMATCH if any_differs else MATCH,
                expected=exp,
                scanned=scn,
                field_diffs=diffs,
            )
        )

    counts = {status: 0 for status in STATUS_LABELS}
    for r in rows:
        counts[r.status] += 1

    return {
        "rows": rows,
        "counts": counts,
        "total_expected": len(expected),
        "total_scanned": len(scanned),
    }
