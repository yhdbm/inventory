import csv
import io
import json
import os
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import date, timezone as dt_timezone

from openpyxl import load_workbook

from django.conf import settings
from django.contrib import messages
from django.core.management import call_command
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from . import printer_supplies, services, warehouse as warehouse_mod
from .forms import ColumnMappingForm, ScanForm, SessionCreateForm
from .models import (
    ASSET_AUDIT_FIELDS,
    ASSET_IDENTITY_FIELDS,
    AccessLog,
    Asset,
    AssetImport,
    AuditNote,
    AuditScan,
    Correction,
    EditStrategy,
    FieldStrategyMark,
    FixPerson,
    ImpressionSnapshot,
    IntuneDevice,
    PlannedAudit,
    PlannedAuditItem,
    Warehouse,
    IntuneFixAssignment,
    IntuneImport,
    ReconciliationSession,
    ReportSnapshot,
    RfMobileCheck,
    RfMobileComment,
    RfSnapshot,
    SavedFilter,
    ScannedItem,
    audit_field_label,
    normalize_tag,
)

# Logical filter key -> (Asset model field, label). The GET param name is the
# logical key. All are multi-select (checkboxes), combined as OR within a
# filter and AND across filters.
ASSET_FILTERS = [
    ("state", "state", "State"),
    ("category", "model_category", "Category"),
    ("location", "location", "Location"),
]


