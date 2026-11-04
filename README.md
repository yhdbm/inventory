# Inventory Reconciliation

A Django web app for auditing and reconciling an IT asset inventory against a
ServiceNow `alm_asset` export. It was built for a multi-warehouse operation:
import the asset spreadsheet, browse and audit the fleet per site, record
**Corrections** for anything that is wrong, and push the fixes back into the
ServiceNow list UI with a companion Chrome extension.

> **Portfolio copy.** Every company, site, person, host and address in this
> repository — code, docs and the bundled demo database — is fictional. The
> relationships inside the data are preserved (see [Demo data](#demo-data)), so the
> app behaves exactly as it did on the real data.

## Features

- **Station report** — asset donut + breakdowns by category, Station ID and state,
  a category × state pivot, and saved snapshots; every figure deep-links into the
  matching ServiceNow list.
- **Multi-site scoping** — one database, several warehouses. An asset belongs to a
  site by its Station ID prefix (`WH1|…`) or, failing that, its Location; each user
  sees their own site, staff can switch.
- **Audit workstation** and mobile scan page — scan a tag, compare current vs
  expected values, save a Correction; planned (random-sample) audits with Pass/Fail.
- **Corrections** — pending fixes with CSV/JSON export, consumed by the Chrome
  extension (`chrome_extension/`) which fills ServiceNow list cells automatically.
- **Printers** — per-site printer list with live SNMP supply levels (pure-Python
  SNMP walker; Xerox web-UI scraping with SNMP fallback) and a new-print-queue helper.
- **Impressions** — lifetime page counts read over SNMP, monthly snapshots, and
  one-click e-mail reports.
- **Intune** — asset ↔ Intune device matching by serial number, compliance, primary
  user, a "who is fixing it" roster, and an Autopilot re-enrolment guide.
- **RF mobiles** — monthly device check-off with snapshots.
- Local accounts with a custom `can_edit_inventory` permission, DB-backed sessions,
  an access log, and a secret key that is never stored in the repository.

## Quick start

```bash
pip install -r requirements.txt

# the app refuses to start without a secret key
export INVENTORY_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')"
#   Windows:  python init_secret_key.py      (writes %PROGRAMDATA%\inventory\secret_key)

python manage.py migrate        # no-op with the bundled demo database
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in:

| User | Password | Role | Site |
| --- | --- | --- | --- |
| `demo` | `demo1234` | superuser | WH1 — Cedarport Main / Annex |
| `editor` | `demo1234` | Editors group | WH2 — Lakeside Rd |

Change or delete both accounts before deploying anywhere. Reads are open to anyone
who can reach the server; writes need a signed-in user with the edit permission.
Extra allowed hosts go in the comma-separated `INVENTORY_ALLOWED_HOSTS` variable.

## Deploy to Railway

`railway.json` holds the start command: `railway_prestart.py` → `migrate` → gunicorn.

1. Create a service from this repo (or `railway up` from the project folder).
2. Add a **Volume** to the service, mounted at `/data`.
3. Set the service variables:

   | Variable | Value |
   | --- | --- |
   | `INVENTORY_SECRET_KEY` | a long random string (`python -c "import secrets; print(secrets.token_urlsafe(50))"`) |
   | `INVENTORY_DB_PATH` | `/data/db.sqlite3` |
   | `INVENTORY_MEDIA_ROOT` | `/data/media` |

4. Settings → Networking → **Generate Domain**. The domain is picked up from
   `RAILWAY_PUBLIC_DOMAIN` automatically; a custom domain goes in
   `INVENTORY_ALLOWED_HOSTS` and `INVENTORY_CSRF_TRUSTED_ORIGINS` (`https://...`).

On the first deploy the bundled `db.sqlite3` is copied onto the volume; after that
the volume copy is the live database and redeploys never overwrite it. Keep the
service at one replica (it is a single SQLite file). Printer SNMP checks need the
printers' LAN, so they won't work from the cloud.

## Demo data

`db.sqlite3` is a masked copy of a real database: about 63,000 assets, 4,000 Intune
devices, snapshots, audit history. Names, hosts, IPs, e-mail addresses, sites,
teams, serial numbers and asset tags were replaced with fictional values by a
deterministic one-to-one mapping, so **the structure of the data is unchanged**:
duplicate tags stay duplicates, a serial still appears inside its Intune device
name, Station ID prefixes still select the right site (1,827 assets at WH1, 308 at
WH2), GL markers, dates, states, costs and counts are untouched. The people,
companies and places you see are generated.

## Layout

```
config/                  settings and URLs
inventory/
  models.py              Asset (alm_asset), Warehouse, Correction, snapshots, Intune, ...
  warehouse.py           which site a request is scoped to
  views.py               all views / JSON endpoints
  services.py            xlsx import, reconciliation and report aggregates
  printer_supplies.py    SNMP supply reader and per-make strategies
  middleware.py          write gate and access log
  management/commands/   import_xlsx, import_intune_csv, set_warehouse, ...
  templates/             server-rendered UI (Bootstrap 5, vanilla JS)
chrome_extension/        MV3 extension that applies Corrections inside ServiceNow
servicenow-autopilot-extension/   bulk / surgical field-fill helper for ServiceNow
CLAUDE.md                architecture notes and design decisions (read this next)
```

## Notes

- Set the real ServiceNow instance in `chrome_extension/manifest.json`
  (`host_permissions`) and in the two `SN_BASE` constants; it ships with the
  placeholder `your-instance.service-now.com`.
- `DEBUG` is off on purpose; `config/urls.py` serves the two static assets itself.
- To load your own data, upload a ServiceNow `alm_asset` `.xlsx` on the home page
  (or `python manage.py import_xlsx <file>`), and an Intune "All devices" export on
  the Intune page.
