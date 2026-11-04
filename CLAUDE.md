# Inventory System — Project Guide (read me first)

> **For Claude / AI assistants:** This is the canonical continuation guide for this
> project. **READ THIS FILE FIRST** at the start of any session before exploring the
> codebase — it exists to save tokens. If anything below conflicts with the code, the
> code wins — verify before asserting, and update this file when you make structural
> changes.
>
> Last updated: 2026-10-01 (multi-site warehouses — see §2.2).

---

## 0. About this portfolio copy

This repository is a **sanitised portfolio version** of an internal tool. Every company,
site, person, host and address in the code, the docs **and the bundled demo database** is
fictional (the sites are `WH1` "Cedarport Main / Annex" and `WH2` "Lakeside Rd"; people are
generated names; hosts use `example` domains; IPs are in private ranges). The relations
inside the data are real: asset ↔ Intune serial matching, Station ID prefixes ↔ warehouse
scope, GL markers, RF and impression snapshots all behave exactly as in the original.

- Demo logins (bundled `db.sqlite3`): `demo` / `demo1234` (superuser, site WH1) and
  `editor` / `demo1234` (Editors group, site WH2). Change or delete them before deploying.
- Create the Django secret key once (`python init_secret_key.py`) or set
  `INVENTORY_SECRET_KEY` — the app refuses to start without one.
- The Chrome extension is loaded **unpacked from `chrome_extension/`**; its host permission is
  the placeholder `https://your-instance.service-now.com/*` — set your own instance.
- Shell notes in this guide use Unix syntax; on Windows use PowerShell equivalents.

## 1. What this is

A **Django** web app for auditing/reconciling the company's **ServiceNow `alm_asset`**
inventory. Workflow: import the ServiceNow asset xlsx → browse/audit assets → record
**Corrections** (pending fixes) → push fixes back into ServiceNow via a companion
**Chrome extension** that fills the ServiceNow list UI. Also includes a printers
section with on-demand SNMP supply-level checks, RF-mobile audit tracking, and a
reconciliation (expected-vs-scanned) module.

## 2. Tech stack & how to run

- Django 6.0.5, Python 3.x. DB = **SQLite** `db.sqlite3` (~67k `alm_asset` rows; note
  file-locking caveats on a network share). Parsing via **openpyxl**. QR codes via
  **segno** (added 2026-09-16) — chosen because it's pure-Python with zero dependencies and
  writes SVG natively, so it preserves the "no compiled deps, any 3.x interpreter" property
  the `.venv` workaround below relies on (`qrcode` would pull in Pillow for raster output).
- Frontend: server-rendered Django templates + vanilla JS + **Bootstrap 5** (sticky navbar).
- Entry point `manage.py`; settings `config/settings.py`; URLs `config/urls.py` →
  `inventory/urls.py`.
- **Run:** `python manage.py runserver` (from the project folder, with the secret key set)
  — **`--insecure` is no longer required** (2026-10-01). `config/urls.py` now serves
  `/static/` itself via `staticfiles.views.serve` with `insecure=True`, so the flag is
  harmless but redundant.
  - Why this was added: `DEBUG` is False (an error page would otherwise print the settings),
    and `runserver` won't serve static files with DEBUG off unless passed `--insecure`. But
    the app has only **two** static assets — `inventory/snow.jpg` and `inventory/soti.svg` —
    because the CSS is a `<style>` block in `base.html` and Bootstrap is a CDN link. So
    forgetting the flag did NOT produce an obviously broken page; it produced a normal-looking
    page whose ServiceNow / SOTI icons silently 404'd. That is a bad failure mode, so the
    route removes it.
  - It grants nothing `--insecure` didn't: the staticfiles view resolves through the
    **finders**, so it can only serve files inside an app's `static/` dir. Verified that
    `/static/../db.sqlite3` and `/static/inventory/../../../config/settings.py` both 404.
- **First run on a new machine:** `python init_secret_key.py` (standalone script at the project
  root — **not** a `manage.py` command; see §2.1 for why). `SECRET_KEY` is no longer in
  `settings.py`, and the app refuses to start without it.
- **Verify without a browser:** `.venv/Scripts/python.exe manage.py check`, and Django's
  `django.test.Client` for hitting views/endpoints (set `DJANGO_SETTINGS_MODULE=config.settings`
  and `django.setup()` in a one-off script).
- **Dependencies:** `pip install -r requirements.txt` (Django, openpyxl, segno — all pure
  Python, so any 3.x interpreter works).
- `ALLOWED_HOSTS` is `localhost`, `127.0.0.1`, `testserver` plus whatever is listed in the
  comma-separated `INVENTORY_ALLOWED_HOSTS` environment variable.

## 2.1 Auth & secrets (rewritten 2026-09-17 — replaced the shared unlock code)

- **Accounts.** Local Django users. Reads stay open to anyone on the LAN; **writes need a
  signed-in user holding `inventory.can_edit_inventory`** (custom permission on `Correction`,
  migration `0023`). Membership of the **"Editors"** group grants it —
  `python manage.py ensure_editors_group --add <username>`. Superusers bypass it (Django
  grants them everything). Manage users at **`/admin/`**, where each account now also carries
  a **warehouse assignment** inline (§2.2). Current accounts: `demo` (superuser, WH1),
  `editor` (Editors, WH2).
- **Gone:** `WRITE_CODE_HASH`, the `unlock`/`lock` views + routes, the navbar code box, and
  the `hash_write_code` command. `request.session["can_write"]` is gone too — templates now
  read a **`can_write`** context variable from `inventory/context_processors.py`
  (the name was kept so the ~36 existing `{% if can_write %}` conditions still read the same).
- **`WriteAccessMiddleware`** checks the permission instead of the session flag. ⚠️ It blocks
  *every* unsafe method, and **logging in is a POST** — `AUTH_PATH_NAMES` allowlists
  login/logout/password-change for exactly that reason. Remove that allowlist and nobody can
  ever authenticate. `WRITE_GATE_EXEMPT` (the Intune Fix endpoints) now means "any signed-in
  user, no edit permission needed".
- **Staff-only** (`user.is_staff`): `/access-logs/`, `/extension/`, `extension_download`
  (gated too — otherwise it routes around the page), and the navbar's Admin link.
- **`SECRET_KEY` is not in `settings.py`.** It's read from `INVENTORY_SECRET_KEY`, else
  `INVENTORY_SECRET_KEY_FILE`, else `%PROGRAMDATA%\inventory\secret_key`, and a missing key
  is a **hard startup failure** (never a default fallback). Why: the project lives on an open
  share, and the old setup signed sessions with a committed `django-insecure-…` key — so
  anyone who could read the share could forge a session cookie (`can_write`, or a superuser
  id).
  - Create it with **`python init_secret_key.py`** — a **standalone script at the project
    root**, importing nothing from Django. It is deliberately *not* a `manage.py` command:
    `manage.py` loads `config/settings.py` before doing anything, and settings is precisely
    what fails when the key is missing, so a management command could never run in the one
    situation it exists for. (It was written as one first; it was unrunnable. Don't move it
    back.) `--show` reports the resolved path, `--force` rotates.
  - Each serving machine needs its own copy. If users must stay signed in across several
    machines, generate once and **copy the file** — re-running the script makes a *different*
    key, and sessions made on one machine are then rejected by the other. With a single
    serving machine, just run it there.