@require_POST
def backup_project(request):
    """Launch the standalone backup script as a detached background process so a
    full (large) zip doesn't block the server. The dated .zip + .log appear next
    to the project folder when it finishes."""
    script = settings.BASE_DIR / "backup_project.py"
    if not script.exists():
        messages.error(request, "Backup script (backup_project.py) not found.")
        return redirect(request.META.get("HTTP_REFERER") or "/")
    kwargs = {"cwd": str(settings.BASE_DIR), "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, str(script)], **kwargs)
    except Exception as exc:
        messages.error(request, f"Could not start backup: {exc}")
        return redirect(request.META.get("HTTP_REFERER") or "/")
    messages.success(
        request,
        "Backup started in the background — the dated .zip will be saved to your "
        "OneDrive “InventoryBackups” folder (auto-uploaded to the cloud) when it "
        "finishes; a .log records the result.",
    )
    return redirect(request.META.get("HTTP_REFERER") or "/")

@require_POST
def warehouse_switch(request):
    """Staff-only: change which warehouse this browser session is scoped to.

    Session-only — nobody's profile is touched, so the switch is per-browser and
    ends with the session. Gated on ``is_staff`` here as well as being hidden
    from the navbar, because the URL is guessable; it is in
    ``middleware.WRITE_GATE_EXEMPT`` since it writes no data (see that list).
    """
    if not request.user.is_staff:
        messages.error(request, "Switching warehouse is available to administrators only.")
        return redirect("station_report")

    code = (request.POST.get("code") or "").strip()
    wh = warehouse_mod.by_code(code)
    if wh is None:
        messages.error(request, f"Unknown warehouse “{code}”.")
    else:
        request.session[warehouse_mod.SESSION_KEY] = wh.code
        messages.success(request, f"Now showing {wh.label}.")
    # Back to the page they were on; its scope is re-resolved on the way in.
    return redirect(request.META.get("HTTP_REFERER") or "/")


SNOW_BASE = "https://your-instance.service-now.com/now/nav/ui/classic/params/target/"
_SNOW_SAFE = "-_.!~*'()"  # same unreserved set as JS encodeURIComponent


def snow_list_url(tags):
    """ServiceNow alm_asset list URL filtered to the given asset tags
    (asset_tag=a^ORasset_tag=b…), double-encoded like the extension's links."""
    from urllib.parse import quote
    tags = [t.strip() for t in tags if t and t.strip()]
    if not tags:
        return ""
    inner = quote("^OR".join(f"asset_tag={t}" for t in tags), safe=_SNOW_SAFE)
    target = quote(
        f"alm_asset_list.do?sysparm_query={inner}&sysparm_first_row=1&sysparm_view=",
        safe=_SNOW_SAFE,
    )
    return SNOW_BASE + target


def snow_asset_url(asset_tag):
    """ServiceNow alm_asset list URL filtered to a single asset tag."""
    return snow_list_url([asset_tag])


# The alm_asset list as a SAVED ServiceNow list module — the starting point for
# exporting a fresh source .xlsx (SNOW's own export menu on that list). The
# ``sysparm_userpref_module`` GUID identifies that saved module and is NOT
# derivable from anything we store, so it's a fixed opaque literal supplied by
# the user; ``sysparm_clear_stack=true`` drops any prior nav breadcrumb so the
# list opens clean. Surfaced on the station report's "Asset source" card.
SNOW_ASSET_EXPORT_URL = (
    "https://your-instance.service-now.com/now/nav/ui/classic/params/target/"
    "alm_asset_list.do%3Fsysparm_userpref_module%3D00000000000000000000000000000003"
    "%26sysparm_clear_stack%3Dtrue"
)


INTUNE_DEVICE_BASE = (
    "https://intune.microsoft.com/#view/Microsoft_Intune_Devices/"
    "DeviceSettingsMenuBlade/~/overview/mdmDeviceId/"
)


def intune_device_url(device_id):
    """Intune admin center device overview deep link for an IntuneDevice's
    ``Device ID`` (the Intune-assigned GUID, not the Azure AD Device ID)."""
    device_id = (device_id or "").strip()
    return INTUNE_DEVICE_BASE + device_id if device_id else ""


# The ServiceNow encoded-query condition that scopes a list to one site. This
# used to be a module constant pair of location sys_ids for CPT + Annex; it now
# comes off the Warehouse row so WH1 and WH2 each get their own scope. See
# Warehouse.snow_scope_condition() for the fallback when a site has no sys_ids.
def _snow_scope(wh):
    return wh.snow_scope_condition() if wh is not None else ""


# Encoded-query condition: GL account does NOT contain the "202606" inventory
# marker. Appended (always last) for the "unmarked" half of the report/pivot links.
# The trailing "^ORgl_accountISEMPTY" is REQUIRED: in ServiceNow (as in SQL), a
# "NOT LIKE" test is false/NULL for empty fields, so empty-GL devices would be missed
# without it. "^OR" groups with the preceding condition — i.e. AND(...) AND (NOT LIKE
# 202606 OR ISEMPTY) — so it must stay the final term in any query it's added to.
_GL_NOT_MARKED = "gl_accountNOT LIKE202606^ORgl_accountISEMPTY"


def snow_breakdown_url(value, wh, extra=""):
    """SNOW alm_asset list scoped to ``wh``, plus a text search for a breakdown
    value (model category / station / state) — a starting point the user can
    fine-tune in ServiceNow. ``extra`` appends one more encoded-query
    condition (e.g. the GL-marker exclusion)."""
    from urllib.parse import quote
    cond = _snow_scope(wh)
    v = (value or "").strip()
    if v and not v.startswith("("):  # skip "(no Station ID)" etc.
        cond += "^123TEXTQUERY321" + v
    if extra:
        cond += "^" + extra
    inner = quote(cond.lstrip("^"), safe=_SNOW_SAFE)
    target = quote(
        f"alm_asset_list.do?sysparm_query={inner}&sysparm_first_row=1&sysparm_view=",
        safe=_SNOW_SAFE,
    )
    return SNOW_BASE + target


def snow_model_category_url(category, wh, extra=""):
    """Precise SNOW alm_asset list for ONE model category within ``wh``.

    The user supplied exact links for two categories that filter on the
    ``model_category`` reference by its sys_id (e.g. ``model_category=b9faa1c7…``).
    Those sys_ids aren't derivable from the name, so to replicate the same exact
    filter for every category we dot-walk the reference instead:
    ``model_category.name=<name>`` resolves to the very same record in ServiceNow.
    ``extra`` appends one more encoded-query condition (e.g. the GL-marker
    exclusion). Double-encoded the same way as the other SNOW links."""
    v = (category or "").strip()
    if not v or v.startswith("("):
        return ""
    tail = f"model_category.name={v}"
    if extra:
        tail += "^" + extra
    return _snow_scoped_url(tail, wh)


def _snow_scoped_url(cond_tail, wh):
    """Build a double-encoded SNOW alm_asset list URL for ``wh``'s scope plus an
    extra encoded-query condition (e.g. ``u_station_id=WH1|x``)."""
    from urllib.parse import quote
    scope = _snow_scope(wh)
    cond = f"{scope}^{cond_tail}" if scope else cond_tail
    inner = quote(cond, safe=_SNOW_SAFE)
    target = quote(
        f"alm_asset_list.do?sysparm_query={inner}&sysparm_first_row=1&sysparm_view=",
        safe=_SNOW_SAFE,
    )
    return SNOW_BASE + target


def snow_station_url(station, wh, extra=""):
    """Precise SNOW alm_asset list for ONE Station ID within ``wh``.

    Station ID is the custom plain-text field ``u_station_id`` (e.g.
    ``u_station_id=WH1|Main Cubicles``), per the user-supplied sample URL.
    ``extra`` appends one more encoded-query condition."""
    v = (station or "").strip()
    if not v or v.startswith("("):
        return ""
    tail = f"u_station_id={v}"
    if extra:
        tail += "^" + extra
    return _snow_scoped_url(tail, wh)


# alm_asset "State" is the ``install_status`` CHOICE field, stored as a numeric
# code (not the label). Map the report's State labels to ServiceNow's standard
# codes; "In stock"=6 confirmed from a sample URL. Labels not listed here fall
# back to a text search (see snow_state_url).
_SN_STATE_CODES = {
    "in use": 1,
    "on order": 2,
    "in maintenance": 3,
    "in stock": 6,
    "retired": 7,
    "missing": 8,
    "in transit": 9,
    "consumed": 10,
    "build": 11,
}


def snow_state_url(state, wh, extra=""):
    """Precise SNOW alm_asset list for ONE State within ``wh``, via
    ``install_status=<code>``. Falls back to the text search for any label not
    in ``_SN_STATE_CODES`` (so it never silently filters on the wrong code).
    ``extra`` appends one more encoded-query condition."""
    v = (state or "").strip()
    if not v or v.startswith("("):
        return ""
    code = _SN_STATE_CODES.get(v.lower())
    if code is None:
        return snow_breakdown_url(state, wh, extra)
    tail = f"install_status={code}"
    if extra:
        tail += "^" + extra
    return _snow_scoped_url(tail, wh)


# GL account DOES contain the 202606 inventory marker (the "green" half of a cell).
_GL_MARKED = "gl_accountLIKE202606"


def _snow_substatus(substate):
    """ServiceNow ``substatus`` condition for a Retired substate label. Real values
    map to ``substatus=<slug>`` (lowercase, spaces -> underscores, e.g. "Pending
    disposal" -> ``substatus=pending_disposal``); a blank/"(no substate)" -> ISEMPTY."""
    s = (substate or "").strip()
    if not s or s.startswith("("):
        return "substatusISEMPTY"
    return "substatus=" + s.lower().replace(" ", "_")


def _matrix_cell_urls(category, state, wh, substate=None):
    """(marked_url, unmarked_url) ServiceNow links for one category×state[/substate]
    cell — same site scope as the rest of the report, filtered by category
    (dot-walk), install_status, and an optional substate text search, plus the GL
    202606 (marked) or NOT-202606 (unmarked) condition. Placeholders like
    "(no category)" are simply dropped."""
    parts = []
    cat = (category or "").strip()
    if cat and not cat.startswith("("):
        parts.append(f"model_category.name={cat}")
    st = (state or "").strip()
    code = _SN_STATE_CODES.get(st.lower())
    if code is not None:
        parts.append(f"install_status={code}")
    elif st and not st.startswith("("):
        parts.append("123TEXTQUERY321" + st)
    if substate is not None:   # a Retired substate cell → filter on substatus
        parts.append(_snow_substatus(substate))
    base = "^".join(parts)

    def url(gl):
        tail = (base + "^" + gl) if base else gl
        return _snow_scoped_url(tail, wh)

    return url(_GL_MARKED), url(_GL_NOT_MARKED)


def _decorate_cat_state_matrix(m, wh):
    """Attach ServiceNow marked/unmarked URLs to every cell of the pivot."""
    for row in m["rows"]:
        for c in row["cells"]:
            c["marked_url"], c["unmarked_url"] = _matrix_cell_urls(row["category"], c["state"], wh)
        for c in row["retired_cells"]:
            c["marked_url"], c["unmarked_url"] = _matrix_cell_urls(row["category"], "Retired", wh, c["substate"])
        tc = row["total_cell"]
        tc["marked_url"], tc["unmarked_url"] = _matrix_cell_urls(row["category"], None, wh)
    for c in m["col_totals"]:
        c["marked_url"], c["unmarked_url"] = _matrix_cell_urls(None, c["state"], wh)
    for c in m["retired_col_totals"]:
        c["marked_url"], c["unmarked_url"] = _matrix_cell_urls(None, "Retired", wh, c["substate"])
    g = m["grand_cell"]
    g["marked_url"], g["unmarked_url"] = _matrix_cell_urls(None, None, wh)
    return m


def _distinct_values(model_field):
    """Distinct non-empty values for a field, alphabetically.

    ``.order_by(model_field)`` is required so the model's Meta.ordering ("id")
    isn't injected into the SELECT DISTINCT (which would defeat distinctness).
    """
    return list(
        Asset.objects.exclude(**{f"{model_field}__isnull": True})
        .exclude(**{model_field: ""})
        .values_list(model_field, flat=True)
        .distinct()
        .order_by(model_field)
    )


def _remember_selections(key, values):
    """Persist the user's chosen values so they pin to the top next time."""
    now = timezone.now()
    for value in values:
        obj, _ = SavedFilter.objects.get_or_create(field=key, value=value)
        SavedFilter.objects.filter(pk=obj.pk).update(
            use_count=F("use_count") + 1, last_used=now
        )


def _handle_asset_upload(request):
    """Replace the entire Asset table with a freshly uploaded xlsx source.

    Saves the file (removing any previous one), truncates and re-imports, and
    records the new version's metadata (filename + the date baked into the xlsx).
    """
    f = request.FILES.get("source")
    if not f:
        messages.error(request, "Choose an .xlsx file to upload.")
        return redirect("station_report")
    if not f.name.lower().endswith(".xlsx"):
        messages.error(request, "Please upload an .xlsx file.")
        return redirect("station_report")

    # Save into media/asset_sources/, removing the previous version first.
    dest_dir = os.path.join(settings.MEDIA_ROOT, "asset_sources")
    os.makedirs(dest_dir, exist_ok=True)
    for old in os.listdir(dest_dir):
        try:
            os.remove(os.path.join(dest_dir, old))
        except OSError:
            pass
    dest_path = os.path.join(dest_dir, f.name)
    with open(dest_path, "wb") as fh:
        for chunk in f.chunks():
            fh.write(chunk)

    # The xlsx file's own creation date (workbook metadata).
    created = None
    try:
        wb = load_workbook(dest_path, data_only=True, read_only=True)
        created = wb.properties.created
        wb.close()
        if created and timezone.is_naive(created):
            created = created.replace(tzinfo=dt_timezone.utc)
    except Exception:
        created = None

    # Replace all rows (truncate) and import every tab.
    try:
        call_command("import_xlsx", dest_path, truncate=True)
    except Exception as exc:
        messages.error(request, f"Import failed: {exc}")
        return redirect("station_report")

    count = Asset.objects.count()
    AssetImport.objects.all().delete()   # keep only the current version
    AssetImport.objects.create(filename=f.name, file_created=created, row_count=count)
    messages.success(request, f"Imported {count} assets from “{f.name}” (previous data replaced).")
    return redirect("station_report")


def asset_list(request):
    """Browse the imported Asset table with search, filtering and paging."""
    if request.method == "POST":
        return _handle_asset_upload(request)

    qs = Asset.objects.all()

    query = request.GET.get("q", "").strip()
    if query:
        qs = qs.filter(
            Q(asset_tag__icontains=query)
            | Q(serial_number__icontains=query)
            | Q(display_name__icontains=query)
            | Q(name__icontains=query)
            | Q(assigned_to__icontains=query)
            | Q(model__icontains=query)
        )

    filters = []
    for key, model_field, label in ASSET_FILTERS:
        selected = [v for v in request.GET.getlist(key) if v.strip()]
        if selected:
            qs = qs.filter(**{f"{model_field}__in": selected})
            _remember_selections(key, selected)

        all_values = _distinct_values(model_field)
        existing = set(all_values)
        # Previously-saved selections (most-recent first) that still exist in
        # the data get pinned to the top; the rest follow alphabetically.
        saved = list(
            SavedFilter.objects.filter(field=key)
            .order_by("-last_used")
            .values_list("value", flat=True)
        )
        pinned = [v for v in saved if v in existing]
        pinned_set = set(pinned)
        rest = [v for v in all_values if v not in pinned_set]

        filters.append({
            "key": key,
            "label": label,
            "pinned": pinned,
            "rest": rest,
            "selected": selected,
            "available": len(all_values),
            "selected_count": len(selected),
        })

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page"))

    # Preserve filters across page links.
    params = request.GET.copy()
    params.pop("page", None)
    querystring = params.urlencode()

    return render(
        request,
        "inventory/asset_list.html",
        {
            "page_obj": page,
            "total": paginator.count,
            "grand_total": Asset.objects.count(),
            "query": query,
            "filters": filters,
            "querystring": querystring,
            "latest_import": AssetImport.objects.first(),
        },
    )


def asset_detail(request, pk):
    """Show every imported field for a single asset."""
    asset = get_object_or_404(Asset, pk=pk)
    fields = []
    for f in Asset._meta.fields:
        if f.name in ("id", "imported_at"):
            continue
        fields.append((f.verbose_name, getattr(asset, f.name)))
    return render(
        request, "inventory/asset_detail.html", {"asset": asset, "fields": fields}
    )


def _report_bundle(assets, wh):
    """The donut + category/Station ID/state breakdown data for one set of assets
    (each SNOW-linked, scoped to site ``wh``). Used to render the report's tabs
    (e.g. Active vs All states)."""
    totals = services.inventory_breakdowns(assets)

    def _decorate(d, blue, gray):
        d["url"] = blue
        d["url_pending"] = gray
        d["pending_n"] = d["total"] - d["inventoried"]

    for c in totals["categories"]:
        _decorate(
            c,
            snow_model_category_url(c["category"], wh) or snow_breakdown_url(c["category"], wh),
            snow_model_category_url(c["category"], wh, _GL_NOT_MARKED)
            or snow_breakdown_url(c["category"], wh, _GL_NOT_MARKED),
        )
    for s in totals["stations"]:
        _decorate(
            s,
            snow_station_url(s["station"], wh) or snow_breakdown_url(s["station"], wh),
            snow_station_url(s["station"], wh, _GL_NOT_MARKED)
            or snow_breakdown_url(s["station"], wh, _GL_NOT_MARKED),
        )
    for s in totals["states"]:
        _decorate(
            s,
            snow_state_url(s["state"], wh) or snow_breakdown_url(s["state"], wh),
            snow_state_url(s["state"], wh, _GL_NOT_MARKED)
            or snow_breakdown_url(s["state"], wh, _GL_NOT_MARKED),
        )

    # Retired (if present) is rendered at the bottom of the state panel with a
    # per-Substate breakdown; each substate links via substatus=<slug>.
    retired_state = next(
        (s for s in totals["states"] if (s["state"] or "").strip().lower() == "retired"),
        None,
    )
    if retired_state is not None:
        retired_subs = services.substate_breakdown(assets, "Retired")
        for sub in retired_subs:
            cond = _snow_substatus(sub["substate"])
            _decorate(
                sub,
                snow_state_url("Retired", wh, cond),
                snow_state_url("Retired", wh, cond + "^" + _GL_NOT_MARKED),
            )
        retired_state["substates"] = retired_subs

    return {
        "marked_assets": totals["inventoried"],
        "unmarked_assets": totals["pending"],
        "marked_pct": totals["marked_pct"],
        "total": totals["total_assets"],
        "category_inventoried": [c for c in totals["categories"] if c["inventoried"] > 0],
        "category_none": [c for c in totals["categories"] if c["inventoried"] == 0],
        "station_inventoried": [s for s in totals["stations"] if s["inventoried"] > 0],
        "station_none": [s for s in totals["stations"] if s["inventoried"] == 0],
        "state_inventoried": [s for s in totals["states"] if s["inventoried"] > 0 and s is not retired_state],
        "state_none": [s for s in totals["states"] if s["inventoried"] == 0 and s is not retired_state],
        "retired_state": retired_state,
    }


def station_report(request):
    """The current warehouse's assets, grouped by Station ID.

    Col 1: Station ID — links to all its assets in ServiceNow.
    Col 2: one row per unique Name in the group — links to assets matching that
           Station ID AND Name, with the device count.
    Col 3: the assets, sorted by category then display name, each with a link.
    Rows ordered: Station IDs carrying the site's own prefix first, blank
    Station IDs last — so for WH1 the "WH1|…" areas lead, and for WH2 the
    "WH2|…" ones do.
    """
    wh = warehouse_mod.current_warehouse(request)
    # This site's assets with the 202606 marker (shared with snapshot save).
    assets = services.warehouse_marked_assets(wh)
    groups = {}
    for a in assets:
        a.snow_url = snow_asset_url(a.asset_tag)
        groups.setdefault((a.station_id or "").strip(), []).append(a)

    def tags_of(items):
        return [x.asset_tag for x in items if (x.asset_tag or "").strip()]

    rows = []
    for station_id, items in groups.items():
        # A station "has the marker" when any of its assets is GL 202606.
        has_marker = any(getattr(a, "inventory_marked", False) for a in items)
        items_sorted = sorted(
            items,
            key=lambda x: ((x.model_category or "").lower(), (x.display_name or "").lower()),
        )
        by_name = {}
        for x in items:
            by_name.setdefault((x.name or "").strip(), []).append(x)
        name_rows = []
        for nm in sorted(by_name, key=lambda s: s.lower()):
            sub = sorted(
                by_name[nm],
                key=lambda x: ((x.model_category or "").lower(), (x.display_name or "").lower()),
            )
            name_rows.append({
                "name": nm or "(no name)",
                "count": len(sub),
                "snow_url": snow_list_url(tags_of(sub)),
                "tags": tags_of(sub),  # for the multi-select SNOW button
                "assets": sub,  # devices for this name (nested into col 3)
            })
        rows.append({
            "station_id": station_id or "(no Station ID)",
            "station_id_raw": station_id,
            "count": len(items),
            "distinct_names": len([n for n in by_name if n]),
            "snow_url": snow_list_url(tags_of(items)),
            "tags": tags_of(items),  # for the multi-select SNOW button
            "name_rows": name_rows,
            "marked": has_marker,
        })

    # Areas carrying this site's own Station ID prefix lead the table; untagged
    # ones sink to the bottom. Lower-cased prefixes so "wh1|it" sorts with
    # "WH1|IT" (the source data has both).
    site_prefixes = tuple(f"{p.lower()}|" for p in wh.prefixes) if wh else ()

    def order_key(r):
        sid = r["station_id_raw"]
        if not sid:
            band = 2                       # no Station ID -> bottom
        elif sid.lower().startswith(site_prefixes):
            band = 0                       # this site's own areas -> top
        else:
            band = 1
        return (band, sid.lower())

    rows.sort(key=order_key)

    # Two report tabs share the same donut + breakdown layout: the first excludes
    # Missing/Retired devices (the "active" fleet); the second is every state.
    active_assets = [
        a for a in assets
        if (a.state or "").strip().lower() not in ("missing", "retired")
    ]
    bundle_active = _report_bundle(active_assets, wh)
    bundle_all = _report_bundle(assets, wh)

    # How the site's scope splits: assets carrying its Station ID prefix vs. ones
    # that are only in scope because of their Location (i.e. untagged). That
    # second number is the retagging backlog, which is the whole point of showing
    # it — it replaced the old fixed "CPT: n · Annex: n" pair.
    tagged = sum(1 for a in assets
                 if (a.station_id or "").strip().lower().startswith(site_prefixes))

    return render(
        request,
        "inventory/station_report.html",
        {
            "rows": rows,
            "latest_import": AssetImport.objects.first(),
            "total_assets": len(assets),
            "tagged_count": tagged,
            "untagged_count": len(assets) - tagged,
            "unique_stations": len([k for k in groups if k]),
            "bundle_active": bundle_active,
            "bundle_all": bundle_all,
            "cat_state_matrix": _decorate_cat_state_matrix(
                services.category_state_matrix(assets), wh
            ),
            "snapshot_count": ReportSnapshot.objects.filter(warehouse=wh).count(),
            "snow_asset_export_url": SNOW_ASSET_EXPORT_URL,
        },
    )


def _soti_url(asset_tag):
    """SOTI MobiControl device search URL for an asset tag."""
    from urllib.parse import quote
    return (
        "https://s104630.mobicontrolcloud.com/MobiControl/WebConsole/home/dashboard/"
        "devices?search=Text%20AlmostEqual%20%22" + quote(asset_tag, safe="") + "%22"
    )


def _rf_mobiles_qs(wh):
    return warehouse_mod.scoped_assets(wh).filter(
        model__icontains="MC930B"
    ).exclude(state__iexact="Retired")


_DONUT_PALETTE = [
    "#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2", "#db2777",
    "#65a30d", "#475569", "#ea580c", "#0d9488", "#9333ea", "#ca8a04", "#4f46e5",
]


def _donut(stats, total):
    """Turn [{label,count}] into a conic-gradient string + colored legend segments.
    A segment labelled "Missing" is always rendered in gray."""
    segs, acc, ci = [], 0.0, 0
    for s in stats:
        pct = (s["count"] / total * 100) if total else 0
        if s["label"] == "Missing":
            color = "#cbd5e1"
        else:
            color = _DONUT_PALETTE[ci % len(_DONUT_PALETTE)]
            ci += 1
        segs.append({
            "label": s["label"], "count": s["count"], "pct": round(pct, 1),
            "color": color, "start": round(acc, 3), "end": round(acc + pct, 3),
        })
        acc += pct
    gradient = ", ".join(f'{x["color"]} {x["start"]}% {x["end"]}%' for x in segs) or "#cbd5e1 0% 100%"
    return {"gradient": gradient, "segs": segs}


def _valid_period(value):
    """Accept only 'YYYY-MM'; otherwise fall back to the current month."""
    now = timezone.localtime()
    if value and len(value) == 7 and value[:4].isdigit() and value[5:].isdigit() and value[4] == "-":
        return value
    return now.strftime("%Y-%m")


def rf_mobiles_list(request):
    """The current warehouse's RF mobiles for a chosen month, with a per-month
    audit checkmark per row and three same-size donuts (checked-vs-missing
    overall, plus the checked split by Station ID and by State with the rest
    shown as Missing)."""
    wh = warehouse_mod.current_warehouse(request)
    qs = _rf_mobiles_qs(wh).order_by("station_id", "name")
    now = timezone.localtime()
    period = _valid_period(request.GET.get("month"))

    # Month dropdown: the 12 months of the current year.
    months = [
        {"value": f"{now.year}-{m:02d}", "label": date(now.year, m, 1).strftime("%B %Y")}
        for m in range(1, 13)
    ]

    # If a snapshot exists for this month AND this site, retrieve its data (the
    # last one for the month); otherwise use the live per-month checks.
    snapshot = (
        RfSnapshot.objects.filter(period=period, warehouse=wh)
        .order_by("-taken_at").first()
    )
    snap_checked, snap_comments = {}, {}
    if snapshot:
        for r in snapshot.rows:
            tag = (r.get("asset_tag") or "").strip()
            if not tag:
                continue
            if r.get("checked"):
                snap_checked[tag] = r.get("checked_at") or ""
            if r.get("comment"):
                snap_comments[tag] = r.get("comment")
    else:
        checked_at = dict(
            RfMobileCheck.objects.filter(period=period, asset__in=qs).values_list("asset_id", "checked_at")
        )
        comments = dict(
            RfMobileComment.objects.filter(period=period, asset__in=qs).values_list("asset_id", "text")
        )

    mobiles = []
    tags = []
    for a in qs:
        tag = (a.asset_tag or "").strip()
        if tag:
            tags.append(a.asset_tag)
        if snapshot:
            is_checked = tag in snap_checked
            ca_str = snap_checked.get(tag, "")
            comment = snap_comments.get(tag, "")
        else:
            ca = checked_at.get(a.pk)
            is_checked = ca is not None
            ca_str = ca.strftime("%Y-%m-%d %H:%M") if ca else ""
            comment = comments.get(a.pk, "")
        mobiles.append({
            "pk": a.pk,
            "asset_tag": a.asset_tag or "",
            "station_id": a.station_id or "",
            "name": a.name or "",
            "state": a.state or "",
            "display_name": a.display_name or "",
            "model_category": a.model_category or "",
            "model": a.model or "",
            "gl_account": a.gl_account or "",
            "snow_url": snow_asset_url(a.asset_tag) if tag else "",
            "soti_url": _soti_url(a.asset_tag) if tag else "",
            "checked": is_checked,
            "checked_at": ca_str,
            "comment": comment,
        })
    total = len(mobiles)
    checked = sum(1 for m in mobiles if m["checked"])
    # The donuts (overall + Station-ID/State breakdowns) are all computed live in
    # the browser from the table, so they stay in sync as rows are checked.
    return render(request, "inventory/rf_mobiles_list.html", {
        "mobiles": mobiles,
        "total": total,
        "checked": checked,
        "missing": total - checked,
        "checked_pct": round(checked / total * 100) if total else 0,
        "snow_url": snow_list_url(tags),
        "period": period,
        "period_label": date(int(period[:4]), int(period[5:]), 1).strftime("%B %Y"),
        "months": months,
        "snapshot_count": RfSnapshot.objects.count(),
        "from_snapshot": bool(snapshot),
        "snapshot_taken": snapshot.taken_at if snapshot else None,
    })


@require_POST
def rf_mobile_check(request):
    """Toggle (and persist) the audit checkmark for one RF mobile, for a month."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    asset = Asset.objects.filter(pk=body.get("asset_id")).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Asset not found"}, status=404)
    period = _valid_period(body.get("period"))
    if body.get("checked"):
        obj, created = RfMobileCheck.objects.get_or_create(asset=asset, period=period)
        when = obj.checked_at.strftime("%Y-%m-%d %H:%M")
    else:
        RfMobileCheck.objects.filter(asset=asset, period=period).delete()
        when = ""
    return JsonResponse({"ok": True, "checked": bool(body.get("checked")), "checked_at": when})


@require_POST
def rf_mobile_comment(request):
    """Save the per-month comment for one RF mobile."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    asset = Asset.objects.filter(pk=body.get("asset_id")).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Asset not found"}, status=404)
    period = _valid_period(body.get("period"))
    text = (body.get("text") or "").strip()
    if text:
        RfMobileComment.objects.update_or_create(
            asset=asset, period=period, defaults={"text": text}
        )
    else:
        RfMobileComment.objects.filter(asset=asset, period=period).delete()
    return JsonResponse({"ok": True, "text": text})


