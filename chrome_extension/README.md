# Inventory Corrections Importer (Chrome extension)

- **Phase 1:** import the corrections CSV exported from the inventory system and
  display the differences (viewer page).
- **Phase 2:** apply the expected values onto the ServiceNow asset list — the
  popup finds each asset tag's row and replaces the corrected columns' values.

## Apply corrections to ServiceNow

1. Import the CSV in the viewer (see below).
2. Open the ServiceNow asset list for the assets you want (the viewer's
   **Open in ServiceNow** button builds that list URL from the unique tags).
3. Click the extension icon → **Apply corrections to this page**.
   - It matches each row by its **Asset tag** column, then for every corrected
     field it finds the column by its **label** (State, Location, Assigned to, …).
   - For each cell it performs ServiceNow's inline list edit: **double-click the
     cell → wait for the editor input → type the expected value → press Enter**
     to commit. Choice/select fields are set by matching the option text.
   - This persists the change to ServiceNow (each committed cell saves the
     record). Committed cells are outlined green.
   - The popup reports how many values were committed (e.g. "Committed 7 of 8").

The popup also shows the **import version**: the date/time the current CSV was
imported, plus row/asset/change counts.

## Field edit strategies (settings)

ServiceNow edits values two ways depending on the field:
- **Dropdown** — choice fields (e.g. State, Substate) open a `<select>`; the
  extension picks the matching option.
- **Text input** — string/reference fields (e.g. Station ID, Name) open a text
  box; the extension types the value.

Open **Field edit strategies ⚙️** from the popup to set the strategy per field:
- Every field is listed with a dropdown to choose its strategy.
- The cell is shown with a **green background when it matches the hardcoded
  default**; changing it removes the green.
- Changes auto-save. **Roll back to defaults** restores the hardcoded setup.

Defaults: `State`/`Substate` → Dropdown; everything else → Text input. The
**Apply** action uses this config to pick the right editor control for each
field (and falls back to the control actually present if they differ).

> Notes:
> - Applying is asynchronous (it waits for each editor to appear and for the
>   save), so it can take a few seconds for many cells — keep the popup open.
> - The list must allow inline edit and show the columns you're correcting.
> - Column matching relies on the list's header labels matching the CSV labels.
> - Reference fields with type-ahead pickers may need a selection step; if a
>   cell doesn't commit, adjust the editor selectors in `applyCorrections`
>   (popup.js) to your instance's DOM.

## Load it in Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and select this `chrome_extension/` folder.
4. Pin the extension and click its toolbar icon → **Open corrections viewer**.

## Use it

1. In the inventory app, go to **Corrections → Export CSV**
   (`/corrections/export/?format=csv`).
2. Import the file either from the **popup** (extension icon → *Import
   corrections CSV*) or in the **viewer** (*Open corrections viewer* → *Import
   corrections CSV*). Importing **replaces any previously imported data**.
3. The table shows every row: Asset tag, Field, Current value, Expected value,
   Status. Rows where current ≠ expected are highlighted red.
   - **Search** filters by any column.
   - **Only changes** hides the "audited, no change" rows.
   - The import is remembered (via `chrome.storage.local`) and reloads next time;
     **Clear** forgets it.

## CSV format expected

Header row: `asset_tag, field, label, current, expected, status`
(one row per changed field; assets with no changes appear as a single row with
empty field/current/expected).

## Files
- `manifest.json` — MV3 manifest (only the `storage` permission).
- `popup.html` / `popup.js` — toolbar popup that opens the viewer.
- `viewer.html` / `viewer.css` / `viewer.js` — the importer + table.