- **Sessions are DB-backed** (`SESSION_ENGINE = ...sessions.backends.db`), 8-hour lifetime,
  expiring on browser close. They used to be signed cookies on the theory that the server
  might not be able to write SQLite on the share — but `AccessLogMiddleware` already writes a
  row on every page view, so that was moot. The real gain: a DB session key is random, so a
  leaked `SECRET_KEY` can't mint one.
- **Known accepted risk:** served over plain HTTP, so `SESSION_COOKIE_SECURE`/
  `CSRF_COOKIE_SECURE` are False and passwords + cookies cross the LAN in cleartext.
  `manage.py check --deploy` reports exactly 4 warnings (W004/W008/W012/W016), all of them
  this. A TLS-terminating reverse proxy is the real fix and is not done.

## 2.2 Warehouses — multi-site scoping (added 2026-10-01)

The app was single-site (Cedarport Main / Annex, Station IDs tagged `WH1|…`) and said so by
repeating a hard-coded Location filter — `Q(location__iregex=r"\bCPT\b") | Q(location__icontains="Annex")`
— in six different views. It is now **multi-tenant by site**: one codebase, one database, one
ServiceNow import, and each person sees only their own warehouse.

- **`Warehouse`** (models.py, migration `0024`; the two rows are seeded by the data migration
  `0025`, then `0026` renames the WH2 one). Fields: `code` ("WH1"/"WH2"), `name`,
  `station_prefixes` (comma-separated, no trailing `|`), `location_regex`,
  `snow_location_sys_ids`, `is_default`, `position`.
  | code | name | station prefix | location regex | SNOW sys_ids |
  |---|---|---|---|---|
  | `WH1` | Cedarport Main / Annex | `WH1\|` | `\bCPT\b\|Annex` | CPT + Annex (**default site**) |
  | `WH2` | Lakeside Rd | `WH2\|` | `WH2` | **none yet** — see §9 |

  > The WH2 site was **`HR` until 2026-10-01** (migration `0026` renamed the code *and* the
  > prefix to `WH2`). `WH2|` is what the data already uses — **46 assets** carry it
  > (`WH2|Production` ×37, `WH2|Vortex Cage` ×6, `WH2|DMS` ×2, and one bare `WH2|`
  > with no area, which pre-dates this work) — whereas **nothing** was ever tagged `HR|`.
  > Don't reintroduce `HR`: there is a real `WH1|HR` station, so `HR` as a loose token is
  > ambiguous in a way `WH2` isn't.
- **The scope rule (`Warehouse.asset_q()`):** an asset belongs to a site when its **Station ID
  starts with that site's prefix + `|`**, OR its **Location matches the regex and no OTHER
  site's prefix claims it**.
  - The prefix match is **anchored at the start**, not a `contains`: real data holds `WH1|HR`
    and even `WH1|WH1|Art Department`, and a loose contains-test would mis-file them.
  - The Location half is **not** redundant. It keeps *untagged* assets (blank or free-form
    Station ID) inside the site that owns them — **254 of WH2's 300**, since only 46 carry
    a `WH2|…` tag and the rest are free-form (`main area`, `vortex cage`) or blank. Drop the
    Location half and the WH2 user loses 85% of their fleet.
  - **"Explicit tag beats location guess"** is what keeps the two sites **disjoint**. Without
    that clause they overlapped on a real row — one asset tagged `WH1|…` whose Location is
    `US_MS_Lakeside Rd` — and every total double-counted it. Verified: 0 assets match both.
  - `Warehouse.owns(asset)` is the Python twin of `asset_q()` for a single already-fetched
    asset. **Keep the two in step**, including the tag-beats-location rule.
- **`UserProfile`** — `OneToOne(User)` + nullable FK to `Warehouse`. `demo` → WH1,
  `editor` → WH2. A user with no profile falls back to the default site rather than erroring,
  so an account created straight in `/admin/` still works.
- **`inventory/warehouse.py`** is the only place that answers "which site is this request?".
  `current_warehouse(request)` resolves: **staff session override → user's profile → the
  `is_default` row → the first row**, caching on the request. Steps 3-4 are load-bearing:
  **reads are open to anonymous LAN visitors** (§2.1), so there is no profile to read and the
  fallback must never raise. `scoped_assets(wh)` is what every view now calls.