RF_EDITABLE_FIELDS = {
    "station_id", "name", "state", "display_name",
    "model_category", "model", "asset_tag", "gl_account",
}


@require_POST
def rf_asset_update(request):
    """Edit one field of a live Asset (from the RF Mobiles inline editor)."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    field = body.get("field")
    if field not in RF_EDITABLE_FIELDS:
        return JsonResponse({"ok": False, "error": "Field not editable"}, status=400)
    asset = Asset.objects.filter(pk=body.get("asset_id")).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Asset not found"}, status=404)
    setattr(asset, field, (body.get("value") or "").strip())
    asset.save(update_fields=[field])
    return JsonResponse({"ok": True, "value": getattr(asset, field) or ""})


@require_POST
def rf_asset_add(request):
    """Create a new RF-mobile Asset (MC930B) for inline filling-in.

    The new row has to land inside the site the person is looking at, or it
    vanishes the moment the page reloads. It used to hard-code
    ``location="CPT"`` for exactly that reason; it now seeds the site's own
    Station ID prefix instead (``"WH1|"`` / ``"WH2|"``), which both scopes the
    row correctly and leaves the area half visibly blank for them to complete
    in the inline editor.
    """
    period = _valid_period(request.POST.get("period"))
    wh = warehouse_mod.current_warehouse(request)
    Asset.objects.create(
        model="MC930B",
        state="In use",
        station_id=f"{wh.prefixes[0]}|" if wh else "",
    )
    messages.success(request, "New RF mobile added — double-click its cells to fill in the details.")
    return redirect(f"/rf-mobiles/?month={period}")


@require_POST
def rf_snapshot_save(request):
    """Record an RF-mobile audit snapshot for the chosen month — donut data plus
    the full table. One snapshot per month PER SITE: re-saving replaces it."""
    period = _valid_period(request.POST.get("period"))
    wh = warehouse_mod.current_warehouse(request)
    qs = _rf_mobiles_qs(wh).order_by("station_id", "name")
    checked_at = dict(
        RfMobileCheck.objects.filter(period=period, asset__in=qs).values_list("asset_id", "checked_at")
    )
    comments = dict(
        RfMobileComment.objects.filter(period=period, asset__in=qs).values_list("asset_id", "text")
    )
    station_checked, state_checked, rows = {}, {}, []
    for a in qs:
        ca = checked_at.get(a.pk)
        if ca is not None:
            sid = (a.station_id or "").strip() or "(no Station ID)"
            station_checked[sid] = station_checked.get(sid, 0) + 1
            st = (a.state or "").strip() or "(no state)"
            state_checked[st] = state_checked.get(st, 0) + 1
        rows.append({
            "station_id": a.station_id or "", "name": a.name or "", "state": a.state or "",
            "display_name": a.display_name or "", "model_category": a.model_category or "",
            "model": a.model or "", "asset_tag": a.asset_tag or "", "gl_account": a.gl_account or "",
            "checked": ca is not None,
            "checked_at": ca.strftime("%Y-%m-%d %H:%M") if ca else "",
            "comment": comments.get(a.pk, ""),
        })
    total = len(rows)
    checked = len(checked_at)
    RfSnapshot.objects.update_or_create(
        period=period,
        warehouse=wh,
        defaults={
            "total": total,
            "checked": checked,
            "missing": total - checked,
            "checked_pct": round(checked / total * 100) if total else 0,
            "stations": sorted(({"label": k, "count": v} for k, v in station_checked.items()), key=lambda d: -d["count"]),
            "states": sorted(({"label": k, "count": v} for k, v in state_checked.items()), key=lambda d: -d["count"]),
            "rows": rows,
        },
    )
    messages.success(request, f"RF snapshot saved for {period} (replaces any earlier one).")
    return redirect("rf_snapshots_list")


def rf_snapshots_list(request):
    """History of RF-mobile audit snapshots for the current site."""
    wh = warehouse_mod.current_warehouse(request)
    return render(request, "inventory/rf_snapshots_list.html", {
        "snapshots": RfSnapshot.objects.filter(warehouse=wh),
    })


@require_POST
def station_snapshot_save(request):
    """Record the current site's Station report totals + breakdowns, timestamped."""
    wh = warehouse_mod.current_warehouse(request)
    assets = services.warehouse_marked_assets(wh)
    data = services.inventory_breakdowns(assets)
    latest = AssetImport.objects.first()
    ReportSnapshot.objects.create(
        warehouse=wh,
        source_filename=(latest.filename if latest else ""),
        total_assets=data["total_assets"],
        inventoried=data["inventoried"],
        pending=data["pending"],
        marked_pct=data["marked_pct"],
        categories=data["categories"],
        stations=data["stations"],
        states=data["states"],
    )
    messages.success(request, "Snapshot saved.")
    return redirect("snapshots_list")


def snapshots_list(request):
    """History of recorded Station report snapshots for the current site."""
    wh = warehouse_mod.current_warehouse(request)
    return render(
        request,
        "inventory/snapshots_list.html",
        {"snapshots": ReportSnapshot.objects.filter(warehouse=wh)},
    )


@require_POST
def snapshot_edit_date(request, pk):
    """Admin (unlocked) manual edit of a snapshot's recorded date/time."""
    get_object_or_404(ReportSnapshot, pk=pk)
    dt = parse_datetime((request.POST.get("taken_at") or "").strip())
    if dt is None:
        messages.error(request, "Could not read that date.")
        return redirect("snapshots_list")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_default_timezone())
    # taken_at is auto_now_add; .update() bypasses it so the date can be edited.
    ReportSnapshot.objects.filter(pk=pk).update(taken_at=dt)
    messages.success(request, "Snapshot date updated.")
    return redirect("snapshots_list")


def _printers_in_use_qs(wh):
    """One site's printers in the 'In use' state — the Printers page's scope.

    (The Impressions page builds its own, narrower scope inline; see
    ``impressions_list``.)"""
    return warehouse_mod.scoped_assets(wh).filter(
        Q(model_category__icontains="printer") & Q(state__iexact="In use")
    )


# Category words that mean "not a laser/toner printer". "Badge" is in here because
# the fleet's one "Badge Printer" is a thermal label unit.
_LABEL_CATEGORY_HINTS = ("label", "thermal", "badge")


def _printer_kind(model_category, manufacturer):
    """Classify a printer as ``"label"`` or ``"laser"`` for the new-queue form's
    radio default.

    Model category is the primary signal (it's what the record is "about"), but it
    alone is wrong for ~7% of the current fleet: a "Badge Printer" and four plain
    "Printer" records are really Sato / Zebra / Avery label units. So fall back to
    the manufacturer, which is the signal the supply-level code already trusts.
    Laser is the default for anything still unclear — the radio is user-overridable.
    """
    cat = (model_category or "").lower()
    if any(h in cat for h in _LABEL_CATEGORY_HINTS):
        return "label"
    if printer_supplies.is_label_make(manufacturer):
        return "label"
    return "laser"


