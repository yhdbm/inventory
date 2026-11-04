// Strategies settings page: per-field edit strategy with hardcoded defaults,
// green highlight on the default value, auto-save, and rollback.

const optLabel = (v) => (STRATEGY_OPTIONS.find((o) => o.value === v) || {}).label || v;

function flash(msg) {
  const el = document.getElementById("saved");
  el.textContent = msg;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => { el.textContent = ""; }, 1500);
}

function markDefault(sel) {
  const key = sel.dataset.key;
  sel.classList.toggle("is-default", sel.value === STRATEGY_DEFAULTS[key]);
}

function saveAll(changedSel) {
  const strategies = {};
  document.querySelectorAll("select[data-key]").forEach((sel) => {
    strategies[sel.dataset.key] = sel.value;
  });
  chrome.storage.local.set({ strategies: strategies }, function () {
    flash("✓ Saved");
    if (changedSel) {
      changedSel.classList.add("just-saved");
      setTimeout(() => changedSel.classList.remove("just-saved"), 1000);
    }
  });
}

function render(current) {
  const tbody = document.getElementById("rows");
  tbody.innerHTML = "";
  STRATEGY_FIELDS.forEach((f) => {
    const val = current[f.key];
    const tr = document.createElement("tr");

    const tdField = document.createElement("td");
    tdField.textContent = f.label;

    const tdSel = document.createElement("td");
    const sel = document.createElement("select");
    sel.dataset.key = f.key;
    STRATEGY_OPTIONS.forEach((o) => {
      const opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = o.label;
      if (o.value === val) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", function () {
      markDefault(sel);
      saveAll(sel);
    });
    tdSel.appendChild(sel);

    const tdDef = document.createElement("td");
    tdDef.className = "def-col";
    tdDef.textContent = optLabel(STRATEGY_DEFAULTS[f.key]);

    tr.appendChild(tdField);
    tr.appendChild(tdSel);
    tr.appendChild(tdDef);
    tbody.appendChild(tr);

    markDefault(sel);
  });
}

document.getElementById("reset").addEventListener("click", function () {
  // Roll back: forget overrides and re-render with the hardcoded defaults.
  chrome.storage.local.remove("strategies", function () {
    render(Object.assign({}, STRATEGY_DEFAULTS));
    flash("Rolled back to defaults");
  });
});

loadStrategies(render);