- **Navbar brand** reads `🧩 Inventory Reconciliation | <code>` — title de-emphasized
  (`.brand-title`), **site code bold and larger** (`.brand-site`), since the code is the one
  part whose meaning changes page to page. `.brand-title` hides below 1500px (it's ~230px of
  width and says nothing the page title doesn't); the code never hides. The bar carries an
  explicit `min-height: 60px` rather than inheriting whatever its tallest child happens to be.
  **If you widen anything in the right-hand cluster, re-check that the centred links still fit
  on one row** — a second row of links is what makes the bar look wrong.
- **Staff-only warehouse picker** in the navbar (`warehouse_switch`, POST, session-only —
  nobody's profile is touched). `context_processors.warehouse` supplies `warehouse` (drives the
  navbar brand + every page heading, so the site code is always on screen) and
  `warehouse_options`, which is **empty for non-staff** so the picker never renders for them.
  The session key is also **ignored for non-staff** in `current_warehouse`, so a stale key can't
  widen anyone's scope. `warehouse_switch` is in `middleware.WRITE_GATE_EXEMPT` because it
  writes no data; the view re-checks `is_staff` since the URL is guessable.
- **Scoped:** Station report, Printers, Intune, Impressions, RF Mobiles, Corrections
  (list + **export** + delete-pending), both Snapshot histories, the Reconcile page's
  autocomplete datalists, SNOW Helper's person list, and every ServiceNow deep link
  (`_snow_scope(wh)` replaced the `_SN_MAIN_LOC`/`_SN_ANNEX_LOC` constants).
- **Deliberately NOT scoped:**
  - **`audit_lookup`** searches all 63k assets. An auditor scans whatever is in front of them,
    and "not found" would be a misleading answer for a device that merely sits at the other
    site. The payload carries **`off_site`** + `site_code` and both audit pages render a red
    "⚠ Not at WH1" warning instead. When a tag is duplicated it now prefers the match at the
    current site.
  - **`asset_list`** (`/assets/`, hidden from the nav) — it's the raw import browser; showing
    everything is its job.
  - **The Intune "Fix" fixer-summary totals** — a fixer's workload is theirs wherever the
    asset sits.
  - **`AuditScan` rows whose asset is NULL** are shown at *both* sites. `AuditScan.asset` is
    `SET_NULL` and a truncating xlsx re-import replaces the whole Asset table, so orphans are
    the normal case, not an edge one; filtering them out would quietly empty the list after
    every import.
- **Snapshots carry a `warehouse` FK** (nullable). `0025` backfills `ReportSnapshot` and
  `RfSnapshot` to WH1 (they were all Main/Annex). `ImpressionSnapshot` is **left null on
  purpose** — that page was cross-site until now, so its saved readings *mix* Main/Annex with
  WH2 and calling them WH1 would be a lie. They still appear at both sites, labelled
  **"all sites (pre-split)"** in an amber italic Site column. `RfSnapshot` is now keyed
  `(period, warehouse)`, not `period` alone.
- **Behaviour changes to know about:**
  - **Impressions split in two.** It was the one page that deliberately spanned Main / Annex
    **and** WH2 in a single monthly grand total; it is now per-site (20 in-use printers at
    WH1, 14 at WH2), and the report subject/heading carry the site code.
  - **WH1 gained ~23 assets** that are tagged `WH1|…` while sitting at another Location
    (1,805 → 1,828). That's the prefix rule working as intended.
  - **`rf_asset_add`** used to hard-code `location="CPT"` so a new row stayed in scope; it now
    seeds the site's own `"<code>|"` Station ID prefix instead and leaves Location blank.
- **CLI:** `manage.py set_warehouse <user> <code> [--create] [--editor] [--staff]`, and
  `manage.py set_warehouse --list` to dump sites + every user's assignment.
- **Performance note (pre-existing, not caused by this):** the SQLite file lives on the network
  share and neither `station_id` nor `location` is indexed, so each scope query is a full
  63k-row scan — measured **~10-15 s**. The *old* hard-coded Main/Annex filter measured 10.4 s,
  the new WH1 scope 14.3 s (one extra `LIKE` in the same scan). Adding a `station_id` index is
  the obvious lever if page loads become a complaint.

## 3. Layout

```
config/            settings.py, urls.py, wsgi/asgi
inventory/
  models.py        Warehouse, UserProfile, Asset (alm_asset; each header a db_column),
                   Correction, ReconciliationSession, ScannedItem, ExpectedItem,
                   ReportSnapshot, EditStrategy, FieldStrategyMark, SavedFilter,
                   AssetImport, RF-mobile models, normalize_tag
  warehouse.py     which site a request is scoped to + scoped_assets() (see §2.2)
  views.py         all view/endpoint handlers
  urls.py          routes
  services.py      Comments parsing, station/inventory aggregates, xlsx import/reconcile logic
  printer_supplies.py   SNMP supply reader + per-make strategies (see §6)
  forms.py, admin.py, middleware.py
  management/commands/   import_xlsx, ensure_editors_group, set_warehouse, seed_* 
  templates/inventory/   base.html, printers_list.html, asset_list.html, audit.html, etc.
chrome_extension/  Chrome MV3 extension (see §7)
media/             uploaded xlsx (asset_sources/) + session uploads
```

## 4. Data model essentials

- **`Asset`** = one ServiceNow asset; columns mirror the xlsx headers via `db_column`
  (e.g. "Asset tag", "Model", "Model category", "Location", "State", "Substate",
  "Disposal reason", "Station ID", "Comments", "Manufacturer"). Python field names:
  `asset_tag, serial_number, station_id, location, name, state, substate,
  disposal_reason, comments, model_category, manufacturer, model, ...`. The editable
  audit set is `ASSET_AUDIT_FIELDS` (models.py) — `disposal_reason` was added there
  (migration `0015`) so it shows in the audit workstation / Reconcile / strategies matrix.
  Its edit strategy mirrors GL account (Text input in Django, `"input"` in the extension).
- **GEMBA rule (Reconcile page `audit.html`, client JS):** when the GL account expected
  value contains "GEMBA" (on load of the scanned asset, or when typed), that value is
  copied into the **Disposal reason** expected input and the GL account is set to the
  `202606` inventory marker (both as overrides → saved as a Correction). `resetGembaFields`
  re-mirrors GL account + Disposal reason to the current asset on every scan so a prior
  asset's values never carry over. Not on the mobile scan page (`audit_scan.html` only flags).
- **Printers** = the **current warehouse's** assets (§2.2) **AND** `model_category` contains
  "printer" **AND** `state` = "In use". Area grouping = `station_id`.
- **`Warehouse` / `UserProfile`** — the per-site scoping, fully described in **§2.2**. Read
  that before touching any view's asset filter.
- **`Correction`** = pending edit per asset; stores `current`/`expected` dicts and a `changes`
  summary; status PENDING. Edits in the UI create Corrections, not direct Asset writes.
  Carries **`updated_by`** (nullable FK to User) — named that, not `created_by`, because both
  write paths use `update_or_create` so a re-scan overwrites the row; it means "last edited
  by" and pairs with `updated_at`. Null for every row predating accounts. Also hosts the
  `can_edit_inventory` permission in its `Meta` (see §2.1).
- **`AccessLog`** carries a nullable **`user`** FK alongside `ip`; null for anonymous reads
  (which don't need login) and pre-accounts rows. The page groups by (day, IP, user).

### Comments field (structured + messy)
`services.py` packs sub-fields into `Asset.comments`:
- `PRINTER_COMMENT_KEYS = ["IP","FMS","FMS 2","DMS","Service ID","FEDEX","UPS","Windows Queue"]`
- `parse_comment_fields` / `comment_fields_map` / `compile_comment_fields` handle both the
  compiled form `"IP: x / FMS: y"` and legacy free-form `"IP 10.0.0.1 | DMS foo | Windows host"`
  (pipe/slash separators, alias mapping; unknown keys preserved).
- `extract_ip(text)` regex-scans for the first valid IPv4 (handles `IP:10..`, `IP 10..`,
  `|IP10..`) — the structured parser alone misses many.

## 5. Features built

> **Every "Main / Annex" below now means "the current warehouse" (§2.2).** The phrasing is
> kept where it describes history or a page's own inline scope; the filters themselves all
> go through `warehouse.scoped_assets()`.

- Station report `/` (home): the donut + 3 breakdown panels (category / Station ID / state)
  are inside a **tabbed report** (`.rpt-tabs`/`.rpt-pane`): **Tab 1 "Active"** excludes
  Missing + Retired devices; **Tab 2 "All states"** is everything. Each tab is built from a
  `_report_bundle(assets, wh)` (donut totals + SNOW-linked breakdowns), rendered via the
  `inventory/_report_panels.html` partial (driven by a `b` bundle; collapse groups are
  class-based `.cat-group`/`.cat-none`, no IDs, so it renders twice safely). The state panel
  shows **Retired** at the bottom with a boxed per-**Substate** breakdown
  (`services.substate_breakdown`), each substate bar linking via `substatus=<slug>`.
  - **"Devices by category & state" pivot** (`services.category_state_matrix`, context
    `cat_state_matrix`, decorated by `views._decorate_cat_state_matrix`): rows = model
    category, columns = each State (Retired last, highlighted) + always-visible Retired
    **substate** columns (`.retsub-col`) + Total. Each cell is split by the 202606 marker —
    `.mv` (has 202606, **accent/blue**) **/** `.uv` (hasn't, **amber #f59e0b**) to match the
    range bars, both bold, each a ServiceNow link (category dot-walk + `install_status` +
    Retired substate via `substatus=<slug>`/`_snow_substatus` + `gl_accountLIKE202606` or
    `_GL_NOT_MARKED`). A zero side is dropped along with the `/` divisor; an all-zero cell
    renders empty. Value columns equal-width + centered. Cell partial:
    `inventory/_matrix_cell.html`. Same Main/Annex asset set. The state panel's Retired
    substate bars use the same `substatus=` filter.
- xlsx import of `alm_asset` (`import_xlsx` loads ALL tabs; not read_only).
  - The station report's **"Asset source" card** (editors only — it's inside
    `{% if can_write %}`; the old `request.session.can_write` flag is long gone, see §2.1)
    holds the upload form, and next to its heading a
    **"Get source from ServiceNow"** pill (`.snow-export` + `.lnk-ico` snow.jpg icon, added
    2026-09-16) opening the *saved* alm_asset list module to export a fresh .xlsx from. The URL
    is `views.SNOW_ASSET_EXPORT_URL` — a fixed opaque literal (its `sysparm_userpref_module`
    GUID identifies the saved SNOW module and isn't derivable from anything we store), passed
    to the template as `snow_asset_export_url`. Note `station_report.html` needed
    `{% load static %}` added for the icon.
- Asset browser with persistent multi-select filters (`SavedFilter`).
- Audit workstation `/audit/` (scan tag → Current/Same/Expected → Save Correction).
- Corrections page + CSV/JSON export.
- `EditStrategy` + `/strategies/` matrix (field × Dropdown/Text + placeholders).
- RF-mobile audit tracking + snapshots.
- Reconciliation (expected vs scanned) sessions.
- Printers page `/printers/` + **printer supply-level check** (§6). The Asset Tag cell carries a
  **ServiceNow icon link** (`snow_asset_url`, `static/inventory/snow.jpg`, `.lnk-ico` 15px —
  same convention as Impressions / Intune / RF Mobiles) rendered *before* the existing 🧩
  Reconcile link. Added 2026-09-16; this had been the only asset-listing page without one.
- **Impressions page `/impressions/`** — on-demand total lifetime impression (page count)
  reads over SNMP. Scope = **LEXMARK + Xerox** printers at the **current warehouse** (the
  page still builds its own `printer_scope` Q inline for the make/category/asset-tag part —
  it does NOT use `_printers_in_use_qs()` — but the site filter is now
  `warehouse.scoped_assets()` like everywhere else). **This page used to span Main / Annex
  AND WH2 in one combined total and was the only deliberate cross-site page; it was split
  per-site on 2026-10-01 — see §2.2 for what that did to the saved snapshots.** In-use
  printers fill the top table; the
  browser polls `printer_impressions_check` (`/impressions/<pk>/`, JSON, read-only) per
  printer in parallel and tallies a live grand total. Reads `prtMarkerLifeCount`
  (`1.3.6.1.2.1.43.10.2.1.4`) via `printer_supplies.get_total_impressions(ip)`
  (max value from the walk). Verified live against Xerox C8000 @ 192.0.2.10. Columns:
  Asset tag (+ ServiceNow icon link via `snow_asset_url`, image `static/inventory/snow.jpg`),
  Serial number, Service ID (Comments sub-field), Area, **Location**, Name, Manufacturer,
  Model, IP (linked to `http://<ip>`), Total impressions. Rows sorted by **location →
  manufacturer → serial number** and **colour-coded by location** (WH2 amber
  `.loc-lakeside`, Main/Annex blue `.loc-mainannex`; legend under the table). On load, each
  in-use cell is prefilled with its **last saved value** from `last_by_tag` (built from all
  snapshots, newest-first) shown **stale** (`.imp-stale`, amber italic + "last <date>" note),
  and the grand total shows a stale baseline; **Read all** overwrites with live figures
  (fresh = green `.imp-imp`), and a failed read falls back to the stale last value rather than
  "no data". The **Read all** button never disables the Save button (only itself, re-enabled
  in a `finally`). Records with **no asset tag are excluded** from both tables (`printer_scope`
  drops null/empty `asset_tag`). Each table footer is labelled **Total**; the not-in-use
  `<details>` shows its total inline in the collapsed summary; a separate totals card stacks
  **Reachable total** (live reads), **Unreachable total** (last-saved for unread in-use +
  not-in-use manual), and **Grand total** (JS `recomputeTotals`, driven by a `readOk` pk→value
  map). **Snapshots include unreachable printers' last saved values** (`inUseUnreachableReadings`
  + `combinedReadings`), so each month's record is complete; the warning modal now says the
  unreachable last values *will be included*. Reports (styled `.eml` HTML, plain text, and the
  snapshot detail table) show a combined **Make / Model** column (manufacturer is persisted in
  each reading; `make_model` computed in the view). Saving is write-gated —
  `impressions_snapshot_save` returns **403** when the session isn't unlocked (JS shows a
  "Locked (403) — enter the access code / reload" hint); unlock via the navbar code to save.
  **Snapshots:** a write-gated "📸 Save snapshot" button POSTs the browser-gathered
  readings to `impressions_snapshot_save` (`/impressions/snapshot/`, JSON) → new
  `ImpressionSnapshot` model (migration `0016`; `taken_at`, `period` "YYYY-MM",
  `total_impressions`, `printer_count`, `readings` JSON). A collapsed "Saved snapshots"
  `<details>` on the same page lists them with per-printer detail, a write-gated Delete
  (`impression_snapshot_delete`), and **three email options** per snapshot (each report
  includes its month; all built client-side from a `json_script#impSnapData` blob):
  1. **✉ Copy styled** — copies a Bootstrap-styled inline-CSS HTML report to the clipboard
     (rich `text/html` via async Clipboard API, `execCommand`+contenteditable fallback for the
     http LAN origin; `text/plain` alternative) and opens the mail app (subject only) to paste into.
  2. **⬇ .eml** — downloads a `message/rfc822` file with `X-Unsent: 1` + the HTML body, so the
     mail app opens it as a draft with the styled report already in it (no paste).
  3. **✉ Plain text** — `mailto:` with a spaced, column-aligned plain-text report IN the body.
  (`mailto:` bodies are plain-text only — hence the copy/.eml routes for styled HTML.)
  **Not-in-use table:** a second collapsed `<details>` below the in-use table lists laser
  (Xerox/LEXMARK) printers in **Main / Annex / Lakeside** whose State is NOT "In use". Each row
  has a manual impressions `<input>`; when a printer has a valid IP its input is prefilled
  with the **last recorded value** for that asset_tag from the newest snapshot (view builds a
  `last_by_tag` lookup). On Save snapshot the client sends live in-use reads **plus** these
  manually-entered rows — all count toward the combined `total_impressions` and the report.
- **Access Log** `/access-logs/` — **staff-only** page (nav link gated by `user.is_staff`, plus a
  view-level gate). `AccessLogMiddleware` (after `WriteAccessMiddleware`) logs each plain HTML
  GET page view to the `AccessLog` model (source IP via `client_ip()`, `url_name`, path, time);
  AJAX/JSON endpoints, POST actions, static & admin are skipped. Page shows **one row per
  (day, IP)** with the **visit count** that day — columns Date · IP · Visits, newest day first
  (busiest IP first within a day). Accumulates (no auto-trim).
- **`IntuneDevice` table** (added 2026-08-14) — mirrors a Microsoft Intune "All devices"
  CSV export (`media/uploads/Intune.csv`, 3,701 rows), one real DB column per CSV header
  (same `db_column`-matches-header convention as `Asset`; header→field map is
  `INTUNE_HEADER_MAP` in `models.py`). All fields are kept as raw text, including dates —
  the export uses .NET sentinel dates (`0001-01-01...` / `9999-12-31...`) and 7-digit
  fractional seconds, so this is a faithful mirror, not a curated/typed table. No UI page
  yet (data-only); reload with
  `python manage.py import_intune_csv media/uploads/Intune.csv --truncate`. Not currently
  joined/cross-referenced with `Asset` (e.g. by serial number) — that'd be a reasonable
  next step if reconciling Intune vs. ServiceNow inventory comes up.
- **Intune page** `/intune/` (added 2026-08-15, nav link right after Printers) —
  `views.intune_list`: the **current warehouse's** assets with `model_category` Computer or
  Personal Computer (via `warehouse.scoped_assets()`; this used to repeat the
  `\bCPT\b`-regex-or-Annex location filter inline), **excluding any `model` containing "Mac"** (Apple
  hardware — MacBook/iMac/Mac mini/etc. — doesn't match Intune's Windows-style device
  naming anyway, so this just drops noise; verified it only removes genuine Mac models,
  ~24 of them, and doesn't change the matched count). Left-joined in Python to
  `IntuneDevice` — a match is an asset whose `serial_number` appears anywhere inside the
  Intune `device_name` (e.g. serial `18WBSW3` inside device name `OOS-18WBSW3`). Every
  qualifying asset is shown, matched or not, so gaps are visible. ~441 assets today, ~138
  matched. **Grouped by State** (`state_groups` in the view, same click-to-expand/
  collapsed-by-default pattern as `access_logs_list.html`'s day groups — `.in-group`/
  `.in-row`, `data-gi` index, `hidden` until toggled); each group header shows its own
  asset count and how many of those matched an Intune device (e.g. "159 assets · 118 in
  Intune"). Within a group, rows are sorted by match status — **"In use" shows
  unmatched (no Intune device) first**, since that's the actionable gap there, then
  **sub-sorted matched-compliant vs. not** (Intune `compliance == "Compliant"` vs.
  anything else), then by **asset tag** as the final tiebreak; every other state just
  shows matched-first with no compliance sub-sort. Columns: Asset tag (+ ServiceNow icon
  link via `snow_asset_url`, same `snow.jpg` convention as other pages)/Serial/Station
  ID/Name/**State**/Location/Model/Assigned to/**202606** (green `.inv-dot` if
  `"202606" in gl_account`, same marker/dot convention as the Station report's
  `inventory_marked`), then Intune device name (+ link to the device's Intune admin
  center record via `views.intune_device_url`, built from `IntuneDevice.device_id` — the
  Intune-assigned GUID, not the Azure AD Device ID —
  `https://intune.microsoft.com/#view/Microsoft_Intune_Devices/DeviceSettingsMenuBlade/~/overview/mdmDeviceId/<id>`;
  this is the standard/commonly-documented Intune deep-link pattern, not verified against
  a live tenant since it needs an authenticated session — only present when
  matched)/Compliance (pill: Compliant=green, Noncompliant=red, other=amber)/**Primary
  user** (red `.dup-user` background when that name appears on more than one row in the
  whole table — someone with multiple devices listed, computed via a `Counter` over all
  rows before grouping)/Last check-in/OS version/Encrypted. Both the SNOW and Intune links
  use `class="popout"` + a JS click handler that calls `window.open(...)` with explicit
  window features (`width=1400,height=900,menubar=no,...`) instead of `target="_blank"`,
  which is what makes browsers spawn a real separate window rather than another tab. The
  page overrides `base.html`'s centered `.wrap-inner { max-width: 1280px }` with `max-width:
  none` (scoped to this template's own `<style>` block — CSS cascade, not `!important`) so
  the wide table gets the full page width. An always-visible **"▲ Go up"** button
  (`.go-up`, fixed bottom-right) resets both the page scroll and the table card's own
  internal scroll (`#intuneCard`, which scrolls independently via
  `max-height`/`overflow:auto`).
  - **Search box searches every column** (changed 2026-10-15; was asset tag/serial only) — a plain
    case-insensitive substring match on each row's full text, so a name like a Primary user or
    Assigned to returns every row it appears on, across all State groups.
  - **"Get source from Intune" pill** (added 2026-10-15) — next to the "Intune device source" heading
    (editors only, same card as the upload form). Opens the Intune admin center's All devices blade
    (`.../Microsoft_Intune_DeviceSettings/DevicesMenu/~/allDevices`) in a new tab, where the user
    exports the CSV and uploads the zip below. URL is a literal in the template; styled `.intune-export`
    to match the Station report's `.snow-export` pill (no icon).
  - **"Fix" column** (added 2026-08-19, last column) — per-asset "who's fixing this" tag,
    backed by two new models: `FixPerson` (small shared roster, `name` + `initials`,
    grown only via the modal's own "add a new person" mini-form — no admin page) and
    `IntuneFixAssignment` (`OneToOneField` to `Asset` + `ForeignKey` to `FixPerson` —
    one assignment per asset, picking someone new overwrites it). The row's button shows
    **"+"** (unassigned) or the assigned person's **initials** (`.fix-btn.assigned`,
    green). Clicking it opens a shared modal (`#fixModal`) listing the roster as **radio**
    buttons (single-select, not checkboxes — picking one immediately POSTs and closes the
    modal, no separate Save button) plus the add-person mini-form. Two write-gated JSON
    endpoints: `views.intune_fix_save` (`/intune/fix/save/`, `{asset_id, person_id}`,
    `person_id: null` clears the assignment) and `views.intune_fix_person_add`
    (`/intune/fix/person/add/`, `{name, initials}`). **Deliberately exempt from the
    write-access code** — `middleware.WRITE_GATE_EXEMPT` allowlists these two URL names
    in `WriteAccessMiddleware._allowed()`, since this is a lightweight who's-on-it tag,
    not a data correction; anyone can assign/add without unlocking (every other POST in
    the app is still gated as before — verified). Each person in the modal list shows a
    **count** (`.fix-count`, reusing the `.in-count` pill style) of how many rows in
    *this filtered table* are currently assigned to them — computed server-side from
    `fix_by_asset` (the same lookup used to paint the row buttons, so it's scoped to the
    page's own Main/Annex/Computer asset set, not a global count), and kept live in the
    browser without a reload: `assignPerson()` decrements the previous person's count and
    increments the new one via `adjustFixCount()`; newly-added people start at 0.
    **Per-person color** (added 2026-08-19) — `FixPerson.color`/`.text_color` are
    computed properties, not DB columns: an 8-slot validated categorical palette
    (Claude's dataviz skill's reference light-mode palette — blue/orange/aqua/yellow/
    magenta/green/violet/red) indexed by `id % 8`, so a person's color is stable
    forever regardless of roster re-sorting (name-ordered) or new additions — keyed by
    identity, not list position. Yellow gets dark text (`#1f2933`) for contrast; every
    other slot gets white. Applied as inline `style="background:...; color:...;"` (not a
    CSS class) on both the row's Fix button and the modal's `.fix-initials-badge`, kept
    in sync by the JSON endpoints returning `color`/`text_color` alongside
    `initials`/`name`. Beyond 8 people the 8 slots repeat — acceptable for a small
    internal roster; not worth a "9th series" strategy for a non-chart identity badge.
  - **"Intune re-enrollment steps" section** (added 2026-10-08, bottom of the page, a collapsed
    `<details>` card after Fixers summary) — ported from the old IT-Toolkit `/intune` page:
    Intune/Autopilot delete links, Shift+F10, the pwsh / TLS12 / ExecutionPolicy / Autopilot
    command walkthrough, and the three WH1 group-tag barcodes. Static HTML only (no view
    change). The 11 screenshots live in `inventory/static/inventory/intune/`. The old page's
    per-image `title=` tooltips (which held the commands) are shown as visible `<code>` text.
- **Planned audits** (added 2026-10-08, section on `/audit-scan/`, migrations `0028`/`0029`) — a signed-in
  editor enters a **sample %**; `planned_audit_create` draws `ceil(pop × %)` random **non-Retired** assets from
  `scoped_assets(wh)` and keeps them as a **draft in `request.session["planned_draft"]` only** (dashed
  preview, no radios). **Approve audit** (`planned_audit_approve`) is what writes **`PlannedAudit`** +
  **`PlannedAuditItem`**; **Discard** drops the draft. Admins (staff/superuser) get a 🗑 per report
  (`planned_audit_delete`, 404 for anyone else). Failed rows carry a `comment` (migration `0029`) edited via
  a "+" modal; it's cleared when the row stops being a Fail. Items are written
  (values snapshotted per row: tag, serial, Station ID, GL account, Location, Assigned to, State, so the
  report survives an xlsx re-import). The opened report (`?planned=<pk>`) is a **collapsed `<details>`
  per State** with **Pass/Fail radios** that save on change (`planned_audit_result`). **Private:**
  `_can_view_planned` / `_visible_planned` allow only `created_by` and `is_staff`/superusers; anyone else
  gets 404 on the item endpoint and sees nothing in the list. Both POSTs are write-gated (edit permission).
- **SNOW Helper** `/snow-helper/` — paste asset tags/serials (auto-detect or forced field)
  and open a single ServiceNow `alm_asset` list of all of them in a new tab; also a person
  lookup (Assigned to / Managed by autocomplete) that opens their SNOW assets. Per-token
  conditions are joined with `^NQ` (not `^OR`, changed 2026-08-08) so the opened SNOW list
  preserves the order tags/serials were typed/pasted instead of ServiceNow's own default sort.
- **Nav bar `Create Incident` link** (added 2026-08-08) — a red button (`.incident-link`,
  `--red` accent, styled to stand out from the plain nav links) opening ServiceNow's
  "Create Incident" catalog item in a new tab. Lives in a `.lock-wrap` container together
  with the lock/unlock form (so it sits right before the "Read-only 🔒 Locked" /
  "Editing enabled" box) — `.lock-wrap`, not `.lock-form`, is now the nav-bar's third grid
  column and the mobile hamburger-collapse target.
- **QR endpoint** `/qr/?text=…` (`views.qr_svg`, added 2026-09-16) — renders arbitrary text as a
  **QR code SVG**; read-only, no write gate, deliberately generic (any page can point an `<img>`
  at it). `segno.make_qr` (**not** `make`, which would return a Micro QR for short payloads that
  many handheld scanners can't read), error correction `M`, optional `scale`/`border` params
  (clamped). Refuses text over `QR_MAX_CHARS` (1200) with a 400 rather than emit an unscannably
  dense code. Sets `Cache-Control: public, max-age=86400` — output is deterministic for the same
  text, so repeated identical requests are served from the browser cache. **`segno` is imported
  inside the function, not at module scope, on purpose:** the share's `.venv` may not have it
  installed yet, and a top-level import would 500 every page in the app instead of just this
  endpoint (which degrades to a readable 503).
- **Login** `/login/` (`registration/login.html`), `/logout/` (**POST-only** — the navbar uses a
  form, a GET returns 405), `/password-change/`. Wired individually rather than
  `include("django.contrib.auth.urls")` because that would expose a password-RESET flow that
  can't work (no email backend). See §2.1.
- Chrome extension (§7).

## 6. Printer supply-level check (added 2026-06-12)

- **Endpoint:** `printers/<int:pk>/supplies/` → `views.printer_supplies_check` (GET, JSON,
  read-only). Resolves IP from comments (`comment_fields_map`→`extract_ip` fallback), picks
  a strategy by make, returns `{ok, ip, model, supplies:[{name,level,max,percent,low,status}], error?}`.
- **`inventory/printer_supplies.py`:** stdlib-only SNMP v1 GETNEXT walker (no pysnmp). Walks
  Printer-MIB `1.3.6.1.2.1.43.11.1.1` — description `.6`, level `.9`, maxCapacity `.8`, type `.5`.
  Community/timeout/threshold from settings. `Supply` dataclass; `StandardPrinterMibStrategy`;
  `resolve_strategy(manufacturer, model)` → strategy for **laser** makes (Xerox/LEXMARK/HP),
  `None` for **label** makes (Zebra/Sato/Avery) so they're skipped. `_MEASURABLE_TYPES` =
  consumed types only (toner/ink/drum/developer) — **waste receptacles never flagged low**.
  Sentinels: level `-2`=ok, `-3`=present, `253/254`=present indicator.
- **Xerox reads its own web UI instead of SNMP (added 2026-08-08):** Xerox's raw
  Printer-MIB level/max counters are linear and were confirmed live to diverge sharply
  from the printer's own displayed remaining-life % near end-of-life (SNMP math gave 1%
  for a toner the printer's own embedded web UI showed as 10%/"Reorder" — Xerox firmware
  applies an undocumented non-linear calibration curve the raw MIB counters don't reflect).
  `resolve_strategy` now routes `manufacturer == "xerox"` to `XeroxWebConsumablesStrategy`,
  which does `GET https://<ip>/stat/consumables.php` (HTTPS, self-signed cert — TLS
  verification is disabled only inside this printer-IP fetch, never globally) and parses
  the calibrated numbers straight out of the page. Markup varies by firmware generation —
  section headings are `<h2>` on newer ConnectKey UIs, `<h3>` on older ones, and some
  generations wrap each row's % bar in a nested `<table>` — so `_XEROX_SECTION_RE` bounds a
  section by "up to the next heading", **not** "up to the next `</table>`" (that would stop
  at the nested table's own close after just the first row). `low` comes from the page's
  own Status word (`!= "OK"`) rather than `PRINTER_SUPPLY_LOW_THRESHOLD` — Xerox has already
  made that call with its calibrated number — scoped to Toner/Drum/Ink sections only (Waste
  Toner Container etc. never flagged, same rule as the SNMP path). **Falls back to
  `StandardPrinterMibStrategy` (plain SNMP)** if the page can't be fetched/parsed: some
  Xerox lines use an older embedded-UI generation that doesn't serve this page at all (TLS
  handshake fails outright) — confirmed for VersaLink C7030/C8000/C9000 and a Proficio
  PX300, which all correctly fall back. Verified live across the Main/Annex fleet that
  AltaLink C8045/C8145/C8200 all use the web-scrape path.
- **Settings:** `PRINTER_SNMP_COMMUNITY="public"`, `PRINTER_SNMP_TIMEOUT=4`,
  `PRINTER_SUPPLY_LOW_THRESHOLD=5` (a toner/drum is "low" at/below 5%; was 20 until 2026-07-30).
- **`printers_list` view:** orders `station_id, model_category, model, name` (area grouping
  kept; within-area sort = category then model). Adds `asset_tag`, `probe_ip`, `supports_levels`
  to each printer dict.
- **`printers_list.html`:** grouped by `station_id`, click row to expand. First column =
  **Asset Tag** (SNOW icon + 🧩 Reconcile link — see §5; the template `{% load static %}`s for
  the icon); a **"Levels"** column with a **"+"** button (laser only) sits **before Comments**;
  clicking "+" opens a **modal** (`#lvlModal`) of supply chips, low (≤threshold) shown red with ⚠.
  Colspan = 16 (this copy has FEDEX/UPS/Windows Queue columns).
  **Queue-column hover popover (added 2026-09-16):** the **FMS / FMS 2 / DMS** cells carry an
  extra `.q-col` class; hovering one shows a small fixed-position popover (`#qPop`, a single
  reusable node repositioned per cell) holding an **"Add new queue?"** button. Because the
  popover contains a *button*, it uses a **grace period** — it stays while the pointer is over
  the cell **or** the popover and hides 200ms after leaving both, and is placed flush to the
  cell's bottom edge so there's no dead gap to cross; hiding instantly on cell-exit would make
  the button unclickable. It's suppressed while the cell is being inline-edited or the modal is
  open, and dropped on any scroll (fixed coords would drift from the cell — the table card
  scrolls independently). Clicking the button opens `#qModal`, which holds:
  - **Step 0 — DNS record** (`.q-dnsreq`, above the form): a button linking to the Service
    Portal **Internal DNS Record Request** catalog item, plus the values to enter there —
    Domain `example.corp`, Change type `Add`, **DNS Name** `<dns>@example.corp` (live from
    the form), Record Type `(A) Host and IP Address (A-Records)`, and **IP Address** from the
    asset record. Shown for **both** printer types (the DNS record is needed either way), so
    it sits outside the laser-only section. The raw `&` in the catalog URL is written `&amp;`
    in the template, same as base.html's "Create Incident" link.
  - **Wrench icon (added 2026-10-09):** every FMS / FMS 2 / DMS cell that has a value shows a 14px
    wrench beside the copy icon (`.pf-icons`/`.pf-wrench`). It opens `#qtModal`, which mirrors the
    IT-Toolkit `:8008/queues_troubleshoot` page: queue field (prefilled, editable) → four QR +
    click-to-copy commands — `lpstat -W -a<q>`, `sudo enq -P<q> -D`, `sudo enq -P<q> -U`,
    `sudo cancel <q>` (the strings that page encodes in its QRs). QRs come from `/qr/`.
  - **Queue name** and **DNS name** inputs. The DNS field takes the **short host** only;
    **two different suffixes** are derived from it — `DNS_SUFFIX` = `.example.corp` (dotted
    FQDN, for `piomkjetd -h`) and `TICKET_SUFFIX` = `@example.corp` (for the ticket row).
    Both forms are named in a `.q-hint` under the field rather than pinned to the input as an
    addon, precisely because there isn't one canonical suffix. An empty DNS yields neither
    suffix, so no orphan `.example.corp` / `@example.corp` can leak into the output.
  - A **Label / Laser** radio pair, preselected from the record by `views._printer_kind`
    (see below) with the source category shown beside it; fully overridable.
  - **Laser:** two click-to-copy command boxes (`#qCmd1`/`#qCmd2`, `.q-copy`, also
    keyboard-activatable, flashing a "Copied" pill). Only the queue name and DNS name vary —
    everything else (`-p 'hplj-4000'`, `-D pcl`, `-x '9100'`, `-p '17' -l '66' -w '180'
    -z '+'`) is a fixed literal. **Watch the two different host forms:** `piomkjetd`'s `-h`
    takes the **FQDN** (`<dns>.example.corp`) while `piochpq`'s `-d` takes the **bare** DNS
    name behind `hp@`. An empty DNS yields `-h ''` rather than an orphan `.example.corp`.
  - A third copy box under a **"Ticket to PRINT-OPS"** heading (`#qTicket`, same `.q-copy`
    click-to-copy behaviour — the handler binds every `.q-copy` in the modal, so further boxes
    need no JS change). Body = a request line plus a prntab row:
    `queue | dns@example.corp | <laser|label> printer | model-on-record`. The printer-type
    wording follows the radio, and the model comes from the asset record (`tr`'s `data-model`).
  - **Label:** no commands specified yet — shows a placeholder and hides the QR. The ticket box
    lives **inside the laser section**, so it's hidden for label too (the spec only qualified it
    "if laser") — worth revisiting if label printers also need a prntab ticket.
  - The **QR encodes the two commands only**, not the ticket (that's copy-paste, not scanned).
  - A **QR code** of both laser commands joined (via `/qr/`, debounced 250ms).
- **`views._printer_kind(model_category, manufacturer)`** — returns `"label"`/`"laser"` for the
  radio default. Category is the primary signal but is **wrong for ~7% of the fleet** (a "Badge
  Printer" and four plain "Printer" records are really Sato/Zebra/Avery), so it falls back to
  `printer_supplies.is_label_make()`. That helper exists rather than reusing
  `resolve_strategy(...) is None` because the latter **also** returns None for a *blank*
  manufacturer — "unknown", not "label". Current split: 51 label / 21 laser of 72.
  **Auto-probe on load (added 2026-07-30):** the page now polls `/printers/<pk>/supplies/`
  for every `supports_levels` printer via a browser worker pool (`CONCURRENCY=8`, adapted
  from the Impressions "Read all" pattern) and colors by status — the printer row gets
  `.sup-low` (red tint + accent) / `.sup-ok` (green) / `.sup-unknown` (unreachable), the
  **Levels** cell is repainted with red **⚠ low-supply chips** (short name + %) beside a
  re-wired "+" (full modal unchanged), and each low printer bumps a red **⚠ N low** badge +
  `.grp-low` tint on its Station header (visible while collapsed). `#lvlProgress` in the top
  card shows "Checking… X/Y" then the low count. Front-end only — endpoint/`low` flag unchanged.
- **Test printers (verified live against the original deployment):** a Xerox VersaLink C8000
  (SNMP `public` works; no ConnectKey web UI, so supplies always fall back to SNMP) and Xerox
  AltaLink C8200 / C8145 units (both serve the ConnectKey supplies page, the web-scrape path)
  — a good pair for testing either code path. The demo database holds masked printer IPs
  from private ranges, so supply reads will show "unreachable" unless you point a record at a
  real printer.

## 7. Chrome extension (`chrome_extension/`, MV3)

- **Loaded unpacked from this folder.** After edits, the user reloads via
  `chrome://extensions`. **Bump `manifest.json` `version` + `version_name`** (the popup footer
  shows `version_name`) so the user can confirm the new build. Currently **0.9.7**
  (0.9.6 added `disposal_reason` to `STRATEGY_FIELDS`/`STRATEGY_DEFAULTS`, same `"input"`
  strategy as `gl_account`; 0.9.7 made an empty expected value clear the field).
- **Files:** `popup.html/js` (main UI), `content.js` (apply engine injected into the ServiceNow
  tab), `viewer.html/js`, `strategies.html/js`, `strategy-defaults.js`, `csv-import.js`.
- **Flow:** import corrections CSV (header `asset_tag,field,label,current,expected,status`) →
  `popup.js` builds a plan `{assetTag:{labelLower:expected}}` (only rows where current≠expected)
  → injects `content.js` → sends `INV_APPLY {plan, fieldOrder}` → on-page overlay edits each
  ServiceNow list cell via inline edit (double-click → editor → set → green OK). `fieldOrder`
  from `STRATEGY_FIELDS` (e.g. State before Substate — see preference below).
- **content.js behavior (today):** `normalize()` treats a cell showing `"(empty)"` as `""`;
  an **empty expected value now CLEARS the field** — when it differs from the current cell
  the apply loop sets the cell blank via `applyBehavior`'s empty path (choice → "-- None --",
  text → `clearEditorValue`); if the cell is already blank it's counted as already-correct
  (never re-cleared). This reversed the earlier "never clear a field" rule (v0.9.7).
- **popup per-field bypass toggles (today):** checkboxes "Assigned to" / "Managed by"
  (checked=apply, uncheck=bypass) above the Apply button; bypassed labels are dropped from the
  plan; choice persists in `chrome.storage.local` key `fieldToggles`. Added because those
  type-ahead reference fields are slow to apply.

## 8. Working preferences

- **No tests** — do not write or run tests for this project.
- **No code blocks in chat replies** — give analysis + results; put code only in files.
- **Django's `{# … #}` comment is SINGLE-LINE ONLY.** Spread one across two lines and it
  stops being a comment: the text renders into the page. Three multi-line ones in
  `base.html` printed commentary straight into the navbar on every page (which is what
  "navbar is too tall" turned out to be). Use `{% comment %} … {% endcomment %}` for
  anything multi-line. Grep for a `{#` on a line with no closing `#}` to catch these.
- **`runserver --noreload` serves stale templates.** `DEBUG=False` makes Django enable the
  **cached template loader**, so template edits are not picked up until the process
  restarts — a template fix can look like it did nothing. Restart before concluding.
- ServiceNow: edit **Substate AFTER State** (Substate options depend on State).
- When changing the extension, **bump the manifest version** so the load is verifiable.

## 9. Pending / next ideas

- **Get WH2's ServiceNow location sys_id** and paste it into admin → Warehouses → WH2 →
  `snow_location_sys_ids`. Until then WH2's SNOW deep links scope with
  `u_station_idSTARTSWITHWH2|`, which reaches only the **46 tagged** assets — the other
  **254 are invisible to those links**. Grab it from the `sys_id=` of the Lakeside Rd record in
  `cmn_location`, or from the URL of any WH2-filtered alm_asset list. The `has_snow_ids`
  column in the Warehouse admin flags which sites still lack it.
- **Retag the remaining 254 WH2 Station IDs to `WH2|…`** in ServiceNow — they're
  free-form (`main area`, `vortex cage`, `qc area`, `by the lifts`) or blank. The Station
  report's "untagged" counter is the backlog. `station_prefixes` takes a comma-separated list
  if a second spelling ever has to count as well.
- Consider an index on `Asset.station_id` if the scope queries get slow enough to notice
  (see the performance note in §2.2).

- Make the extension READ the Django `EditStrategy` table (e.g. a `/strategies/export/` JSON
  endpoint) instead of its local `strategy-defaults.js`, so strategies + field ordering are
  data-driven.
- Reference type-ahead fields (Assigned to / Location / Manufacturer / Model) may need a picker
  step in the extension (related to the bypass toggles just added).
- Preserve pending `Correction`s across xlsx imports via an archive table (3 open decisions).
- **Put TLS in front of the app** (reverse proxy). Login now exists (§2.1) but credentials
  still cross the LAN in cleartext — this is the biggest remaining gap.
- Label-printer commands for the Printers page's new-queue modal (laser only today).