def printers_list(request):
    """The current site's printers, with the structured Comments sub-fields shown
    as editable columns (IP, FMS, FMS 2, DMS, Service ID)."""
    wh = warehouse_mod.current_warehouse(request)
    # Areas stay grouped/ordered by station_id; within an area, by category then model.
    qs = _printers_in_use_qs(wh).order_by("station_id", "model_category", "model", "name")
    groups = {}  # ordered by the queryset's station_id ordering
    for a in qs:
        d = services.comment_fields_map(a.comments)
        ip = d.get("ip", "")
        # The level probe needs a real IP; comments store it inconsistently, so
        # fall back to a regex scan when the structured "IP:" field is absent.
        probe_ip = ip or services.extract_ip(a.comments)
        sid = (a.station_id or "").strip() or "(no Station ID)"
        groups.setdefault(sid, []).append({
            "pk": a.pk,
            "asset_tag": a.asset_tag or "",
            "name": a.name or "",
            "manufacturer": a.manufacturer or "",
            "model_category": a.model_category or "",
            "model": a.model or "",
            "comments": a.comments or "",
            "ip": ip,
            "ip_href": ip if ip.lower().startswith(("http://", "https://")) else ("http://" + ip if ip else ""),
            "snow_url": snow_asset_url(a.asset_tag) if (a.asset_tag or "").strip() else "",
            "fms": d.get("fms", ""),
            "fms2": d.get("fms 2", ""),
            "dms": d.get("dms", ""),
            "service_id": d.get("service id", ""),
            "fedex": d.get("fedex", ""),
            "ups": d.get("ups", ""),
            "windows_queue": d.get("windows queue", ""),
            # Preselects the new-queue modal's Label/Laser radio.
            "kind": _printer_kind(a.model_category, a.manufacturer),
            # Laser/toner makes can be polled for supplies; label printers cannot.
            "supports_levels": bool(
                probe_ip and printer_supplies.resolve_strategy(a.manufacturer, a.model)
            ),
        })
    printer_groups = [
        {"station_id": sid, "count": len(items), "printers": items}
        for sid, items in groups.items()
    ]
    total = sum(g["count"] for g in printer_groups)
    return render(
        request,
        "inventory/printers_list.html",
        {"printer_groups": printer_groups, "total": total},
    )


def _handle_intune_upload(request):
    """Replace the IntuneDevice table from an uploaded .zip containing one
    Intune "All devices" CSV export.

    Saves the zip's CSV member (removing any previous one), truncates and
    re-imports via the same management command the CLI path uses, and
    records the new version's metadata. Mirrors ``_handle_asset_upload``.
    """
    f = request.FILES.get("source")
    if not f:
        messages.error(request, "Choose a .zip file to upload.")
        return redirect("intune_list")
    if not f.name.lower().endswith(".zip"):
        messages.error(request, "Please upload a .zip file.")
        return redirect("intune_list")

    try:
        with zipfile.ZipFile(f) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv") and not n.endswith("/")]
            if not csv_names:
                messages.error(request, "The zip file doesn't contain a .csv file.")
                return redirect("intune_list")
            if len(csv_names) > 1:
                messages.error(
                    request,
                    f"The zip file contains multiple .csv files ({', '.join(csv_names)}) — expected exactly one.",
                )
                return redirect("intune_list")
            csv_bytes = zf.read(csv_names[0])
    except zipfile.BadZipFile:
        messages.error(request, "That file isn't a valid .zip archive.")
        return redirect("intune_list")

    # Save into media/intune_sources/, removing the previous version first.
    dest_dir = os.path.join(settings.MEDIA_ROOT, "intune_sources")
    os.makedirs(dest_dir, exist_ok=True)
    for old in os.listdir(dest_dir):
        try:
            os.remove(os.path.join(dest_dir, old))
        except OSError:
            pass
    dest_path = os.path.join(dest_dir, os.path.basename(csv_names[0]))
    with open(dest_path, "wb") as fh:
        fh.write(csv_bytes)

    # Replace all rows (truncate) and import.
    try:
        call_command("import_intune_csv", dest_path, truncate=True)
    except Exception as exc:
        messages.error(request, f"Import failed: {exc}")
        return redirect("intune_list")

    count = IntuneDevice.objects.count()
    IntuneImport.objects.all().delete()   # keep only the current version
    IntuneImport.objects.create(filename=f.name, row_count=count)
    messages.success(request, f"Imported {count} Intune devices from “{f.name}” (previous data replaced).")
    return redirect("intune_list")


def intune_list(request):
    """The current site's computers, joined against the imported Intune export.

    An Asset matches an IntuneDevice when the asset's serial number appears
    anywhere inside the Intune device name (e.g. serial "18WBSW3" inside
    device name "OOS-18WBSW3") — that's how the export embeds it. Every
    qualifying asset is shown (not just the ones with a match) so gaps are
    visible; grouped by State (each group collapsed until clicked, showing
    its own asset count and how many of those matched an Intune device).
    """
    if request.method == "POST":
        return _handle_intune_upload(request)

    wh = warehouse_mod.current_warehouse(request)
    assets = list(
        warehouse_mod.scoped_assets(wh).filter(
            Q(model_category__iexact="Computer") | Q(model_category__iexact="Personal Computer")
        ).exclude(model__icontains="Mac").order_by("state")
    )
    intune_devices = list(IntuneDevice.objects.exclude(device_name__isnull=True).exclude(device_name=""))
    # Keyed by normalized asset tag (not an Asset FK) so the assignment
    # survives a later ServiceNow re-import — see IntuneFixAssignment's
    # docstring in models.py.
    fix_by_tag = {
        f.asset_tag: f
        for f in IntuneFixAssignment.objects.select_related("person")
    }

    rows = []
    matched_count = 0
    fix_counts = Counter()
    for a in assets:
        serial = (a.serial_number or "").strip()
        match = None
        if serial:
            serial_lower = serial.lower()
            match = next(
                (d for d in intune_devices if serial_lower in d.device_name.lower()),
                None,
            )
        if match:
            matched_count += 1
        tag = (a.asset_tag or "").strip()
        tag_norm = normalize_tag(tag)
        fix = fix_by_tag.get(tag_norm) if tag_norm else None
        fix_person = fix.person if fix else None
        if fix_person:
            fix_counts[fix_person.id] += 1
        rows.append({
            "asset_id": a.pk,
            "asset_tag": a.asset_tag or "",
            "snow_url": snow_asset_url(tag) if tag else "",
            "serial_number": serial,
            "station_id": a.station_id or "",
            "name": a.name or "",
            "state": a.state or "(no State)",
            "location": a.location or "",
            "model": a.model or "",
            "assigned_to": a.assigned_to or "",
            "inventory_marked": "202606" in (a.gl_account or ""),
            "match": match,
            "intune_url": intune_device_url(match.device_id) if match else "",
            "fix_person_id": fix_person.id if fix_person else "",
            "fix_initials": fix_person.initials if fix_person else "",
            "fix_color": fix_person.color if fix_person else "",
            "fix_text_color": fix_person.text_color if fix_person else "",
            "fix_fixed": fix.fixed if fix else False,
            "fix_fixed_at": timezone.localtime(fix.fixed_at).strftime("%Y-%m-%d %H:%M") if fix and fix.fixed_at else "",
        })

    # Flag a Primary user cell red when that name appears on more than one row
    # in the whole table (someone with more than one device in this list).
    user_counts = Counter(
        r["match"].primary_user_display_name.strip()
        for r in rows
        if r["match"] and (r["match"].primary_user_display_name or "").strip()
    )
    for r in rows:
        name = r["match"].primary_user_display_name.strip() if r["match"] and r["match"].primary_user_display_name else ""
        r["duplicate_primary_user"] = bool(name) and user_counts[name] > 1

    groups = {}  # ordered by the queryset's state ordering
    for r in rows:
        groups.setdefault(r["state"], []).append(r)
    state_groups = []
    for state, items in groups.items():
        # "In use" is where an unmatched device is the actionable gap, so those go
        # first there, then sub-sorted compliant-before-not, then by asset tag; every
        # other state is the opposite (matched ones lead, no compliance sub-sort).
        if state.lower() == "in use":
            def _in_use_key(r):
                matched = r["match"] is not None
                compliant = matched and r["match"].compliance == "Compliant"
                return (matched, not compliant, r["asset_tag"])
            items.sort(key=_in_use_key)
        else:
            items.sort(key=lambda r: r["match"] is None)
        state_groups.append({
            "state": state,
            "rows": items,
            "total": len(items),
            "matched": sum(1 for r in items if r["match"]),
        })

    fix_people = list(FixPerson.objects.all())
    for p in fix_people:
        p.count = fix_counts.get(p.id, 0)

    # Fixers summary table (page footer): totals across ALL of a person's
    # assignments, not just this page's currently-filtered asset set — unlike
    # fix_people/p.count above (which stays scoped to this page's rows, for
    # the picker modal), this is meant to answer "how is each fixer doing
    # overall," which should keep counting an assignment even if that asset
    # later drops out of this page's site/Computer filter. Note this total is
    # deliberately NOT warehouse-scoped either — a fixer's workload is theirs
    # wherever the asset sits.
    fixer_summary = list(
        FixPerson.objects.annotate(
            assigned_total=Count("assignments", distinct=True),
            fixed_total=Count("assignments", filter=Q(assignments__fixed=True), distinct=True),
        ).order_by("name")
    )
    for p in fixer_summary:
        p.remaining = p.assigned_total - p.fixed_total

    return render(
        request,
        "inventory/intune_list.html",
        {
            "state_groups": state_groups,
            "total": len(assets),
            "matched_count": matched_count,
            "fix_people": fix_people,
            "fixer_summary": fixer_summary,
            "latest_import": IntuneImport.objects.first(),
        },
    )


@require_POST
def intune_fix_save(request):
    """Assign (or clear) which FixPerson is fixing an asset's Intune row.

    Keyed by normalized asset tag rather than an Asset FK/PK so the
    assignment survives a later ServiceNow re-import — see
    IntuneFixAssignment's docstring in models.py.
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    asset_tag = normalize_tag(body.get("asset_tag"))
    if not asset_tag:
        return JsonResponse({"ok": False, "error": "Asset tag required"}, status=400)

    person_id = body.get("person_id")
    if not person_id:
        IntuneFixAssignment.objects.filter(asset_tag=asset_tag).delete()
        return JsonResponse({"ok": True, "cleared": True})

    person = FixPerson.objects.filter(pk=person_id).first()
    if not person:
        return JsonResponse({"ok": False, "error": "Person not found"}, status=404)

    # A (re)assignment always starts unfixed — "fixed" describes what the
    # newly-assigned person has done, not whatever a previous assignee left behind.
    IntuneFixAssignment.objects.update_or_create(
        asset_tag=asset_tag,
        defaults={
            "person": person,
            "serial_number": normalize_tag(body.get("serial_number")),
            "fixed": False,
            "fixed_at": None,
        },
    )
    return JsonResponse({
        "ok": True,
        "initials": person.initials,
        "name": person.name,
        "color": person.color,
        "text_color": person.text_color,
        "fixed": False,
        "fixed_at": "",
    })


@require_POST
def intune_fix_mark(request):
    """Toggle an assignment's fixed status.

    Gated by the assigned person's initials as a lightweight confirmation
    (not real auth — same spirit as the rest of the Fix feature, which is
    exempt from the app's write-access code because it's a who's-on-it tag,
    not a data correction).
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    asset_tag = normalize_tag(body.get("asset_tag"))
    if not asset_tag:
        return JsonResponse({"ok": False, "error": "Asset tag required"}, status=400)

    assignment = IntuneFixAssignment.objects.filter(asset_tag=asset_tag).select_related("person").first()
    if not assignment:
        return JsonResponse({"ok": False, "error": "No one is assigned to this asset yet."}, status=404)

    entered = (body.get("initials") or "").strip().upper()
    if not entered or entered != (assignment.person.initials or "").strip().upper():
        return JsonResponse({"ok": False, "error": "Incorrect initials."}, status=403)

    assignment.fixed = not assignment.fixed
    assignment.fixed_at = timezone.now() if assignment.fixed else None
    assignment.save(update_fields=["fixed", "fixed_at"])
    return JsonResponse({
        "ok": True,
        "fixed": assignment.fixed,
        "fixed_at": timezone.localtime(assignment.fixed_at).strftime("%Y-%m-%d %H:%M") if assignment.fixed_at else "",
    })


