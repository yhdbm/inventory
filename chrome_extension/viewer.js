// Inventory Corrections viewer — imports the exported CSV and displays it.
// CSV parsing/validation/storage helpers live in csv-import.js (shared).

let allRows = [];  // [{asset_tag, field, label, current, expected, status}]

const $ = (s) => document.querySelector(s);

function isChange(r) {
  // A real difference: has a field and current != expected.
  return r.field && (r.current || "") !== (r.expected || "");
}

function render() {
  const term = $("#search").value.trim().toLowerCase();
  const onlyChanges = $("#only-changes").checked;
  const tbody = $("#rows");
  tbody.innerHTML = "";

  const filtered = allRows.filter((r) => {
    if (onlyChanges && !isChange(r)) return false;
    if (!term) return true;
    return [r.asset_tag, r.field, r.label, r.current, r.expected, r.status]
      .some((v) => (v || "").toLowerCase().includes(term));
  });

  for (const r of filtered) {
    const tr = document.createElement("tr");
    const change = isChange(r);
    tr.className = change ? "changed" : "nochange";
    tr.innerHTML =
      `<td><strong>${esc(r.asset_tag) || "—"}</strong></td>` +
      `<td>${esc(r.label || r.field) || "<span class='muted'>no change</span>"}</td>` +
      `<td>${esc(r.current) || "—"}</td>` +
      `<td class="expected">${esc(r.expected) || "—"}</td>` +
      `<td><span class="pill">${esc(r.status) || "—"}</span></td>`;
    tbody.appendChild(tr);
  }

  const assets = uniqueTags().length;
  const changes = allRows.filter(isChange).length;
  $("#summary").textContent = allRows.length
    ? `${allRows.length} rows · ${assets} unique asset tag(s) · ${changes} field change(s) · showing ${filtered.length}`
    : "";
  $("#empty").style.display = allRows.length ? "none" : "block";
}

function esc(s) {
  return (s == null ? "" : String(s))
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function load(rows) {
  allRows = rows;
  storeCorrections(rows);  // wipes old data, then stores the new import
  render();
}

function showImportMsg(file, count) {
  const el = $("#import-msg");
  el.textContent = "✓ Imported " + count + " rows from “" + file.name + "” at " +
                   new Date().toLocaleTimeString() + " — previous data replaced.";
  el.style.display = "block";
}

// --- Wiring ---------------------------------------------------------------
$("#file").addEventListener("change", function (e) {
  const file = e.target.files[0];
  if (!file) return;
  parseCorrectionsCSV(file, function (rows, err) {
    if (err) { alert(err); $("#file").value = ""; return; }
    load(rows);
    showImportMsg(file, rows.length);
  });
});

// --- ServiceNow list link from the unique asset tags ----------------------
const SN_BASE = "https://your-instance.service-now.com/now/nav/ui/classic/params/target/";

function uniqueTags() {
  const seen = new Set();
  const out = [];
  for (const r of allRows) {
    const t = (r.asset_tag || "").trim();
    if (t && !seen.has(t)) { seen.add(t); out.push(t); }
  }
  return out;
}

function buildServiceNowUrl(tags) {
  // asset_tag=<t1>^ORasset_tag=<t2>... encoded once, then the whole target
  // (alm_asset_list.do?...) encoded again — matches ServiceNow's nav format.
  const inner = tags.map((t) => "asset_tag=" + t).join("^OR");
  const target = "alm_asset_list.do?sysparm_query=" + encodeURIComponent(inner) +
                 "&sysparm_first_row=1&sysparm_view=";
  return SN_BASE + encodeURIComponent(target);
}

$("#open-sn").addEventListener("click", function () {
  const tags = uniqueTags();
  if (!tags.length) { alert("No asset tags to open."); return; }
  window.open(buildServiceNowUrl(tags), "_blank");
});

$("#copy-sn").addEventListener("click", function () {
  const tags = uniqueTags();
  if (!tags.length) { alert("No asset tags to copy."); return; }
  navigator.clipboard.writeText(buildServiceNowUrl(tags)).then(
    function () { $("#copy-sn").textContent = "Copied!"; setTimeout(function () { $("#copy-sn").textContent = "Copy link"; }, 1500); },
    function () { alert("Could not copy to clipboard."); }
  );
});

$("#search").addEventListener("input", render);
$("#only-changes").addEventListener("change", render);
$("#clear").addEventListener("click", function () {
  allRows = [];
  chrome.storage.local.remove(["corrections", "importedAt"]);
  $("#file").value = "";
  $("#import-msg").style.display = "none";
  render();
});

// Restore the last import on open, and show when it was imported.
chrome.storage.local.get(["corrections", "importedAt"], function (data) {
  allRows = (data && data.corrections) || [];
  if (allRows.length && data.importedAt) {
    const el = $("#import-msg");
    el.textContent = "✓ " + allRows.length + " rows loaded (imported " +
                     new Date(data.importedAt).toLocaleString() + ").";
    el.style.display = "block";
  }
  render();
});
