// Inventory Corrections — applies expected values onto a ServiceNow list.
// The cell-editing engine (open editor, type/select, autocomplete, commit) is
// adapted from the proven "ServiceNow AutoPilot" content script; the per-asset
// targeting and label-based control picking are ours.
(function () {
  if (window.__INV_CORR_LOADED__) return;
  window.__INV_CORR_LOADED__ = true;

  const T = {
    pollInterval: 50,
    editorOpenTimeout: 3500,
    editorReadyTimeout: 3500,
    autocompleteTimeout: 7000,
    saveTimeout: 4500,
    shortSaveTimeout: 1200,
    legacyAutocompletePause: 900,
    maxRetries: 3,
    retryBackoff: 200,
    betweenCells: 120,
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(check, { timeout = T.saveTimeout, interval = T.pollInterval } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      try { const r = check(); if (r) return r; } catch (e) {}
      await sleep(interval);
    }
    return null;
  }

  const normalize = (v) => {
    const s = (v || "").replace(/\s+/g, " ").trim().toLowerCase();
    // ServiceNow renders a blank list cell as the literal text "(empty)".
    return s === "(empty)" ? "" : s;
  };

  // The list cell may visually truncate long values (e.g. Comments) but keep the
  // full text in a title/tooltip. When the visible text looks cut off, prefer the
  // longer title so comparisons use the complete value, not the shown snippet.
  function fullCellText(cell) {
    if (!cell) return "";
    const txt = (cell.textContent || "").trim();
    if (/[.…]{1,3}$/.test(txt)) {
      const titled = (cell.matches && cell.matches("[title]")) ? cell
        : (cell.querySelector && cell.querySelector("[title]"));
      const full = titled && titled.getAttribute("title");
      if (full && full.trim().length > txt.length) return full.trim();
    }
    return txt;
  }

  function isVisible(el) {
    if (!el) return false;
    const s = window.getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // --- On-page status overlay (so it's obvious whether the run is active) ----
  // Set true to stop the run early (Stop button or Esc). Checked between cells
  // and between retries so a bad cell doesn't force the whole cycle to finish.
  let __aborted = false;
  function abortRun() {
    __aborted = true;
    const stop = document.getElementById("__inv_stop");
    if (stop) { stop.disabled = true; stop.textContent = "Stopping…"; }
  }
  function ensureOverlay() {
    let el = document.getElementById("__inv_status");
    if (!el) {
      el = document.createElement("div");
      el.id = "__inv_status";
      el.style.cssText =
        "position:fixed;top:12px;right:12px;z-index:2147483647;background:#1f2933;" +
        "color:#fff;font:13px/1.4 system-ui,Segoe UI,Roboto,sans-serif;padding:10px 14px;" +
        "border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.35);max-width:340px;";
      const msg = document.createElement("div");
      msg.id = "__inv_status_msg";
      el.appendChild(msg);
      const stop = document.createElement("button");
      stop.id = "__inv_stop";
      stop.textContent = "■ Stop (Esc)";
      stop.style.cssText =
        "margin-top:8px;cursor:pointer;border:0;border-radius:6px;padding:5px 10px;" +
        "background:#dc2626;color:#fff;font:12px system-ui,Segoe UI,Roboto,sans-serif;";
      stop.addEventListener("click", abortRun);
      el.appendChild(stop);
      (document.body || document.documentElement).appendChild(el);
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") abortRun(); }, true);
    }
    return el;
  }
  let __spin = 0;
  function setStatus(html, bg) {
    const el = ensureOverlay();
    const msg = document.getElementById("__inv_status_msg");
    (msg || el).innerHTML = html;
    if (bg) el.style.background = bg;
  }
  function setRunning(html) {
    const dots = ".".repeat((__spin++ % 3) + 1);
    setStatus("⏳ <b>Running" + dots + "</b><br>" + html, "#1f2933");
  }

  function dispatchMouseSequence(target, type) {
    const o = { bubbles: true, cancelable: true, view: window };
    target.dispatchEvent(new MouseEvent("pointerdown", o));
    target.dispatchEvent(new MouseEvent("mousedown", o));
    target.dispatchEvent(new MouseEvent("mouseup", o));
    target.dispatchEvent(new MouseEvent("click", o));
    if (type === "dblclick") {
      target.dispatchEvent(new MouseEvent("pointerdown", o));
      target.dispatchEvent(new MouseEvent("mousedown", o));
      target.dispatchEvent(new MouseEvent("mouseup", o));
      target.dispatchEvent(new MouseEvent("click", o));
      target.dispatchEvent(new MouseEvent("dblclick", o));
    }
  }

  function clickElement(target) {
    if (!target) return;
    dispatchMouseSequence(target, "click");
    if (typeof target.click === "function") target.click();
  }

  // Select an autocomplete suggestion. ServiceNow's reference completer commits
  // the choice on mousedown of the inner cell (before the input blur hides the
  // list), so target the <td>/text node and lead with hover + mousedown.
  function clickOption(opt) {
    const target = (opt.querySelector && opt.querySelector("td.ac_cell, td, a")) || opt;
    try { target.scrollIntoView({ block: "nearest" }); } catch (e) {}
    const o = { bubbles: true, cancelable: true, view: window };
    target.dispatchEvent(new MouseEvent("mouseover", o));
    target.dispatchEvent(new MouseEvent("mousemove", o));
    target.dispatchEvent(new MouseEvent("mousedown", o));
    target.dispatchEvent(new MouseEvent("mouseup", o));
    target.dispatchEvent(new MouseEvent("click", o));
    if (typeof target.click === "function") target.click();
  }

  const KEY_CODES = { Enter: 13, Tab: 9, Escape: 27, ArrowDown: 40, ArrowUp: 38, Backspace: 8 };
  function keyCodeFor(key) {
    if (KEY_CODES[key] != null) return KEY_CODES[key];
    if (key && key.length === 1) return key.toUpperCase().charCodeAt(0);
    return 0;
  }
  // KeyboardEvent ignores keyCode/which in its init dict, but ServiceNow's
  // legacy autocomplete reads them — force them onto the event so the reference
  // lookup actually fires.
  function makeKeyEvent(type, key, extra) {
    const code = keyCodeFor(key);
    const e = new KeyboardEvent(type, { key, code: key, bubbles: true, cancelable: true, ...extra });
    try {
      Object.defineProperty(e, "keyCode", { get: () => code });
      Object.defineProperty(e, "which", { get: () => code });
    } catch (err) {}
    return e;
  }
  function dispatchKeyboard(target, key, extra = {}) {
    target.dispatchEvent(makeKeyEvent("keydown", key, extra));
    target.dispatchEvent(makeKeyEvent("keypress", key, extra));
    target.dispatchEvent(makeKeyEvent("keyup", key, extra));
  }

  function setNativeValue(element, value) {
    const proto = Object.getPrototypeOf(element);
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(element, value);
    else element.value = value;
  }

  function selectAllText(control) {
    control.focus();
    const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform);
    dispatchKeyboard(control, "a", isMac ? { metaKey: true } : { ctrlKey: true });
    if (typeof control.select === "function") control.select();
    if (typeof control.setSelectionRange === "function") {
      const v = control.value || "";
      try { control.setSelectionRange(0, v.length); } catch (e) {}
    }
  }

  function clearEditorValue(control) {
    selectAllText(control);
    setNativeValue(control, "");
    control.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function typeTextLikeUser(control, value) {
    control.focus();
    setNativeValue(control, value);
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Type into a reference/combobox input one character at a time, firing real
  // key events per keystroke. This both makes the text visibly appear and
  // triggers ServiceNow's reference lookup (which a single bulk set does not).
  // Re-acquires the live element by id each keystroke in case SN re-renders it.
  async function typeReference(control, value) {
    const id = control.id;
    const live = () => (id && document.getElementById(id)) || control;
    let el = live();
    el.focus();
    clearEditorValue(el);
    let acc = "";
    for (const ch of value) {
      acc += ch;
      el = live();
      el.focus();
      setNativeValue(el, acc);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      dispatchKeyboard(el, ch);
      await sleep(60);
    }
    return el;
  }

  function findAutocompleteOptions() {
    const selectors = [
      "#AC\\.cell_edit_value li", "#AC li", "#ac_reference_input_list li",
      "ul.ui-autocomplete li", ".ui-autocomplete li", ".ac_results li",
      ".ac_cell", ".autocomplete li", "[id^='AC.'] li", "[id^='AC.'] a", "[id^='AC.'] tr",
    ];
    const opts = selectors.flatMap((s) => { try { return Array.from(document.querySelectorAll(s)); } catch (e) { return []; } });
    return Array.from(new Set(opts)).filter((el) => isVisible(el) && normalize(el.textContent));
  }

  const waitForAutocompleteOption = () =>
    waitFor(() => findAutocompleteOptions()[0] || null, { timeout: T.autocompleteTimeout });

  // Suggestions for a specific reference input: ServiceNow names the results
  // container after the field via aria-owns (e.g. "AC.LIST_EDIT_alm_asset.department").
  // Prefer that list; fall back to the global scan if it isn't found.
  // The reference list has a "Showing 1 through 14 of 14" counter row that is
  // not a real option — never offer it as a pick.
  const isRealOption = (el) => {
    const t = normalize(el.textContent);
    return t && !/^showing\b.*\bof\b/.test(t);
  };
  function autocompleteOptionsFor(control) {
    const owns = control && control.getAttribute && control.getAttribute("aria-owns");
    const list = owns ? document.getElementById(owns) : null;
    if (list) {
      // The ac_dropdown (role=listbox) renders options as role="option" rows, or
      // as table rows / ac_cell divs in older skins. Try the most specific match
      // first so we click a real option, not a wrapper.
      for (const sel of ['[role="option"]', "li", "a", "tr", "td.ac_cell", ".ac_cell"]) {
        const items = Array.from(list.querySelectorAll(sel))
          .filter((el) => isVisible(el) && isRealOption(el));
        if (items.length) return items;
      }
    }
    return findAutocompleteOptions().filter(isRealOption);
  }

  // Pick the suggestion that matches the wanted value: exact first, then
  // contains, then fall back to the first one (old behavior) so we never
  // regress fields that only ever return a single suggestion.
  function pickAutocompleteOption(options, value) {
    const want = normalize(value);
    return (
      options.find((o) => normalize(o.textContent) === want) ||
      options.find((o) => normalize(o.textContent).includes(want)) ||
      options[0] || null
    );
  }

  // Find the <option> that best matches the wanted value. Lenient like the
  // autocomplete path (which just takes the first filtered suggestion): try an
  // exact normalized match on text/value first, then fall back to a
  // contains-match so trailing context ("IT (Company)") still resolves.
  function findSelectOption(control, value) {
    const opts = Array.from(control.options || []).filter((o) => normalize(o.textContent) || o.value);
    const want = normalize(value);
    if (!want) return null;
    return (
      opts.find((o) => normalize(o.textContent) === want || normalize(o.value) === want) ||
      opts.find((o) => normalize(o.textContent).includes(want) || want.includes(normalize(o.textContent))) ||
      null
    );
  }

  // A <select> editor (choice field) may not have its <option>s populated the
  // instant it appears (e.g. dependent choice lists). Poll until the wanted
  // option exists, like the autocomplete path waits for its suggestion box.
  const waitForSelectOption = (control, value) =>
    waitFor(() => findSelectOption(control, value), { timeout: T.autocompleteTimeout });

  function getEditorCell(orig) {
    return document.querySelector("td.vt.list_edit_cursor_cell") ||
           document.querySelector("td.list_edit_cursor_cell") || orig;
  }
  const waitForEditorCell = (cell) =>
    waitFor(() => { const a = getEditorCell(cell); return a && isVisible(a) ? a : null; }, { timeout: T.editorOpenTimeout });

  // --- Label-aware control finding (handles the State+Substate combined window)
  function controlLabel(el) {
    const cont = el.closest('td[id^="container_"]');
    if (cont) return (cont.textContent || "").replace(/\s+/g, " ").trim().split(":")[0].trim();
    const sr = document.getElementById("sr-container");
    return sr ? (sr.textContent || "").trim() : "";
  }

  function getEditorControls() {
    const out = [];
    const add = (el) => { if (el && !out.includes(el)) out.push(el); };
    document.querySelectorAll(".list-edit-input, .list_edit_input, input[role=combobox], input[id*='sys_display'], #cell_edit_value")
      .forEach(add);
    return out.filter(isVisible).map((el) => ({ el, label: controlLabel(el) }));
  }

  function findControl(label) {
    const controls = getEditorControls();
    if (!controls.length) return null;
    const m = controls.find((c) => normalize(c.label) === normalize(label));
    if (m) return m.el;
    if (controls.length === 1) return controls[0].el;
    const combo = controls.find((c) => c.el.matches && c.el.matches("input[role=combobox], input[id*='sys_display']"));
    if (combo) return combo.el;
    return controls[0].el;
  }
  const waitForControl = (label) =>
    waitFor(() => findControl(label), { timeout: T.editorReadyTimeout });

  function detectBehavior(control) {
    const tag = (control.tagName || "").toLowerCase();
    if (tag === "select") return "choice";
    const role = (control.getAttribute("role") || "").toLowerCase();
    // ServiceNow reference fields use id "sys_display.LIST_EDIT_<table>.<field>".
    // That prefix is present immediately, whereas role=combobox/aria-owns are
    // attached a beat later — so key off the id to avoid racing into "direct".
    if (/^sys_display\./.test(control.id || "")) return "autocomplete";
    if (role === "combobox" || control.getAttribute("aria-autocomplete") || control.getAttribute("aria-owns"))
      return "autocomplete";
    return "direct";
  }

  // --- Confirm (Save) button
  function findConfirmButton() {
    const explicit = document.getElementById("cell_edit_ok");
    if (explicit && isVisible(explicit)) return explicit;
    return null;
  }
  const waitForConfirmButton = () => waitFor(findConfirmButton, { timeout: T.editorReadyTimeout });

  // The inline editor is "open" while the Save button or the active edit cell is
  // present. Once it's gone after a Save, the edit committed — even if the list
  // cell renders the saved text differently from what we typed (e.g. Comments,
  // which is truncated/formatted), so an exact text match would never succeed.
  function editorIsOpen() {
    if (findConfirmButton()) return true;
    const cell = document.querySelector("td.vt.list_edit_cursor_cell, td.list_edit_cursor_cell");
    return !!(cell && isVisible(cell));
  }
  async function waitForEditorClosed(timeout = T.shortSaveTimeout) {
    return !!(await waitFor(() => (editorIsOpen() ? null : true), { timeout }));
  }

  async function waitForCellValue(cell, value, timeout = T.saveTimeout) {
    const expected = normalize(value);
    return !!(await waitFor(() => normalize(fullCellText(cell)) === expected ? cell : null, { timeout }));
  }
  async function waitForApplied(control, value, timeout = T.editorReadyTimeout) {
    const expected = normalize(value);
    return !!(await waitFor(() => normalize(control.value || control.textContent) === expected ? true : null, { timeout }));
  }

  async function applyBehavior(control, value, behavior) {
    // Empty expected value = delete the current value. Clear the editor input
    // (or select the blank "-- None --" option on a real dropdown); commit()
    // then clicks the green OK button to save the now-empty cell.
    if (!normalize(value)) {
      if (behavior === "choice") {
        const none = Array.from(control.options || []).find(
          (o) => !normalize(o.textContent) || /none/.test(normalize(o.textContent)));
        control.focus();
        control.value = none ? none.value : "";
        control.dispatchEvent(new Event("input", { bubbles: true }));
        control.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        const el = (control.id && document.getElementById(control.id)) || control;
        el.focus();
        clearEditorValue(el);
      }
      return true;
    }
    if (behavior === "choice") {
      const opt = await waitForSelectOption(control, value);
      if (!opt) {
        // No matching option appeared. Log what the <select> actually offered so
        // we can tell whether the value is mislabeled or the list is dependent.
        const seen = Array.from(control.options || []).map((o) => (o.textContent || "").trim());
        console.warn("[inv] no <select> option matched: " + JSON.stringify({
          field: control.id || control.name || "(select)", wanted: value, options: seen }));
        return false;                    // → retry / report as failed
      }
      control.focus();
      control.value = opt.value;
      control.dispatchEvent(new Event("input", { bubbles: true }));
      control.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (behavior === "autocomplete") {
      // Type the value character-by-character (real keystrokes) so the text
      // shows up and ServiceNow's reference lookup actually fires.
      const live = await typeReference(control, value);
      const ownsId = (live.getAttribute && live.getAttribute("aria-owns")) || "";
      console.log("[inv] typed into " + (live.id || "(ref)") + " → input.value is now \"" + live.value + "\"");
      await sleep(T.legacyAutocompletePause);
      // While polling for options, snapshot the dropdown HTML whenever it has
      // content, so we can inspect its real markup even after it closes.
      const opt = await waitFor(() => {
        const container = ownsId ? document.getElementById(ownsId) : null;
        if (container && container.children.length) {
          window.__inv_lastAC = container.outerHTML;
        }
        const o = autocompleteOptionsFor(live);
        return o.length ? pickAutocompleteOption(o, value) : null;
      }, { timeout: T.autocompleteTimeout });
      if (!opt) {
        console.warn("[inv] no reference suggestions appeared: " + JSON.stringify({
          field: live.id || live.name || "(ref)",
          wanted: value,
          typed: live.value,
          sawDropdown: !!window.__inv_lastAC,
          ariaInvalid: live.getAttribute && live.getAttribute("aria-invalid"),
        }));
        if (window.__inv_lastAC) {
          console.warn("[inv] last dropdown HTML (copy the string below):\n" + window.__inv_lastAC);
        }
        return false;
      }
      console.log("[inv] picking option \"" + (opt.textContent || "").trim() + "\" for " + (live.id || "(ref)"));
      clickOption(opt);
      return true;
    }
    // direct text
    clearEditorValue(control);
    typeTextLikeUser(control, value);
    return waitForApplied(control, value);
  }

  async function commit(control, cell, value, behavior) {
    const ok = await waitForConfirmButton();
    if (ok) {
      clickElement(ok);
      if (await waitForCellValue(cell, value, behavior === "autocomplete" ? T.saveTimeout : T.shortSaveTimeout)) return true;
      // Saved but the cell text doesn't match exactly (e.g. Comments): the editor
      // closing after Save is itself proof the value committed.
      if (await waitForEditorClosed()) return true;
    }
    dispatchKeyboard(control, "Enter");
    if (await waitForCellValue(cell, value, T.shortSaveTimeout)) return true;
    if (await waitForEditorClosed()) return true;
    dispatchKeyboard(control, "Tab");
    if (typeof control.blur === "function") control.blur();
    if (await waitForCellValue(cell, value)) return true;
    return waitForEditorClosed();
  }

  async function editCellOnce(cell, label, value) {
    cell.scrollIntoView({ block: "center", inline: "nearest" });
    clickElement(cell);
    dispatchMouseSequence(cell, "dblclick");
    const active = await waitForEditorCell(cell);
    if (!active) { console.warn("[inv] editor cell never opened for " + label); return false; }
    const control = await waitForControl(label);
    if (!control) { console.warn("[inv] no editor control found for " + label); return false; }
    const behavior = detectBehavior(control);
    console.log("[inv] editing " + label + " → \"" + value + "\" via " + behavior +
                " (control=" + (control.id || control.name || control.tagName) + ")");
    if (!(await applyBehavior(control, value, behavior))) return false;
    return commit(control, active, value, behavior);
  }

  async function editCell(cell, label, value) {
    for (let attempt = 1; attempt <= T.maxRetries; attempt += 1) {
      if (__aborted) return false;
      if (attempt > 1) setRunning(`Retrying ${label} → “${value}” (attempt ${attempt}/${T.maxRetries})…`);
      if (await editCellOnce(cell, label, value)) return true;
      // close any stray editor before retrying
      const cancel = document.getElementById("cell_edit_cancel");
      if (cancel && isVisible(cancel)) { clickElement(cancel); await sleep(150); }
      await sleep(T.retryBackoff * attempt);
    }
    return false;
  }

  // The first inline edit on a freshly loaded list usually fails until the grid
  // is "activated", so open and cancel an edit on the first cell to warm it up.
  async function primeCell(cell) {
    cell.scrollIntoView({ block: "center", inline: "nearest" });
    clickElement(cell);
    dispatchMouseSequence(cell, "dblclick");
    await waitForEditorCell(cell);
    const cancel = document.getElementById("cell_edit_cancel");
    if (cancel && isVisible(cancel)) clickElement(cancel);
    await sleep(350);
  }

  // --- Table / header / row helpers (find the cell for an asset_tag + column)
  function getHeaderCells(table) {
    for (const sel of ["thead th", "thead td", "tr.list_header th", "th", "[role=columnheader]"]) {
      const cells = Array.from(table.querySelectorAll(sel)).filter(isVisible);
      if (cells.length) return cells;
    }
    return [];
  }
  function findTables() {
    return Array.from(document.querySelectorAll("table")).filter((t) => getHeaderCells(t).length);
  }
  function getColumnIndex(table, wanted) {
    const w = normalize(wanted);
    if (!w) return -1;
    const texts = getHeaderCells(table).map((h) => normalize(h.innerText || h.textContent));
    // Exact match first, so "State" never grabs the "Substate" column (and vice
    // versa) just because one label is a substring of the other.
    const exact = texts.indexOf(w);
    if (exact !== -1) return exact;
    // Fall back to fuzzy contains only when there's no exact header.
    for (let i = 0; i < texts.length; i += 1) {
      if (texts[i] && (texts[i].includes(w) || w.includes(texts[i]))) return i;
    }
    return -1;
  }
  function getRows(table) {
    let rows = Array.from(table.querySelectorAll("tbody tr"));
    if (!rows.length) rows = Array.from(table.querySelectorAll("tr"));
    return rows.filter((r) => !r.closest("thead") && r.querySelector("td"));
  }

  const orderFields = (fields, fieldOrder) => Object.keys(fields).sort((a, b) => {
    const ia = fieldOrder.indexOf(normalize(a)), ib = fieldOrder.indexOf(normalize(b));
    return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
  });

  async function applyCorrections(plan, fieldOrder) {
    fieldOrder = fieldOrder || [];
    const labels = new Set();
    Object.values(plan).forEach((f) => Object.keys(f).forEach((l) => labels.add(l)));

    // Only the frame that actually holds the asset list does the work / shows UI.
    const tables = findTables().filter((t) => getColumnIndex(t, "asset tag") >= 0);
    if (!tables.length) return { applied: 0, already: 0, failed: 0, matched: 0, targets: 0 };

    // Pre-count the cells we'll touch, for a progress denominator.
    let plannedTotal = 0;
    for (const table of tables) {
      const tagCol = getColumnIndex(table, "asset tag");
      const colByLabel = {};
      labels.forEach((l) => { const i = getColumnIndex(table, l); if (i >= 0) colByLabel[l] = i; });
      for (const row of getRows(table)) {
        const tagCell = row.children[tagCol];
        if (!tagCell || tagCell.tagName === "TH") continue;
        const fields = plan[(tagCell.innerText || "").trim()];
        if (!fields) continue;
        plannedTotal += Object.keys(fields).filter((l) => colByLabel[l] !== undefined).length;
      }
    }

    let applied = 0, already = 0, failed = 0, targets = 0, done = 0;
    const matchedRows = new Set();
    let primed = false;
    // Fresh run: clear any earlier Stop and re-enable the button.
    __aborted = false;
    const stopBtn = document.getElementById("__inv_stop");
    if (stopBtn) { stopBtn.disabled = false; stopBtn.textContent = "■ Stop (Esc)"; }
    setRunning(`Preparing ${plannedTotal} cell(s)… (press Esc or Stop to abort)`);

    for (const table of tables) {
      if (__aborted) break;
      const tagCol = getColumnIndex(table, "asset tag");
      const colByLabel = {};
      labels.forEach((l) => { const i = getColumnIndex(table, l); if (i >= 0) colByLabel[l] = i; });

      for (const row of getRows(table)) {
        if (__aborted) break;
        const tagCell = row.children[tagCol];
        if (!tagCell || tagCell.tagName === "TH") continue;
        const tag = (tagCell.innerText || "").trim();
        const fields = plan[tag];
        if (!fields) continue;
        matchedRows.add(row);

        for (const label of orderFields(fields, fieldOrder)) {
          if (__aborted) break;
          const ci = colByLabel[label];
          if (ci === undefined) { failed += 1; continue; }
          const cell = row.children[ci];
          if (!cell) { failed += 1; continue; }
          targets += 1;
          const value = String(fields[label]);
          // An empty expected value clears the field: when it differs from the
          // current cell, apply "" so the cell becomes blank ("(empty)"). If the
          // cell is already blank, the equality check below treats it as already
          // correct (nothing to do) — so this never re-clears an empty cell.
          if (normalize(fullCellText(cell)) === normalize(value)) {
            already += 1; done += 1; cell.style.outline = "2px solid #94a3b8"; continue;
          }
          if (!primed) {
            setRunning("🔥 Warming up the list (first edit)…");
            await primeCell(cell);
            primed = true;
          }
          setRunning(`Cell ${done + 1}/${plannedTotal} — <b>${tag}</b> · ${label} → “${value}”`);
          const ok = await editCell(cell, label, value);
          if (ok) { applied += 1; cell.style.outline = "2px solid #16a34a"; }
          else { failed += 1; cell.style.outline = "2px solid #dc2626"; }
          done += 1;
          await sleep(T.betweenCells);
        }
      }
    }

    const allOk = failed === 0 && !__aborted;
    setStatus(
      (__aborted ? "🛑 <b>Stopped</b>" : allOk ? "✅ <b>Finished</b>" : "⚠️ <b>Finished with issues</b>") +
      `<br>Committed ${applied} · already correct ${already} · failed ${failed}` +
      `<br><span style="font-size:11px;opacity:.8">of ${plannedTotal} cell(s) — you can close this.</span>`,
      __aborted ? "#7f1d1d" : allOk ? "#166534" : "#92400e"
    );
    return { applied, already, failed, matched: matchedRows.size, targets, aborted: __aborted };
  }

  // Exposed so the popup can call it directly via chrome.scripting.executeScript
  // (same isolated world, per frame).
  window.__invApply = applyCorrections;

  chrome.runtime.onMessage.addListener((msg, sender, send) => {
    if (msg && msg.type === "INV_PING") { send({ ok: true }); return false; }
    if (msg && msg.type === "INV_APPLY") {
      applyCorrections(msg.plan || {}, msg.fieldOrder || [])
        .then(send)
        .catch((e) => send({ error: e && e.message ? e.message : "content script error" }));
      return true; // async response
    }
    return false;
  });
})();