@require_POST
def intune_fix_person_add(request):
    """Add a new person to the shared Fix picker roster."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    name = (body.get("name") or "").strip()
    initials = (body.get("initials") or "").strip()
    if not name or not initials:
        return JsonResponse({"ok": False, "error": "Name and initials are required."}, status=400)

    person = FixPerson.objects.create(name=name, initials=initials)
    return JsonResponse({
        "ok": True,
        "id": person.id,
        "name": person.name,
        "initials": person.initials,
        "color": person.color,
        "text_color": person.text_color,
    })


@require_POST
def printer_comments_save(request):
    """Save edited printer sub-fields as a pending Comments correction.

    Rebuilds the Comments string from the sub-fields (preserving any extra keys
    already present), and merges it into the asset's correction without clobbering
    other fields the auditor may have changed.
    """
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    asset = Asset.objects.filter(pk=body.get("asset_id")).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Asset not found"}, status=404)

    edits = body.get("fields") or {}
    known_lower = {k.lower() for k in services.PRINTER_COMMENT_KEYS}
    extras = [
        (k, v) for k, v in services.parse_comment_fields(asset.comments)
        if k.lower() not in known_lower
    ]
    new_pairs = [(k, (edits.get(k) or "").strip()) for k in services.PRINTER_COMMENT_KEYS]
    compiled = services.compile_comment_fields(new_pairs + extras)

    correction = getattr(asset, "correction", None)
    expected = dict(correction.expected) if correction and isinstance(correction.expected, dict) else {}
    expected["comments"] = compiled
    current, changes = _compute_changes(asset, expected)
    Correction.objects.update_or_create(
        asset=asset,
        defaults={
            "asset_tag": asset.asset_tag or "",
            "current": current,
            "expected": expected,
            "changes": changes if changes else "",
            "status": Correction.STATUS_PENDING,
            "updated_by": request.user if request.user.is_authenticated else None,
        },
    )
    return JsonResponse({
        "ok": True,
        "compiled": compiled,
        "pending_count": _scoped_corrections(request).filter(
            status=Correction.STATUS_PENDING
        ).count(),
    })


def printer_supplies_check(request, pk):
    """Read a single laser printer's toner/drum levels over SNMP (on demand).

    Read-only, so no write gate. Returns JSON the printers page renders in a modal.
    """
    asset = Asset.objects.filter(pk=pk).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Printer not found"}, status=404)

    ip = services.comment_fields_map(asset.comments).get("ip", "") or services.extract_ip(asset.comments)
    if not ip:
        return JsonResponse({"ok": False, "error": "No IP address found in this printer's comments."})

    strategy = printer_supplies.resolve_strategy(asset.manufacturer, asset.model)
    if strategy is None:
        return JsonResponse({"ok": False, "error": "Supply levels aren't supported for this printer model."})

    try:
        supplies = strategy.get_supplies(ip)
    except printer_supplies.SnmpError as e:
        return JsonResponse({"ok": False, "ip": ip, "error": str(e)})

    return JsonResponse({
        "ok": True,
        "ip": ip,
        "model": asset.model or "",
        "supplies": supplies,
    })


# Longest text we'll encode. A QR code tops out around 2,900 alphanumeric chars
# at the lowest error correction, and the bigger the payload the denser (and
# harder to scan) the result — so refuse rather than emit an unscannable code.
QR_MAX_CHARS = 1200


def qr_svg(request):
    """Render arbitrary text as a QR code SVG (``/qr/?text=...``).

    Read-only and side-effect free, so no write gate. Returns SVG (not PNG) so
    it scales crisply and needs no image library — ``segno`` writes SVG with
    pure Python, keeping the project's "no compiled dependencies" property (see
    the .venv note in CLAUDE.md §2).

    Built for the printers page's "new queue" modal, which re-fetches this as the
    form changes, but it's deliberately generic — any page can point an <img> at it.
    """
    # Imported HERE, not at module scope, on purpose: segno is a new requirement
    # and the share's .venv may not have it yet. A top-level import would take
    # every page in the app down with it; this way only the QR endpoint degrades,
    # and with a readable message instead of a traceback.
    try:
        import segno
    except ImportError:
        return HttpResponse(
            "QR support needs the 'segno' package: pip install segno (see requirements.txt).",
            status=503,
            content_type="text/plain",
        )

    text = request.GET.get("text", "")
    if not text.strip():
        return HttpResponse("Missing ?text=", status=400, content_type="text/plain")
    if len(text) > QR_MAX_CHARS:
        return HttpResponse(
            f"Text too long for a scannable QR code ({len(text)} chars, max {QR_MAX_CHARS}).",
            status=400,
            content_type="text/plain",
        )

    # scale/border are cosmetic knobs the caller may tune; error correction stays
    # at "M" (~15% recovery) — a reasonable default for a screen-displayed code
    # that may be scanned at an angle, without inflating the module count.
    try:
        scale = min(max(int(request.GET.get("scale", 4)), 1), 20)
        border = min(max(int(request.GET.get("border", 2)), 0), 10)
    except ValueError:
        return HttpResponse("scale/border must be integers", status=400, content_type="text/plain")

    # make_qr (not make) so short payloads never come back as a Micro QR, which
    # many handheld scanners won't read.
    qr = segno.make_qr(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=scale, border=border, xmldecl=False, svgclass=None, lineclass=None)

    resp = HttpResponse(buf.getvalue(), content_type="image/svg+xml")
    # Same text always yields the same image, so let the browser cache it — this
    # is what keeps rapid form edits from re-rendering identical codes.
    resp["Cache-Control"] = "public, max-age=86400"
    return resp


def impressions_list(request):
    """On-demand total-impression reads for LEXMARK / Xerox printers (the makes
    that report impressions over Printer-MIB) at the current site. The browser
    polls each in-use printer via ``printer_impressions_check`` and tallies a
    grand total; a second (collapsed) table lists the not-in-use ones for manual
    entry / last-recorded prefill. Rows are colour-coded by location.

    This page used to span Main / Annex AND WH2 in one combined total — it
    predated warehouses and was the only page that deliberately crossed sites.
    It is now scoped like every other page, so WH1 and WH2 each report their own
    monthly figure. Consequence to know: snapshots saved BEFORE the split carry
    no warehouse and mix both sites, so they are listed separately rather than
    being silently attributed to either one.
    """
    wh = warehouse_mod.current_warehouse(request)
    mfr_q = Q(manufacturer__icontains="lexmark") | Q(manufacturer__icontains="xerox")
    # Exclude records with no asset tag (they'd render as "—" and can't be tracked).
    printer_scope = (
        Q(model_category__icontains="printer") & mfr_q
        & ~Q(asset_tag__isnull=True) & ~Q(asset_tag__exact="")
    )
    scoped = warehouse_mod.scoped_assets(wh)

    def loc_class(loc):
        return "loc-lakeside" if "lakeside" in (loc or "").lower() else "loc-mainannex"

    # Newest-first {asset_tag: last recorded reading} from saved snapshots. Used to
    # prefill BOTH tables with the last saved value (shown "stale" until a live read).
    # Pre-split snapshots (warehouse=None) are included on purpose: they are the
    # only history some of these printers have, and the prefill is keyed by asset
    # tag, so a WH2 printer only ever picks up its own past reading.
    last_by_tag = {}
    for snap in ImpressionSnapshot.objects.filter(
        Q(warehouse=wh) | Q(warehouse__isnull=True)
    ):  # ordered -taken_at (newest first)
        taken = timezone.localtime(snap.taken_at).strftime("%Y-%m-%d")
        for r in snap.readings:
            tag = (r.get("asset_tag") or "").strip()
            if not tag or tag in last_by_tag:
                continue
            try:
                last_by_tag[tag] = {"value": int(r.get("impressions")), "taken": taken}
            except (TypeError, ValueError):
                pass

    qs = (
        scoped.filter(printer_scope & Q(state__iexact="In use"))
        .order_by("location", "manufacturer", "serial_number")
    )
    printers = []
    for a in qs:
        cmap = services.comment_fields_map(a.comments)
        ip = cmap.get("ip", "") or services.extract_ip(a.comments)
        lr = last_by_tag.get((a.asset_tag or "").strip()) if (a.asset_tag or "").strip() else None
        printers.append({
            "pk": a.pk,
            "asset_tag": a.asset_tag or "",
            "serial_number": a.serial_number or "",
            # Service ID is a structured sub-field parsed out of the Comments string.
            "service_id": cmap.get("service id", ""),
            "area": (a.station_id or "").strip() or "(no Station ID)",
            "location": a.location or "",
            "loc_class": loc_class(a.location),
            "name": a.name or "",
            "manufacturer": a.manufacturer or "",
            "model": a.model or "",
            "ip": ip,
            "ip_href": ip if ip.lower().startswith(("http://", "https://")) else ("http://" + ip if ip else ""),
            "snow_url": snow_asset_url(a.asset_tag) if (a.asset_tag or "").strip() else "",
            # Last saved value, shown stale on load until a live read overwrites it.
            "last_value": lr["value"] if lr else None,
            "last_value_fmt": f"{lr['value']:,}" if lr else "",
            "last_taken": lr["taken"] if lr else "",
        })
    snapshots = []
    for s in ImpressionSnapshot.objects.filter(Q(warehouse=wh) | Q(warehouse__isnull=True)):
        local = timezone.localtime(s.taken_at)
        snapshots.append({
            "pk": s.pk,
            "taken_at": local.strftime("%Y-%m-%d %H:%M"),
            "month_label": local.strftime("%B %Y"),
            "period": s.period,
            "total": s.total_impressions,
            "total_fmt": f"{s.total_impressions:,}",
            "count": s.printer_count,
            # Pre-split snapshots span both sites; the list labels them so nobody
            # reads one as this site's figure.
            "site_label": s.warehouse.code if s.warehouse_id else "all sites (pre-split)",
            "cross_site": not s.warehouse_id,
            "readings": [
                {
                    **r,
                    "impressions_fmt": f"{int(r.get('impressions', 0)):,}",
                    "make_model": (f"{r.get('manufacturer', '')} {r.get('model', '')}").strip(),
                }
                for r in s.readings
            ],
        })
    # Below the in-use table: a collapsed list of this site's NOT-in-use laser
    # (Xerox / LEXMARK) printers, entered manually; prefilled with the last
    # recorded value (from ``last_by_tag`` above) when the printer has a valid IP.
    offline_qs = (
        scoped.filter(printer_scope)
        .exclude(state__iexact="In use")
        .order_by("location", "manufacturer", "serial_number")
    )
    offline_printers = []
    for a in offline_qs:
        cmap = services.comment_fields_map(a.comments)
        ip = cmap.get("ip", "") or services.extract_ip(a.comments)
        tag = a.asset_tag or ""
        lr = last_by_tag.get(tag.strip()) if tag.strip() else None
        offline_printers.append({
            "pk": a.pk,
            "asset_tag": tag,
            "serial_number": a.serial_number or "",
            "service_id": cmap.get("service id", ""),
            "area": (a.station_id or "").strip() or "(no Station ID)",
            "location": a.location or "",
            "loc_class": loc_class(a.location),
            "manufacturer": a.manufacturer or "",
            "model": a.model or "",
            "state": a.state or "",
            "ip": ip,
            "ip_href": ip if ip.lower().startswith(("http://", "https://")) else ("http://" + ip if ip else ""),
            "snow_url": snow_asset_url(tag) if tag.strip() else "",
            "last_value": lr["value"] if lr else None,
            "last_value_fmt": f"{lr['value']:,}" if lr else "",
            "last_taken": lr["taken"] if lr else "",
            # Prefill only when there's a valid IP AND a recorded value to retrieve.
            "prefill": lr["value"] if (lr and ip) else "",
        })
    return render(
        request,
        "inventory/impressions_list.html",
        {
            "printers": printers,
            "total": len(printers),
            "snapshots": snapshots,
            "offline_printers": offline_printers,
        },
    )


@require_POST
def impressions_snapshot_save(request):
    """Save the impression readings the browser just gathered as a snapshot.

    Readings are collected client-side (each printer is polled over SNMP), so
    they're POSTed here as JSON rather than recomputed. Write-gated via the
    middleware. The recorded ``period`` (month) is the current month."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    clean, total = [], 0
    for r in body.get("readings") or []:
        try:
            imp = int(r.get("impressions"))
        except (TypeError, ValueError):
            continue
        clean.append({
            "asset_tag": str(r.get("asset_tag") or ""),
            "serial_number": str(r.get("serial_number") or ""),
            "service_id": str(r.get("service_id") or ""),
            "area": str(r.get("area") or ""),
            "manufacturer": str(r.get("manufacturer") or ""),
            "model": str(r.get("model") or ""),
            "ip": str(r.get("ip") or ""),
            "impressions": imp,
        })
        total += imp
    if not clean:
        return JsonResponse({"ok": False, "error": "No readings to save — read impressions first."})
    wh = warehouse_mod.current_warehouse(request)
    snap = ImpressionSnapshot.objects.create(
        warehouse=wh,
        period=timezone.localtime(timezone.now()).strftime("%Y-%m"),
        total_impressions=total,
        printer_count=len(clean),
        readings=clean,
    )
    return JsonResponse({
        "ok": True,
        "pk": snap.pk,
        "count": ImpressionSnapshot.objects.filter(
            Q(warehouse=wh) | Q(warehouse__isnull=True)
        ).count(),
    })


@require_POST
def impression_snapshot_delete(request, pk):
    """Delete one impressions snapshot (admin/unlocked only, via the write gate)."""
    ImpressionSnapshot.objects.filter(pk=pk).delete()
    messages.success(request, "Impressions snapshot deleted.")
    return redirect("impressions_list")


def printer_impressions_check(request, pk):
    """Read one printer's total lifetime impressions over SNMP (on demand).

    Read-only, so no write gate. Returns JSON the Impressions page tallies.
    """
    asset = Asset.objects.filter(pk=pk).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Printer not found"}, status=404)

    ip = services.comment_fields_map(asset.comments).get("ip", "") or services.extract_ip(asset.comments)
    if not ip:
        return JsonResponse({"ok": False, "error": "No IP address found in this printer's comments."})

    try:
        impressions = printer_supplies.get_total_impressions(ip)
    except printer_supplies.SnmpError as e:
        return JsonResponse({"ok": False, "ip": ip, "error": str(e)})

    return JsonResponse({
        "ok": True,
        "ip": ip,
        "model": asset.model or "",
        "impressions": impressions,
    })


def snapshot_export(request, pk):
    """Download one snapshot's totals + breakdowns as CSV."""
    snap = get_object_or_404(ReportSnapshot, pk=pk)
    response = HttpResponse(content_type="text/csv")
    stamp = snap.taken_at.strftime("%Y-%m-%d %H%M")
    response["Content-Disposition"] = f'attachment; filename="snapshot {stamp}.csv"'
    writer = csv.writer(response)
    writer.writerow(["taken_at", snap.taken_at.strftime("%Y-%m-%d %H:%M:%S")])
    writer.writerow(["source_file", snap.source_filename])
    writer.writerow(["total_assets", snap.total_assets])
    writer.writerow(["inventoried", snap.inventoried])
    writer.writerow(["pending", snap.pending])
    writer.writerow(["inventoried_pct", snap.marked_pct])
    writer.writerow([])
    writer.writerow(["dimension", "name", "inventoried", "total"])
    for dim, rows, key in (
        ("category", snap.categories, "category"),
        ("station", snap.stations, "station"),
        ("state", snap.states, "state"),
    ):
        for r in rows:
            writer.writerow([dim, r.get(key, ""), r.get("inventoried", 0), r.get("total", 0)])
    return response


# --- Audit workstation -----------------------------------------------------

def _value_str(asset, field):
    value = getattr(asset, field)
    return "" if value is None else str(value).strip()


def _norm(value):
    return (value or "").strip().casefold()


def _compute_changes(asset, expected):
    """Return (current, changes) for the audit fields.

    ``current`` = current value of every audit field.
    ``changes`` = parallel dicts of only the differing fields, e.g.
        {"current": {"state": "In Use"}, "expected": {"state": "In stock"}}
    Empty ({}) when nothing differs.
    """
    current = {}
    diff_current = {}
    diff_expected = {}
    for field in ASSET_AUDIT_FIELDS:
        cur = _value_str(asset, field)
        current[field] = cur
        # Only fields the auditor actually overrode are present in ``expected``.
        if field not in expected:
            continue
        exp = (expected.get(field) or "").strip()
        # A difference includes an empty expected (clear/delete the value) as
        # long as the current value isn't already empty.
        if _norm(exp) != _norm(cur):
            diff_current[field] = cur
            diff_expected[field] = exp
    changes = {"current": diff_current, "expected": diff_expected} if diff_expected else {}
    return current, changes


def audit(request):
    """The scan-and-verify workstation page."""
    # Some fields autocomplete from more than one column: the people fields
    # ("Assigned to", "Managed by") share the combined set of names.
    OPTION_SOURCES = {
        "assigned_to": ["assigned_to", "managed_by"],
        "managed_by": ["assigned_to", "managed_by"],
    }

    # Autocomplete suggestions come from THIS SITE's assets only — the whole
    # point of the datalists is "what do the neighbouring devices say", and
    # offering WH1's Station IDs while auditing WH2 is how wrong values get
    # typed in. (The tag LOOKUP stays global; see audit_lookup.)
    wh = warehouse_mod.current_warehouse(request)
    site_assets = warehouse_mod.scoped_assets(wh)

    def distinct_values(col):
        return (
            site_assets.exclude(**{f"{col}__isnull": True})
            .exclude(**{col: ""})
            .values_list(col, flat=True)
            .distinct()
            .order_by(col)
        )

    # Build datalist options (distinct values) for each editable audit field.
    field_options = []
    for field in ASSET_AUDIT_FIELDS:
        if field == "comments":
            # Free-text; skip the (huge, useless) distinct-values datalist.
            field_options.append({"field": field, "label": audit_field_label(field), "options": []})
            continue
        cols = OPTION_SOURCES.get(field, [field])
        combined = set()
        for col in cols:
            combined.update(v.strip() for v in distinct_values(col) if v and v.strip())
        values = sorted(combined, key=str.casefold)[:10000]
        field_options.append({
            "field": field,
            "label": audit_field_label(field),
            "options": values,
        })
    # Split the fields into two halves so the template can render two tables
    # side by side (less vertical scrolling).
    mid = (len(field_options) + 1) // 2
    return render(
        request,
        "inventory/audit.html",
        {
            "identity_fields": [
                {"field": f, "label": audit_field_label(f)}
                for f in ASSET_IDENTITY_FIELDS
            ],
            "field_options": field_options,
            "field_options_left": field_options[:mid],
            "field_options_right": field_options[mid:],
            "pending_count": _scoped_corrections(request).filter(
                status=Correction.STATUS_PENDING
            ).count(),
            "notes": AuditNote.objects.all(),
        },
    )


@require_POST
def note_add(request):
    """Add an audit note (e.g. an area observation)."""
    text = (request.POST.get("text") or "").strip()
    if text:
        AuditNote.objects.create(text=text)
        messages.success(request, "Note added.")
    else:
        messages.error(request, "The note was empty.")
    return redirect(request.POST.get("next") or "audit")


@require_POST
def note_delete(request, pk):
    """Delete a single audit note."""
    AuditNote.objects.filter(pk=pk).delete()
    messages.info(request, "Note deleted.")
    return redirect(request.POST.get("next") or "audit")


_COMPUTER_CATEGORIES = {"computer", "personal computer"}


def _asset_intune_info(asset):
    """Intune device match for a Computer/Personal Computer asset, else None.

    Same substring match as intune_list's (serial number found inside the
    Intune device name), but pushed into the DB as an icontains lookup
    instead of a preloaded Python scan — intune_list matches ~441 assets
    against the whole IntuneDevice table in one page load, so preloading is
    worth it there; here it's one asset per scan, so a single query is cheaper.
    """
    category = (asset.model_category or "").strip().lower()
    if category not in _COMPUTER_CATEGORIES:
        return None
    serial = (asset.serial_number or "").strip()
    match = None
    if serial:
        match = (
            IntuneDevice.objects.exclude(device_name__isnull=True)
            .exclude(device_name="")
            .filter(device_name__icontains=serial)
            .first()
        )
    if not match:
        return {"in_intune": False, "assigned_to": "", "compliance": ""}
    return {
        "in_intune": True,
        "assigned_to": match.primary_user_display_name or "",
        "compliance": match.compliance or "",
    }


def _asset_payload(asset, wh=None):
    """JSON-serializable snapshot of an asset for the workstation.

    ``off_site`` tells the page the scanned asset belongs to a DIFFERENT
    warehouse than the one being audited. It's a warning, not a block: a device
    genuinely can be sitting at the wrong site, and that's exactly the kind of
    thing an audit is supposed to catch — refusing the lookup would hide it.
    """
    correction = getattr(asset, "correction", None)
    saved_expected = correction.expected if correction else {}
    return {
        "found": True,
        "asset_id": asset.pk,
        "snow_url": snow_asset_url(asset.asset_tag),
        "intune": _asset_intune_info(asset),
        "off_site": not warehouse_mod.owns(wh, asset),
        "site_code": wh.code if wh else "",
        "identity": [
            {"field": f, "label": audit_field_label(f), "value": _value_str(asset, f)}
            for f in ASSET_IDENTITY_FIELDS
        ],
        "fields": [
            {
                "field": f,
                "label": audit_field_label(f),
                "current": _value_str(asset, f),
                "expected": saved_expected.get(f, ""),
            }
            for f in ASSET_AUDIT_FIELDS
        ],
        "has_saved": correction is not None,
        "saved_changes": correction.changes if correction else {},
    }


def audit_lookup(request):
    """AJAX: look up an asset by scanned tag (or explicit id).

    Deliberately searches ALL assets, not just the current site's: an auditor
    scans whatever is physically in front of them, and "not found" would be a
    misleading answer for a device that merely sits at the other warehouse. The
    payload carries ``off_site`` so the page can say so instead.
    """
    wh = warehouse_mod.current_warehouse(request)
    asset_id = request.GET.get("id")
    if asset_id:
        try:
            return JsonResponse(_asset_payload(Asset.objects.get(pk=asset_id), wh))
        except Asset.DoesNotExist:
            return JsonResponse({"found": False})

    tag = (request.GET.get("tag") or "").strip()
    if not tag:
        return JsonResponse({"found": False})

    matches = Asset.objects.filter(asset_tag=tag)
    if not matches.exists():
        matches = Asset.objects.filter(asset_tag__iexact=tag)
    if not matches.exists():
        # Fall back to serial number (the Audit scan view accepts tag OR serial).
        matches = Asset.objects.filter(serial_number=tag)
        if not matches.exists():
            matches = Asset.objects.filter(serial_number__iexact=tag)
    count = matches.count()
    if count == 0:
        return JsonResponse({"found": False, "tag": tag})

    # Prefer a match at the current site when the tag/serial is not unique, so a
    # duplicated tag resolves to the device the auditor is actually standing in
    # front of rather than to whichever row has the lower id.
    chosen = next((a for a in matches if warehouse_mod.owns(wh, a)), None) or matches.first()
    payload = _asset_payload(chosen, wh)
    payload["match_count"] = count  # warn the UI if the tag is not unique
    return JsonResponse(payload)


def audit_scan(request):
    """Mobile-friendly audit: scan a tag/serial, review the ServiceNow values,
    flag any that are wrong, and submit. Below the form: the recent submissions
    (each flagged field shown in a per-asset modal on wide screens).

    The recent list shows this site's scans PLUS any whose asset is gone.
    ``AuditScan.asset`` is SET_NULL and a truncating xlsx re-import replaces the
    whole Asset table, so orphaned scans are the normal case rather than an edge
    one — dropping them would quietly empty this list after every import.
    """
    wh = warehouse_mod.current_warehouse(request)
    scans = []
    scan_qs = AuditScan.objects.filter(
        Q(asset__in=warehouse_mod.scoped_assets(wh)) | Q(asset__isnull=True)
    )
    for s in scan_qs.select_related("asset")[:100]:
        a = s.asset
        flagged = []
        for f, v in (s.flagged or {}).items():
            try:
                label = str(audit_field_label(f))
            except Exception:
                label = f
            flagged.append({"label": label, "value": "" if v is None else str(v)})
        scans.append({
            "pk": s.pk,
            "handled": s.handled,
            "comment": s.comment or "",
            "asset_tag": s.asset_tag or "",
            "serial_number": s.serial_number or "",
            "created_at": timezone.localtime(s.created_at).strftime("%Y-%m-%d %H:%M"),
            "model": (getattr(a, "model", "") or "") if a else "",
            "name": (getattr(a, "name", "") or "") if a else "",
            "location": (getattr(a, "location", "") or "") if a else "",
            "state": (getattr(a, "state", "") or "") if a else "",
            "flagged": flagged,
            "flagged_count": len(flagged),
        })
    ctx = {"scans": scans, "wh": wh}
    ctx.update(_planned_context(request))
    return render(request, "inventory/audit_scan.html", ctx)


def _can_view_planned(user, audit):
    """A planned audit is private: its creator and administrators only."""
    if not (user and user.is_authenticated):
        return False
    return bool(user.is_staff or user.is_superuser or audit.created_by_id == user.pk)


def _visible_planned(user):
    """Planned audits this user may see (none for anonymous visitors)."""
    if not (user and user.is_authenticated):
        return PlannedAudit.objects.none()
    qs = PlannedAudit.objects.select_related("warehouse", "created_by")
    if user.is_staff or user.is_superuser:
        return qs
    return qs.filter(created_by=user)


def _planned_row_key(r):
    """Sub-tables sort by Name (blank Names last), then asset tag."""
    name = (r.name or "").strip().lower()
    return (not name, name, (r.asset_tag or "").lower())


def _planned_context(request):
    """Template context for the "Planned audits" section: the caller's reports
    (all of them for an admin) with pass/fail tallies, plus the one opened via
    ``?planned=<pk>`` with its rows grouped by State."""
    user = request.user
    audits = list(_visible_planned(user)[:50])
    tallies = {
        r["audit_id"]: r
        for r in PlannedAuditItem.objects.filter(audit__in=audits)
        .values("audit_id")
        .annotate(
            passed=Count("pk", filter=Q(result=PlannedAuditItem.RESULT_PASS)),
            failed=Count("pk", filter=Q(result=PlannedAuditItem.RESULT_FAIL)),
        )
    }
    for a in audits:
        t = tallies.get(a.pk, {})
        a.passed, a.failed = t.get("passed", 0), t.get("failed", 0)
        a.pending = a.sample_size - a.passed - a.failed

    opened, groups = None, []
    pk = request.GET.get("planned")
    if pk and pk.isdigit():
        opened = next((a for a in audits if a.pk == int(pk)), None)
        if opened is None:
            # Past the 50 shown, or not theirs: re-check access rather than assume.
            cand = PlannedAudit.objects.select_related("warehouse", "created_by").filter(pk=pk).first()
            if cand and _can_view_planned(user, cand):
                opened = cand
                opened.passed = opened.items.filter(result=PlannedAuditItem.RESULT_PASS).count()
                opened.failed = opened.items.filter(result=PlannedAuditItem.RESULT_FAIL).count()
                opened.pending = opened.sample_size - opened.passed - opened.failed
    if opened:
        by_state = {}
        for it in opened.items.all():
            it.snow_url = snow_asset_url(it.asset_tag) if it.asset_tag else ""
            by_state.setdefault(it.state or "(no status)", []).append(it)
        for state in sorted(by_state, key=str.lower):
            rows = sorted(by_state[state], key=_planned_row_key)
            groups.append({
                "state": state,
                "rows": rows,
                "passed": sum(r.result == PlannedAuditItem.RESULT_PASS for r in rows),
                "failed": sum(r.result == PlannedAuditItem.RESULT_FAIL for r in rows),
            })
    draft = None
    d = request.session.get("planned_draft") if user.is_authenticated else None
    if d:
        by_state = {}
        for a in sorted(Asset.objects.filter(pk__in=d["ids"]), key=_planned_row_key):
            a.snow_url = snow_asset_url(a.asset_tag) if a.asset_tag else ""
            by_state.setdefault(a.state or "(no status)", []).append(a)
        draft = {
            "percent": d["percent"], "population": d["population"], "size": len(d["ids"]),
            "excluded": d.get("excluded", 0),
            "groups": [{"state": s, "rows": by_state[s]} for s in sorted(by_state, key=str.lower)],
        }
    return {"planned_audits": audits, "planned_open": opened, "planned_groups": groups,
            "planned_draft": draft}


# For In stock devices the Name says where they sit. Plain "IT Storage" is the IT
# office; a suffixed one (IT Storage A/E/G/Floor…), the bare G/D shorthand, and
# the Outside Rack / Waste Container spots are containers elsewhere, so they are
# not sampled.
_IN_STOCK_OUTSIDE_IT_OFFICE = Q(state__iexact="In stock") & (
    Q(name__istartswith="IT Storage ")
    | Q(name__iregex=r"^\s*(G|D)\s*$")
    | Q(name__iexact="Outside Rack")
    | Q(name__iexact="Waste Container")
)


@require_POST
def planned_audit_create(request):
    """Draw a random sample of the current site's non-retired inventory at the
    requested percentage (Retired, Missing and In transit excluded) and hold it as a DRAFT in the caller's session only —
    nothing reaches the audit tables until ``planned_audit_approve``."""
    import math
    import random

    wh = warehouse_mod.current_warehouse(request)
    try:
        percent = int(request.POST.get("percent", ""))
    except ValueError:
        percent = 0
    if not 1 <= percent <= 100:
        messages.error(request, "Enter a sample percentage between 1 and 100.")
        return redirect("audit_scan")

    rows = list(
        warehouse_mod.scoped_assets(wh)
        .exclude(Q(state__iexact="Retired") | Q(state__iexact="Missing") | Q(state__iexact="In transit"))
        .exclude(_IN_STOCK_OUTSIDE_IT_OFFICE)
        .exclude(model__icontains="MC930B")  # RF mobiles, in any state (same test as _rf_mobiles_qs)
        .values_list("pk", "asset_tag")
    )
    # An asset whose latest recorded result at this site is a Pass is done — skip
    # it. Failed (or never-marked) ones stay eligible. Matched by asset tag, not
    # the Asset FK, because a truncating xlsx re-import nulls that FK.
    latest = {}
    for tag, result in (
        PlannedAuditItem.objects.filter(audit__warehouse=wh, checked_at__isnull=False)
        .exclude(result="").exclude(asset_tag="")
        .order_by("checked_at").values_list("asset_tag", "result")
    ):
        latest[normalize_tag(tag)] = result
    passed_tags = {t for t, r in latest.items() if r == PlannedAuditItem.RESULT_PASS}
    ids = [pk for pk, tag in rows if normalize_tag(tag) not in passed_tags]
    excluded = len(rows) - len(ids)
    if not ids:
        messages.error(request, "No assets left to sample at this site (all have already passed an audit)."
                       if rows else "No assets found at this site to sample.")
        return redirect("audit_scan")
    size = min(len(ids), max(1, math.ceil(len(ids) * percent / 100)))
    picked = random.sample(ids, size)

    request.session["planned_draft"] = {
        "wh": wh.pk if wh else None, "percent": percent,
        "population": len(ids), "ids": picked, "excluded": excluded,
    }
    return redirect(f"{reverse('audit_scan')}#planned")


def _next_planned_name():
    """Today's report name: ``YYYYMMDD`` for the first of the day, then
    ``YYYYMMDD-1``, ``YYYYMMDD-2``… for any further ones. Derived from the
    names already stored, so deleting a report never causes a repeat."""
    base = timezone.localdate().strftime("%Y%m%d")
    taken = set(PlannedAudit.objects.filter(name__startswith=base).values_list("name", flat=True))
    if base not in taken:
        return base
    n = 1
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


@require_POST
def planned_audit_approve(request):
    """Commit the session's draft sample as a real PlannedAudit."""
    d = request.session.get("planned_draft")
    if not d:
        messages.error(request, "There's no draft sample to approve — create one first.")
        return redirect(f"{reverse('audit_scan')}#planned")
    assets = list(Asset.objects.filter(pk__in=d["ids"]))
    audit = PlannedAudit.objects.create(
        name=_next_planned_name(),
        created_by=request.user, warehouse=Warehouse.objects.filter(pk=d["wh"]).first(),
        percent=d["percent"], population=d["population"], sample_size=len(assets),
    )
    PlannedAuditItem.objects.bulk_create([
        PlannedAuditItem(
            audit=audit, asset=a, name=a.name or "", display_name=a.display_name or "",
            model_category=a.model_category or "",
            asset_tag=a.asset_tag or "", serial_number=a.serial_number or "",
            station_id=a.station_id or "", gl_account=a.gl_account or "",
            location=a.location or "", assigned_to=a.assigned_to or "",
            state=a.state or "",
        )
        for a in assets
    ])
    request.session.pop("planned_draft", None)
    return redirect(f"{reverse('audit_scan')}?planned={audit.pk}#planned")


@require_POST
def planned_audit_discard(request):
    """Throw away the session's draft sample (it was never saved)."""
    request.session.pop("planned_draft", None)
    return redirect(f"{reverse('audit_scan')}#planned")


@require_POST
def planned_audit_delete(request, pk):
    """Administrators only: remove a report and all its rows."""
    user = request.user
    if not (user.is_authenticated and (user.is_staff or user.is_superuser)):
        raise Http404
    audit = get_object_or_404(PlannedAudit, pk=pk)
    label = audit.name or f"#{pk}"
    audit.delete()
    messages.success(request, f"Deleted planned audit {label}.")
    return redirect(f"{reverse('audit_scan')}#planned")


@require_POST
def planned_audit_result(request, pk):
    """AJAX: set one sampled row to pass / fail (or clear it with an empty
    value). Only the audit's creator or an administrator may."""
    item = PlannedAuditItem.objects.select_related("audit").filter(pk=pk).first()
    if not item or not _can_view_planned(request.user, item.audit):
        return JsonResponse({"ok": False, "error": "Not found"}, status=404)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    result = (body.get("result") or "").strip()
    if result not in ("", PlannedAuditItem.RESULT_PASS, PlannedAuditItem.RESULT_FAIL):
        return JsonResponse({"ok": False, "error": "Invalid result"}, status=400)
    item.result = result
    item.checked_at = timezone.now() if result else None
    # A comment explains a failure, so it goes when the row stops being a fail.
    # Omitting "comment" (a bare radio click) keeps whatever was already saved.
    if result != PlannedAuditItem.RESULT_FAIL:
        item.comment = ""
    elif "comment" in body:
        item.comment = (body.get("comment") or "").strip()
    item.save(update_fields=["result", "checked_at", "comment"])
    items = item.audit.items
    return JsonResponse({
        "ok": True,
        "passed": items.filter(result=PlannedAuditItem.RESULT_PASS).count(),
        "failed": items.filter(result=PlannedAuditItem.RESULT_FAIL).count(),
        "total": item.audit.sample_size,
        "comment": item.comment,
    })


@require_POST
def audit_scan_save(request):
    """Save a mobile audit scan — the flagged {field: value} dict + the date."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    asset = Asset.objects.filter(pk=body.get("asset_id")).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Asset not found"}, status=404)
    flagged = body.get("flagged") or {}
    comment = (body.get("comment") or "").strip()
    AuditScan.objects.create(
        asset=asset,
        asset_tag=asset.asset_tag or "",
        serial_number=asset.serial_number or "",
        flagged=flagged,
        comment=comment,
    )
    return JsonResponse({"ok": True, "count": AuditScan.objects.count()})


def snow_helper(request):
    """Scan/paste many asset tags and/or serial numbers, then open a single
    ServiceNow alm_asset list for all of them (link built client-side). Also a
    person lookup (Assigned to / Managed by) autocompletes from those fields.

    The person list is scoped to the current site — it's there to find "who at
    my warehouse has devices", and the unscoped version offers thousands of
    names from across the whole company."""
    wh = warehouse_mod.current_warehouse(request)
    site_assets = warehouse_mod.scoped_assets(wh)

    def site_values(col):
        return (
            site_assets.exclude(**{f"{col}__isnull": True})
            .exclude(**{col: ""})
            .values_list(col, flat=True)
            .distinct()
            .order_by(col)
        )

    people = sorted(
        {v.strip() for v in site_values("assigned_to") if v and v.strip()}
        | {v.strip() for v in site_values("managed_by") if v and v.strip()},
        key=str.lower,
    )
    return render(request, "inventory/snow_helper.html", {"people": people})


@require_POST
def audit_scan_handle(request, pk):
    """Mark a submitted scan as handled/voided (persists the reviewer's row action).
    Write-gated by the middleware."""
    scan = AuditScan.objects.filter(pk=pk).first()
    if not scan:
        return JsonResponse({"ok": False, "error": "Scan not found"}, status=404)
    scan.handled = True
    scan.save(update_fields=["handled"])
    return JsonResponse({"ok": True})


@require_POST
def audit_save(request):
    """AJAX: save the expected values + computed differences for an asset."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    asset_id = body.get("asset_id")
    expected_in = body.get("expected") or {}
    asset = Asset.objects.filter(pk=asset_id).first()
    if not asset:
        return JsonResponse({"ok": False, "error": "Asset not found"}, status=404)

    # Keep known audit fields the auditor overrode. An empty value is kept on
    # purpose — it means "clear/delete this field".
    expected = {
        f: str(expected_in[f]).strip()
        for f in ASSET_AUDIT_FIELDS
        if f in expected_in
    }
    current, changes = _compute_changes(asset, expected)

    # Always save a correction, even when nothing differs. When there are no
    # differences, store "" in the changes field (an audited-but-no-change marker).
    Correction.objects.update_or_create(
        asset=asset,
        defaults={
            "asset_tag": asset.asset_tag or "",
            "current": current,
            "expected": expected,
            "changes": changes if changes else "",
            "status": Correction.STATUS_PENDING,
            "updated_by": request.user if request.user.is_authenticated else None,
        },
    )
    return JsonResponse({
        "ok": True,
        "changes": changes,
        "change_count": len(changes["expected"]) if changes else 0,
        "pending_count": _scoped_corrections(request).filter(
            status=Correction.STATUS_PENDING
        ).count(),
    })


@require_POST
def audit_clear(request):
    """AJAX: clear the saved expected values for an asset."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)
    asset_id = body.get("asset_id")
    Correction.objects.filter(asset_id=asset_id).delete()
    return JsonResponse({
        "ok": True,
        "pending_count": _scoped_corrections(request).filter(
            status=Correction.STATUS_PENDING
        ).count(),
    })


def access_logs_list(request):
    """Read-only Access Log: who connected (source IP) and the pages they viewed.

    Staff-only (defense in depth beyond the hidden navbar link). Aggregates
    every logged page view into one row per IP, with all visits available in a
    scrollable modal."""
    if not request.user.is_staff:
        messages.error(request, "The access log is available to administrators only.")
        return redirect("station_report")

    # Count page views per (day, IP, user), then group by day so each date is one
    # collapsible header row. The user is part of the key so one shared PC shows a
    # separate line per person; it's blank for anonymous reads (which don't
    # require login) and for every row logged before accounts existed.
    counts = {}  # (date, ip, username) -> count
    for log in AccessLog.objects.select_related("user"):  # ordered -created_at
        day = timezone.localtime(log.created_at).date()
        who = log.user.get_username() if log.user_id else ""
        counts[(day, log.ip, who)] = counts.get((day, log.ip, who), 0) + 1

    by_day = {}
    for (day, ip, who), n in counts.items():
        by_day.setdefault(day, []).append({"ip": ip, "user": who, "count": n})

    day_groups = []
    for day in sorted(by_day, reverse=True):  # newest day first
        iprows = sorted(by_day[day], key=lambda r: r["count"], reverse=True)
        day_groups.append({
            "date": day,
            "ips": len({r["ip"] for r in iprows}),
            "total": sum(r["count"] for r in iprows),
            "rows": iprows,
        })

    return render(
        request,
        "inventory/access_logs_list.html",
        {
            "day_groups": day_groups,
            "total_hits": sum(g["total"] for g in day_groups),
        },
    )


def _scoped_corrections(request):
    """Corrections for assets belonging to the current site.

    ``Correction.asset`` is a non-null OneToOne, so the site is always
    derivable — no orphan case to handle here (a truncating xlsx re-import
    cascades the corrections away with the assets).
    """
    wh = warehouse_mod.current_warehouse(request)
    return Correction.objects.filter(asset__in=warehouse_mod.scoped_assets(wh))


def corrections_list(request):
    """HTML list of the current site's saved corrections, with export links."""
    corrections = _scoped_corrections(request).select_related("asset", "updated_by")
    return render(
        request,
        "inventory/corrections_list.html",
        {"corrections": corrections, "notes": AuditNote.objects.all()},
    )


@require_POST
def correction_delete_change(request, pk, field):
    """Delete a single field's change (current/expected) from a correction."""
    corr = get_object_or_404(Correction, pk=pk)
    ch = corr.changes if isinstance(corr.changes, dict) else {}
    if "current" in ch or "expected" in ch:        # parallel-dict shape
        ch.get("current", {}).pop(field, None)
        ch.get("expected", {}).pop(field, None)
        corr.changes = ch if ch.get("expected") else ""
    elif field in ch:                              # legacy {field: {...}}
        ch.pop(field, None)
        corr.changes = ch if ch else ""
    # Keep the top-level expected snapshot in sync.
    if isinstance(corr.expected, dict):
        corr.expected.pop(field, None)
    corr.save(update_fields=["changes", "expected"])
    messages.info(request, f"Removed change for “{field}”.")
    return redirect("corrections_list")


@require_POST
def correction_delete(request, pk):
    """Delete an entire correction (also used for no-difference records)."""
    tag = (
        Correction.objects.filter(pk=pk)
        .values_list("asset_tag", flat=True)
        .first()
    )
    Correction.objects.filter(pk=pk).delete()
    messages.info(request, f"Deleted correction{(' for ' + tag) if tag else ''}.")
    return redirect("corrections_list")


@require_POST
def corrections_delete_pending(request):
    """Delete every pending correction FOR THIS SITE in one go.

    Scoped deliberately: this is a bulk destructive action reachable from a page
    that now only shows one site's rows, so clearing it must not reach into the
    other site's pending work.
    """
    qs = _scoped_corrections(request).filter(status=Correction.STATUS_PENDING)
    n = qs.count()
    qs.delete()
    messages.info(request, f"Deleted {n} pending correction(s).")
    return redirect("corrections_list")


def corrections_export(request):
    """Download the current site's corrections as JSON or CSV for the extension.

    ?format=json|csv (default json) · ?status=pending|applied|all (default pending)

    Site-scoped: this file is what the Chrome extension replays into the
    ServiceNow list UI, so an export taken at WH2 must not carry WH1's
    edits along with it.
    """
    fmt = request.GET.get("format", "json")
    status = request.GET.get("status", "pending")

    qs = _scoped_corrections(request)
    if status != "all":
        qs = qs.filter(status=status)

    # Dated filename: site code + year-month-day hour-minutes (local time; dash
    # is filename-safe on Windows). The site code is in the name so two people's
    # exports don't look identical sitting in the same Downloads folder.
    wh = warehouse_mod.current_warehouse(request)
    site = f"{wh.code} " if wh else ""
    stamp = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H-%M")

    if fmt == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="corrections {site}{stamp}.csv"'
        writer = csv.writer(response)
        writer.writerow(["asset_tag", "field", "label", "current", "expected", "status"])
        for c in qs:
            rows = c.diff_rows()
            if rows:
                for row in rows:
                    writer.writerow([
                        c.asset_tag, row["field"], row["label"],
                        row["current"], row["expected"], c.status,
                    ])
            else:
                # Audited with no differences -> a row with "" values.
                writer.writerow([c.asset_tag, "", "", "", "", c.status])
        return response

    data = [
        {
            "asset_tag": c.asset_tag,
            "asset_id": c.asset_id,
            "status": c.status,
            "updated_at": c.updated_at.isoformat(),
            "expected": c.expected,
            "changes": c.changes,
        }
        for c in qs
    ]
    response = JsonResponse({"corrections": data}, json_dumps_params={"indent": 2})
    if request.GET.get("download"):
        response["Content-Disposition"] = f'attachment; filename="corrections {site}{stamp}.json"'
    return response


# --- Chrome extension distribution -----------------------------------------

EXTENSION_DIR = settings.BASE_DIR / "chrome_extension"
EXTENSION_ZIP_NAME = "inventory-corrections-extension.zip"
# Files/dirs to leave out of the download.
EXTENSION_SKIP = {"sample_corrections.csv"}


def extension_page(request):
    """Developer page: download the extension + install/use/packaging steps.

    Staff-only — it hands out the extension that pushes edits into ServiceNow."""
    if not request.user.is_staff:
        messages.error(request, "The extension page is available to administrators only.")
        return redirect("station_report")
    return render(request, "inventory/extension.html", {})


def extension_download(request):
    """Zip the chrome_extension folder on the fly and serve it (always current).

    Gated the same as ``extension_page`` — otherwise the download URL would be a
    way around that gate."""
    if not request.user.is_staff:
        messages.error(request, "The extension download is available to administrators only.")
        return redirect("station_report")
    if not EXTENSION_DIR.is_dir():
        raise Http404("Extension folder not found.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(EXTENSION_DIR)
            if rel.parts[0] in EXTENSION_SKIP or rel.name in EXTENSION_SKIP:
                continue
            # Put everything under a top-level folder so it unzips cleanly
            # (zip arcnames use forward slashes).
            arcname = "inventory-corrections-extension/" + rel.as_posix()
            zf.write(path, arcname)
    buf.seek(0)
    response = HttpResponse(buf.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{EXTENSION_ZIP_NAME}"'
    return response


# --- Edit-strategy matrix --------------------------------------------------

# Default strategy assignment per audit field (matches the extension defaults).
DEFAULT_STRATEGY_BY_FIELD = {
    "state": "Dropdown",
    "substate": "Dropdown",
    "location": "Dropdown",
    "department": "Dropdown",
    "assigned_to": "Dropdown",
    "managed_by": "Dropdown",
}
DEFAULT_STRATEGY_FALLBACK = "Text input"


def _ensure_strategies():
    """Seed the strategy columns (and default marks) the first time."""
    if EditStrategy.objects.exists():
        return
    names = ["Dropdown", "Text input", "A", "B", "C", "D", "E"]
    created = {}
    for i, name in enumerate(names):
        created[name] = EditStrategy.objects.create(name=name, position=i)
    # Seed default checkmarks.
    for field in ASSET_AUDIT_FIELDS:
        name = DEFAULT_STRATEGY_BY_FIELD.get(field, DEFAULT_STRATEGY_FALLBACK)
        FieldStrategyMark.objects.get_or_create(field=field, strategy=created[name])


def strategies_view(request):
    """View/edit the field × strategy checkmark matrix."""
    _ensure_strategies()
    strategies = list(EditStrategy.objects.all())

    if request.method == "POST":
        # Update (editable) strategy names.
        for s in strategies:
            new = (request.POST.get(f"name_{s.id}") or "").strip()
            if new and new != s.name:
                s.name = new
                s.save(update_fields=["name"])
        # Rebuild the checkmarks from the submitted checkboxes.
        valid_fields = set(ASSET_AUDIT_FIELDS)
        valid_ids = {s.id for s in strategies}
        FieldStrategyMark.objects.all().delete()
        seen = set()
        for token in request.POST.getlist("mark"):
            try:
                field, sid = token.split(":")
                sid = int(sid)
            except (ValueError, AttributeError):
                continue
            if field in valid_fields and sid in valid_ids and (field, sid) not in seen:
                seen.add((field, sid))
                FieldStrategyMark.objects.create(field=field, strategy_id=sid)
        messages.success(request, "Strategies saved.")
        return redirect("strategies")

    marks = {(m.field, m.strategy_id) for m in FieldStrategyMark.objects.all()}
    rows = []
    for field in ASSET_AUDIT_FIELDS:
        rows.append({
            "field": field,
            "label": audit_field_label(field),
            "cells": [
                {"strategy": s, "checked": (field, s.id) in marks}
                for s in strategies
            ],
        })
    return render(
        request,
        "inventory/strategies.html",
        {"strategies": strategies, "rows": rows},
    )


def session_list(request):
    sessions = ReconciliationSession.objects.all()
    return render(request, "inventory/session_list.html", {"sessions": sessions})


def session_create(request):
    if request.method == "POST":
        form = SessionCreateForm(request.POST, request.FILES)
        if form.is_valid():
            session = form.save()
            try:
                session.columns = services.read_headers(session.expected_file.path)
            except Exception as exc:  # malformed / unreadable workbook
                session.delete()
                messages.error(request, f"Could not read spreadsheet: {exc}")
                return redirect("session_create")
            session.save(update_fields=["columns"])
            if not session.columns:
                messages.error(request, "No header row found in the spreadsheet.")
            return redirect("session_map_columns", pk=session.pk)
    else:
        form = SessionCreateForm()
    return render(request, "inventory/session_form.html", {"form": form})


def session_map_columns(request, pk):
    session = get_object_or_404(ReconciliationSession, pk=pk)
    if request.method == "POST":
        form = ColumnMappingForm(request.POST, columns=session.columns)
        if form.is_valid():
            session.asset_tag_column = form.cleaned_data["asset_tag_column"]
            session.save(update_fields=["asset_tag_column"])
            try:
                count = services.import_expected_items(session)
            except Exception as exc:
                messages.error(request, f"Import failed: {exc}")
                return redirect("session_map_columns", pk=session.pk)
            messages.success(request, f"Imported {count} expected items.")
            return redirect("session_detail", pk=session.pk)
    else:
        form = ColumnMappingForm(
            columns=session.columns,
            initial={"asset_tag_column": session.asset_tag_column},
        )
    return render(
        request,
        "inventory/map_columns.html",
        {"session": session, "form": form},
    )


def session_detail(request, pk):
    session = get_object_or_404(ReconciliationSession, pk=pk)

    if request.method == "POST":
        form = ScanForm(request.POST, session=session)
        if form.is_valid():
            tag, data = form.cleaned_data_payload()
            ScannedItem.objects.create(
                session=session,
                asset_tag=normalize_tag(tag),
                data=data,
                scanned_by=form.cleaned_data.get("scanned_by", ""),
            )
            messages.success(request, f"Recorded scan for {tag}.")
            return redirect("session_detail", pk=session.pk)
    else:
        form = ScanForm(session=session)

    report = services.compare(session)
    return render(
        request,
        "inventory/session_detail.html",
        {
            "session": session,
            "form": form,
            "report": report,
            "scanned_items": session.scanned_items.all(),
        },
    )


def scan_delete(request, pk, scan_pk):
    session = get_object_or_404(ReconciliationSession, pk=pk)
    item = get_object_or_404(ScannedItem, pk=scan_pk, session=session)
    if request.method == "POST":
        item.delete()
        messages.info(request, "Scan removed.")
    return redirect("session_detail", pk=session.pk)


def session_delete(request, pk):
    session = get_object_or_404(ReconciliationSession, pk=pk)
    if request.method == "POST":
        session.delete()
        messages.info(request, "Session deleted.")
        return redirect("session_list")
    return render(
        request, "inventory/session_confirm_delete.html", {"session": session}
    )


def export_report(request, pk):
    """Download the discrepancy report as CSV."""
    session = get_object_or_404(ReconciliationSession, pk=pk)
    report = services.compare(session)

    response = HttpResponse(content_type="text/csv")
    safe_name = session.name.replace(" ", "_")
    response["Content-Disposition"] = f'attachment; filename="report_{safe_name}.csv"'

    writer = csv.writer(response)
    columns = session.value_columns
    header = ["asset_tag", "status"]
    for col in columns:
        header += [f"{col} (expected)", f"{col} (scanned)"]
    writer.writerow(header)

    for row in report["rows"]:
        line = [row.asset_tag, row.status_label]
        exp_data = row.expected.data if row.expected else {}
        scn_data = row.scanned.data if row.scanned else {}
        for col in columns:
            line += [exp_data.get(col, ""), scn_data.get(col, "")]
        writer.writerow(line)

    return response
